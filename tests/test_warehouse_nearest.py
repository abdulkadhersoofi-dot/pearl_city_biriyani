"""The nearest-warehouse default (app/warehouse/nearest.py): service area beats
distance, an exact pincode beats a prefix, nearest-with-stock beats
nearest-but-short, an unknown pincode uses the branch's fallback, a manual
pick is stored as "manual", an order can be split across warehouses, and the
delivery address goes from the order to the invoice and the e-way bill."""

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db as _db
from app.inventory import services as inv
from app.invoicing.services import LineInput
from app.models.customer import Customer
from app.models.inventory import AdjustmentReason, LocationKind, StockLocation
from app.models.invoice_series import DocumentType
from app.models.product import Product
from app.models.sales import SalesDocument, SalesDocumentKind
from app.models.warehouse import PincodeGeo
from app.sales import services as ss
from app.warehouse import nearest, pincodes

D = Decimal
TODAY = date.today()

GEO = {   # pincode: (lat, lng, district)
    "628001": (8.7642, 78.1348, "Thoothukudi"), "628002": (8.8000, 78.1500, "Thoothukudi"),
    "628215": (8.4955, 78.1219, "Thoothukudi"), "627001": (8.7139, 77.7567, "Tirunelveli"),
    "627011": (8.7200, 77.7000, "Tirunelveli"), "625001": (9.9252, 78.1198, "Madurai"),
    "625020": (9.9300, 78.1300, "Madurai"),
}


@pytest.fixture()
def three(db, tenant, client_admin, staff):
    """Warehouses at Thoothukudi (A, 2 fans), Tirunelveli (B, 4) and Madurai (C, 10)."""
    for key in ("inventory", "purchases", "serial_imei", "sales_documents", "warehouse_dispatch", "eway_bill"):
        tenant.set_feature(key, True)
    for pin, (lat, lng, district) in GEO.items():
        db.session.add(PincodeGeo(pincode=pin, lat=lat, lng=lng, district=district, state="Tamil Nadu"))
    a = StockLocation(tenant_id=tenant.id, name="A Thoothukudi", kind=LocationKind.GODOWN, pincode="628001")
    b = StockLocation(tenant_id=tenant.id, name="B Tirunelveli", kind=LocationKind.GODOWN, pincode="627001")
    c = StockLocation(tenant_id=tenant.id, name="C Madurai", kind=LocationKind.GODOWN, pincode="625001")
    fan = Product(tenant_id=tenant.id, name="Ceiling fan", hsn_or_sac_code="8414", gst_rate=D("18"), unit="PCS",
                  sale_price=D("2500"), allow_negative_stock=False)
    buyer = Customer(tenant_id=tenant.id, name="Ravi Stores", state_code="27", state_name="Maharashtra",
                     address_line1="12 Beach Road", city="Thoothukudi", pincode="628002", phone="9876500000")
    db.session.add_all([a, b, c, fan, buyer])
    db.session.commit()
    for loc, qty in ((a, 2), (b, 4), (c, 10)):
        inv.create_adjustment(tenant, client_admin, loc.id, AdjustmentReason.OPENING,
                              [inv.StockLineInput(fan.id, D(qty), D("1500"))])
    db.session.commit()
    return type("T", (), {"a": a, "b": b, "c": c, "fan": fan, "buyer": buyer, "tenant": tenant,
                          "admin": client_admin, "staff": staff})


def fans(t, qty):
    return [LineInput(description="Ceiling fan", hsn_or_sac_code="8414", qty=D(qty), rate=D("2500"),
                      discount_percent=D("0"), gst_rate=D("18"), product_id=t.fan.id, unit="PCS")]


def order(t, qty="1", **kw):
    doc = ss.create_sales_document(t.tenant, kw.pop("user", t.admin), ss.SalesDocInput(
        kind=SalesDocumentKind.SALES_ORDER, gstin_id=t.tenant.gstins.first().id, customer_id=t.buyer.id,
        place_of_supply_state_code="27", doc_date=TODAY, invoice_document_type=DocumentType.TAX_INVOICE,
        lines=fans(t, qty), **kw))
    _db.session.commit()
    return doc


@pytest.mark.parametrize("pincode,qty,areas,expect,reason", [
    ("628002", "1", {}, "A", "nearest"),
    ("627011", "1", {}, "B", "nearest"),
    ("625020", "1", {}, "C", "nearest"),
    ("628215", "1", {}, "A", "nearest"),
    ("628002", "3", {}, "B", "nearest_in_stock"),        # A has 2: the nearest that has 3
    ("628002", "5", {}, "C", "nearest_in_stock"),        # A 2, B 4: Madurai
    ("628002", "12", {}, "A", "nearest"),                # nobody has 12: the nearest, short
    ("628002", "1", {"C": "628"}, "C", "service_area"),  # the service area wins over distance
    ("628002", "1", {"C": "628", "B": "628002"}, "B", "service_area"),   # exact pincode beats prefix
    ("628215", "1", {"C": "628", "B": "628002"}, "C", "service_area"),
    ("999999", "1", {}, "C", "fallback"),                # unknown pincode: the branch's fallback
    ("", "1", {}, "C", "fallback"),
])
def test_the_suggestion_table(three, pincode, qty, areas, expect, reason):
    for key, codes in areas.items():
        nearest.set_service_area(getattr(three, key.lower()), codes)
    from app.inventory import ledger

    ledger.location_for_user(three.tenant.id, three.staff).default_dispatch_location_id = three.c.id
    s = nearest.suggest_warehouse(three.tenant, pincode, fans(three, qty),
                                  fallback=ledger.location_for_user(three.tenant.id, three.staff).default_dispatch_location)
    assert (s.location.name[0], s.reason) == (expect, reason), s.message
    if reason == "fallback":
        assert "Pincode not found" in s.message or "No delivery pincode" in s.message


def test_the_ranked_list_and_why(three):
    s = nearest.suggest_warehouse(three.tenant, "628002", fans(three, "3"))
    assert [o.location.name[0] for o in s.ranked] == ["A", "B", "C"]
    assert 4 < s.ranked[0].km < 6 and 40 < s.ranked[1].km < 55 and 120 < s.ranked[2].km < 135
    assert s.ranked[0].short == [("Ceiling fan", D("2"), D("3"))]
    assert "A Thoothukudi (4." in s.message and "is short of Ceiling fan (2 free of 3) - defaulted to B Tirunelveli" in s.message
    big = nearest.suggest_warehouse(three.tenant, "628002", fans(three, "12"))
    assert [(loc.name[0], parts) for loc, parts in big.split] == [("A", {0: D("2")}), ("B", {0: D("4")}), ("C", {0: D("6")})]
    assert nearest.suggest_warehouse(three.tenant, "628002", fans(three, "17")).split is None


def test_a_warehouse_point_typed_in_beats_its_pincode(three):
    three.c.latitude, three.c.longitude = 8.80, 78.15          # say C really sits next to the customer
    _db.session.commit()
    assert nearest.suggest_warehouse(three.tenant, "628002", fans(three, "1")).location.id == three.c.id


def test_an_order_stores_the_warehouse_and_why_and_manual_when_overridden(three):
    auto = order(three)                                         # no warehouse given: ARFA's choice (bulk / API)
    assert auto.dispatch_location_id == three.a.id and auto.dispatch_choice_reason == "nearest"
    manual = order(three, dispatch_location_id=three.c.id)
    assert manual.dispatch_choice_reason == "manual"
    same = order(three, dispatch_location_id=three.a.id)
    assert same.dispatch_choice_reason == "nearest"


def test_the_delivery_address_defaults_from_the_customer_and_can_differ(three):
    first = order(three)
    assert first.ship_to["pincode"] == "628002" and first.ship_to["address_line1"] == "12 Beach Road"
    other = order(three, ship_to={"name": "Ravi (godown)", "address_line1": "4 Mill St", "city": "Madurai",
                                  "pincode": "625020", "phone": "9000000000"})
    assert other.ship_to["pincode"] == "625020" and other.dispatch_location_id == three.c.id   # nearest to the new address
    assert Customer.query.get(three.buyer.id).pincode == "628002"                              # the customer is unchanged
    with pytest.raises(ss.SalesError, match="6 digits"):
        order(three, ship_to={"address_line1": "x", "pincode": "62"})


def test_the_delivery_address_reaches_the_invoice_and_the_eway_bill(three):
    from app.einvoice import eway
    from app.invoicing.services import InvoiceInput, create_invoice

    doc = order(three, ship_to={"name": "Ravi (godown)", "address_line1": "4 Mill St", "city": "Madurai",
                                "pincode": "625020"})
    invoice = create_invoice(three.tenant, three.admin, InvoiceInput(
        gstin_id=doc.gstin_id, document_type=DocumentType.TAX_INVOICE, customer_id=doc.customer_id,
        place_of_supply_state_code="27", invoice_date=TODAY, notes=None, lines=fans(three, "1"),
        ship_to=doc.ship_to, dispatch_location_id=three.c.id))
    _db.session.commit()
    assert invoice.ship_to["pincode"] == "625020"
    draft = eway.draft_from(three.tenant, invoice, three.admin)
    assert draft.to_party["pincode"] == "625020" and draft.to_party["addr1"] == "4 Mill St"
    from flask import render_template

    html = render_template("invoicing/pdf.html", invoice=invoice, document_label="Tax Invoice", firm_name="Acme",
                           logo_data_uri=None)
    assert "Ship to" in html and "4 Mill St" in html


def test_an_order_too_big_for_one_warehouse_is_split_when_asked(three):
    with pytest.raises(ss.SalesError, match="Only 2 of Ceiling fan free at A"):
        order(three, qty="9", dispatch_location_id=three.a.id)
    _db.session.rollback()
    first = order(three, qty="9", dispatch_location_id=three.a.id, split=True)
    parts = SalesDocument.query.order_by(SalesDocument.id).all()
    assert [(p.dispatch_location.name[0], p.lines[0].qty) for p in parts] == [("A", D("2")), ("B", D("4")), ("C", D("3"))]
    assert {p.split_group_id for p in parts} == {first.id} and {p.dispatch_choice_reason for p in parts} == {"split"}
    assert all(p.dispatch_status.value == "to_pick" for p in parts)
    assert sum(p.grand_total for p in parts) == D("9") * D("2500") * D("1.18")


def test_an_emi_sale_isnt_split(three):
    from app.warehouse.emi import add_financier

    f = add_financier(three.tenant, "Bajaj Finserv")
    with pytest.raises(ss.SalesError, match="can't be split"):
        order(three, qty="9", dispatch_location_id=three.a.id, split=True, is_finance_sale=True, financier_id=f.id,
              finance_amount=D("10000"))


def test_the_order_form_asks_for_the_suggestion(three, client):
    client.post("/auth/login", data={"email": three.staff.email, "password": "CashierSecret123"})
    res = client.get(f"/warehouse/suggest.json?pincode=628002&p={three.fan.id}&q=3").get_json()
    assert res["default"] == three.b.id and res["reason"] == "nearest_in_stock"
    assert [w["name"] for w in res["warehouses"]] == ["A Thoothukudi", "B Tirunelveli", "C Madurai"]
    assert res["warehouses"][1]["free"] == {str(three.fan.id): 4.0} and res["products"][0]["name"] == "Ceiling fan"
    assert client.get(f"/warehouse/suggest.json?pincode=628002&p={three.fan.id}&q=12").get_json()["split"]
    page = client.get("/sales/documents/new?kind=sales_order").get_data(as_text=True)
    assert "Deliver to a different address" in page and "customer-addresses" in page and "628002" in page


def test_the_warehouse_settings_take_pincode_point_and_service_area(three, client):
    client.post("/auth/login", data={"email": three.admin.email, "password": "ClientSecret123"})
    client.post(f"/warehouse/locations/{three.c.id}", data={"pincode": "625002", "latitude": "9.9", "longitude": "78.1",
                                                             "service_area": "628, 629001"})
    _db.session.refresh(three.c)
    assert (three.c.pincode, three.c.latitude, three.c.longitude) == ("625002", 9.9, 78.1)
    assert nearest.service_area(three.tenant.id, "628777").id == three.c.id
    res = client.post(f"/warehouse/locations/{three.b.id}", data={"pincode": "627001", "service_area": "629001"},
                      follow_redirects=True)
    assert "already in C Madurai" in res.get_data(as_text=True)
    page = client.get("/inventory/locations").get_data(as_text=True)
    assert "Always serves these pincodes" in page and "Fallback warehouse" in page
    res = client.post("/inventory/locations", data={"name": "D", "kind": "godown"}, follow_redirects=True)
    assert "pincode" in res.get_data(as_text=True).lower() and StockLocation.query.filter_by(name="D").count() == 0


def test_the_pincode_table_is_built_with_fallback_levels(app, db, tmp_path):
    rows = [
        {"pincode": "628001", "Latitude": "8.76", "Longitude": "78.13", "District": "THOOTHUKUDI", "StateName": "TN"},
        {"pincode": "628001", "Latitude": "8.78", "Longitude": "78.15", "District": "THOOTHUKUDI", "StateName": "TN"},
        {"pincode": "628002", "Latitude": "NA", "Longitude": "", "District": "THOOTHUKUDI", "StateName": "TN"},
        {"pincode": "628999", "Latitude": "0", "Longitude": "0", "District": "NOWHERE", "StateName": "TN"},
        {"pincode": "999001", "Latitude": "", "Longitude": "", "District": "LOST", "StateName": "X"},
        {"pincode": "12", "Latitude": "1", "Longitude": "1"},
    ]
    built = {r["pincode"]: r for r in pincodes.build(rows)}
    assert built["628001"]["level"] == "own" and built["628001"]["lat"] == 8.77
    assert built["628002"]["level"] == "district" and built["628002"]["lat"] == 8.77    # its district's average
    assert built["628999"]["level"] == "area"                                          # 0,0 is wrong: its 628 area's
    assert "999001" not in built and "12" not in built
    path = tmp_path / "geo.csv.gz"
    pincodes.write(list(built.values()), path)
    assert pincodes.load(path) == 3 and PincodeGeo.query.count() == 3
    assert pincodes.load(path) == 0                                                    # already up to date
    assert pincodes.load(tmp_path / "missing.csv.gz") == 0
