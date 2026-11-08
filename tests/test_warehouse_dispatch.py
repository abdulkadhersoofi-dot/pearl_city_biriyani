"""Warehouse dispatch, stage 1 (app/warehouse/dispatch.py): a sales order
names the warehouse it ships from, what it takes is held there, and a
warehouse can't promise more than it has free. A serial is sold only from
where it is. With the module off nothing changes."""

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db as _db
from app.inventory import hooks, ledger
from app.invoicing.services import LineInput
from app.models.customer import Customer
from app.models.inventory import LocationKind, ProductSerial, SerialStatus, StockLocation
from app.models.invoice_series import DocumentType
from app.models.product import Product
from app.models.sales import SalesDocument, SalesDocumentKind
from app.models.warehouse import DispatchStatus, WarehouseLogin, WarehouseNotice
from app.purchases import services as purchases
from app.sales import services as ss
from app.warehouse import dispatch

D = Decimal
TODAY = date.today()


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _on(tenant, *keys):
    for key in keys:
        tenant.set_feature(key, True)
    _db.session.commit()


@pytest.fixture()
def wh(db, tenant, client_admin):
    """A business with warehouse dispatch on: two godowns, an AC with serials
    (5 at Warehouse A, 2 at B), a customer."""
    _on(tenant, "inventory", "purchases", "serial_imei", "sales_documents", "warehouse_dispatch")
    a = StockLocation(tenant_id=tenant.id, name="Warehouse A", kind=LocationKind.GODOWN, address="Plot 4, Thoothukudi 628001")
    b = StockLocation(tenant_id=tenant.id, name="Warehouse B", kind=LocationKind.GODOWN)
    db.session.add_all([a, b])
    ac = Product(tenant_id=tenant.id, name="LG AC 1.5T", hsn_or_sac_code="8415", gst_rate=D("28"), unit="PCS",
                 sale_price=D("40000"), track_serial=True, model_no="AR18TY3QBWK")
    fridge = Product(tenant_id=tenant.id, name="Fridge 260L", hsn_or_sac_code="8418", gst_rate=D("18"), unit="PCS",
                     sale_price=D("25000"), track_serial=True)
    buyer = Customer(tenant_id=tenant.id, name="Ravi Stores", state_code="27", state_name="Maharashtra")
    db.session.add_all([ac, fridge, buyer])
    db.session.commit()
    buy(tenant, client_admin, a, ac, *[f"ACA{i}" for i in range(1, 6)])
    buy(tenant, client_admin, b, ac, "ACB1", "ACB2")
    buy(tenant, client_admin, a, fridge, "FRA1")
    return type("WH", (), {"a": a, "b": b, "ac": ac, "fridge": fridge, "buyer": buyer, "tenant": tenant,
                           "admin": client_admin})


def buy(tenant, user, location, product, *serials):
    from app.models.purchase import Supplier

    supplier = Supplier.query.filter_by(tenant_id=tenant.id).first()
    if supplier is None:
        supplier = Supplier(tenant_id=tenant.id, name="LG Distributor", state_code="27", state_name="Maharashtra")
        _db.session.add(supplier)
        _db.session.flush()
    purchases.create_purchase_bill(tenant, user, purchases.PurchaseInput(
        supplier_id=supplier.id, gstin_id=tenant.gstins.first().id, supplier_invoice_no=f"B-{serials[0]}",
        supplier_invoice_date=TODAY, location_id=location.id,
        lines=[purchases.PurchaseLineInput(product_id=product.id, description=product.name, hsn_or_sac_code="8415",
                                           qty=D(len(serials)), unit="PCS", rate=D("30000"), gst_rate=D("28"),
                                           serials=list(serials))]))
    _db.session.commit()


def line(product, qty="1"):
    return LineInput(description=product.name, hsn_or_sac_code=product.hsn_or_sac_code, qty=D(qty),
                     rate=product.sale_price, discount_percent=D("0"), gst_rate=product.gst_rate,
                     product_id=product.id, unit="PCS")


def order(w, user=None, location=None, lines=None, commit=True):
    doc = ss.create_sales_document(w.tenant, user or w.admin, ss.SalesDocInput(
        kind=SalesDocumentKind.SALES_ORDER, gstin_id=w.tenant.gstins.first().id, customer_id=w.buyer.id,
        place_of_supply_state_code="27", doc_date=TODAY, invoice_document_type=DocumentType.TAX_INVOICE,
        lines=lines or [line(w.ac, "2")], dispatch_location_id=(location or w.a).id if location is not False else None))
    if commit:
        _db.session.commit()
    return doc


# --- the warehouse on the order -------------------------------------------------------------------

def test_an_order_names_its_warehouse_and_is_held_there(wh):
    doc = order(wh)
    assert doc.dispatch_location_id == wh.a.id and doc.dispatch_status == DispatchStatus.TO_PICK
    free = {r["location"].name: r["items"][wh.ac.id] for r in dispatch.free_by_warehouse(wh.tenant.id, [wh.ac.id])}
    assert free["Warehouse A"] == {"stock": D("5"), "held": D("2"), "free": D("3")}
    assert free["Warehouse B"]["free"] == D("2")
    notice = WarehouseNotice.query.one()
    assert notice.kind == "new_order" and notice.location_id == wh.a.id and not notice.silent
    assert notice.title == f"New order {doc.number}" and "2 items" in notice.body


def test_an_order_needs_one_of_this_businesss_warehouses(wh, db, tenant, super_admin):
    with pytest.raises(ss.SalesError, match="Pick the warehouse"):
        order(wh, location=False)
    store = ledger.default_location(tenant.id)
    with pytest.raises(ss.SalesError, match="active warehouses"):
        order(wh, location=store)                       # a store, not a godown
    wh.b.is_active = False
    db.session.commit()
    with pytest.raises(ss.SalesError, match="active warehouses"):
        order(wh, location=wh.b)
    from app.models.tenant import BillingCycle, RegistrationType, Tenant

    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    theirs = StockLocation(tenant_id=other.id, name="Their godown", kind=LocationKind.GODOWN)
    db.session.add(theirs)
    db.session.commit()
    with pytest.raises(ss.SalesError, match="active warehouses"):
        order(wh, location=theirs)
    assert SalesDocument.query.count() == 0


def test_a_warehouse_cant_promise_more_than_it_has_free(wh):
    order(wh, lines=[line(wh.ac, "3")])                                 # 3 of A's 5 held
    with pytest.raises(ss.SalesError, match=r"Only 2 of LG AC 1.5T free at Warehouse A \(5 in stock, 3 held"):
        order(wh, lines=[line(wh.ac, "3")])
    _db.session.rollback()
    with pytest.raises(ss.SalesError, match="Only 2 of LG AC 1.5T free at Warehouse B"):
        order(wh, location=wh.b, lines=[line(wh.ac, "3")])              # B's stock is B's
    _db.session.rollback()
    order(wh, lines=[line(wh.ac, "2")])                                 # exactly what's left is fine
    assert SalesDocument.query.count() == 2


def test_cancelling_frees_the_hold_and_moving_carries_it(wh):
    first = order(wh, lines=[line(wh.ac, "4")])
    ss.cancel_document(first, wh.admin, "customer changed mind")
    _db.session.commit()
    assert dispatch.held(wh.tenant.id, [wh.ac.id]) == {}
    assert WarehouseNotice.query.filter_by(kind="cancelled").count() == 1
    second = order(wh, lines=[line(wh.ac, "2")])
    old = dispatch.change_warehouse(wh.tenant, second, wh.b)
    _db.session.commit()
    assert old.id == wh.a.id and second.dispatch_location_id == wh.b.id
    assert dispatch.held(wh.tenant.id, [wh.ac.id]) == {(wh.ac.id, wh.b.id): D("2")}
    kinds = [(n.kind, n.location_id) for n in WarehouseNotice.query.order_by(WarehouseNotice.id)][-2:]
    assert kinds == [("moved", wh.a.id), ("new_order", wh.b.id)]
    with pytest.raises(dispatch.DispatchError, match=r"Only 0 .* \(2 in stock, 2 held for other orders\)"):
        dispatch.change_warehouse(wh.tenant, order(wh, lines=[line(wh.ac, "1")]), wh.b)


def test_a_claimed_order_keeps_its_warehouse(wh):
    doc = order(wh)
    picker = WarehouseLogin(tenant_id=wh.tenant.id, name="Ravi", login_id="ravi", password_hash="x")
    picker.locations = [wh.a]
    _db.session.add(picker)
    _db.session.commit()
    dispatch.claim(doc, picker)
    with pytest.raises(dispatch.DispatchError, match="already taken"):
        dispatch.change_warehouse(wh.tenant, doc, wh.b)


def test_the_branchs_usual_warehouse_is_the_default(wh, db, staff):
    home = ledger.location_for_user(wh.tenant.id, staff)
    assert dispatch.default_warehouse(wh.tenant.id, staff) is None      # two warehouses, none chosen
    home.default_dispatch_location_id = wh.b.id
    db.session.commit()
    assert dispatch.default_warehouse(wh.tenant.id, staff).id == wh.b.id
    wh.b.is_active = False
    db.session.commit()
    assert dispatch.default_warehouse(wh.tenant.id, staff).id == wh.a.id  # the only one left


# --- the website ------------------------------------------------------------------------------------

def _form(w, **extra):
    data = {"kind": "sales_order", "gstin_id": w.tenant.gstins.first().id, "document_type": "tax_invoice",
            "customer_id": str(w.buyer.id), "place_of_supply_state_code": "27", "invoice_date": TODAY.isoformat(),
            "line_description[]": [w.ac.name], "line_hsn[]": ["8415"], "line_qty[]": ["2"], "line_rate[]": ["40000"],
            "line_discount[]": ["0"], "line_gst_rate[]": ["28"], "line_product_id[]": [str(w.ac.id)],
            "line_unit[]": ["PCS"]}
    data.update(extra)
    return data


def test_a_branch_salesperson_makes_an_order_and_picks_the_warehouse(wh, client, staff):
    _login(client, staff.email, "CashierSecret123")
    page = client.get("/sales/documents/new?kind=sales_order").get_data(as_text=True)
    assert 'name="dispatch_location_id"' in page and "Warehouse A" in page and "Warehouse B" in page
    free = client.get(f"/warehouse/free.json?p={wh.ac.id}").get_json()
    assert {w["name"]: w["free"][str(wh.ac.id)] for w in free["warehouses"]} == {"Warehouse A": 5.0, "Warehouse B": 2.0}
    res = client.post("/sales/documents/new", data=_form(wh, dispatch_location_id=str(wh.b.id)), follow_redirects=True)
    assert "sent to Warehouse B" in res.get_data(as_text=True)
    doc = SalesDocument.query.one()
    assert doc.created_by_id == staff.id and doc.dispatch_location_id == wh.b.id
    assert "Waiting at Warehouse B" in client.get(f"/sales/documents/{doc.id}").get_data(as_text=True)
    page = client.post("/sales/documents/new", data=_form(wh, dispatch_location_id=str(wh.b.id)),
                       follow_redirects=True).get_data(as_text=True)
    assert "Only 0 of LG AC 1.5T free at Warehouse B" in page
    assert client.get("/sales/documents/new?kind=quotation").status_code == 403   # orders only, for a branch
    assert client.get("/sales/documents?kind=quotation").status_code == 403


def test_a_branch_sees_only_its_own_orders(wh, client, staff, db):
    mine = order(wh, user=staff)
    theirs = order(wh)
    _login(client, staff.email, "CashierSecret123")
    page = client.get("/sales/documents?kind=sales_order").get_data(as_text=True)
    assert mine.number in page and theirs.number not in page
    assert client.get(f"/sales/documents/{theirs.id}").status_code == 404
    res = client.post(f"/sales/documents/{mine.id}/warehouse", data={"dispatch_location_id": wh.b.id},
                      follow_redirects=True)
    assert "now ships from Warehouse B" in res.get_data(as_text=True)


def test_with_the_module_off_a_sales_order_is_as_before(db, tenant, client_admin, client, staff):
    _on(tenant, "sales_documents", "inventory", "serial_imei")
    buyer = Customer(tenant_id=tenant.id, name="Ravi Stores", state_code="27", state_name="Maharashtra")
    db.session.add(buyer)
    db.session.commit()
    doc = ss.create_sales_document(tenant, client_admin, ss.SalesDocInput(
        kind=SalesDocumentKind.SALES_ORDER, gstin_id=tenant.gstins.first().id, customer_id=buyer.id,
        place_of_supply_state_code="27", doc_date=TODAY, invoice_document_type=DocumentType.TAX_INVOICE,
        lines=[LineInput(description="Rack", hsn_or_sac_code="9403", qty=D("2"), rate=D("100"),
                         discount_percent=D("0"), gst_rate=D("18"))]))
    db.session.commit()
    assert doc.dispatch_location_id is None and doc.dispatch_status is None and WarehouseNotice.query.count() == 0
    _login(client, client_admin.email, "ClientSecret123")
    assert 'name="dispatch_location_id"' not in client.get("/sales/documents/new?kind=sales_order").get_data(as_text=True)
    assert client.get("/warehouse/").status_code in (302, 403, 404)
    client.get("/auth/logout")
    _login(client, staff.email, "CashierSecret123")
    assert client.get("/sales/documents?kind=sales_order").status_code == 403


# --- a serial is sold from where it is -------------------------------------------------------------

def test_a_serial_at_another_location_cant_be_sold_here(wh):
    unit = ProductSerial.query.filter_by(serial_no="ACB1").one()
    sale = hooks.SaleLine(wh.ac.id, 1, serials=["ACB1"])
    with pytest.raises(ledger.StockError, match="ACB1 is at Warehouse B, not here"):
        hooks._check_serials(wh.tenant.id, wh.ac, sale, wh.a.id)
    hooks._check_serials(wh.tenant.id, wh.ac, sale, wh.b.id)            # where it is: fine
    unit.location_id = None
    _db.session.commit()
    hooks._check_serials(wh.tenant.id, wh.ac, sale, wh.a.id)            # a unit with no place recorded passes
    unit.status = SerialStatus.PICKED
    with pytest.raises(ledger.StockError, match="held for a sales order"):
        hooks._check_serials(wh.tenant.id, wh.ac, sale, wh.b.id)


def test_model_no_on_the_item_form_and_in_the_spreadsheet(wh, client):
    from app.bulk.specs import PRODUCT_COLUMNS

    assert "model_no" in [c.key for c in PRODUCT_COLUMNS]
    _login(client, wh.admin.email, "ClientSecret123")
    page = client.get(f"/products/{wh.ac.id}/edit").get_data(as_text=True)
    assert 'name="model_no"' in page and "AR18TY3QBWK" in page


def test_the_warehouse_settings_on_stock_locations(wh, client, staff, db):
    _login(client, wh.admin.email, "ClientSecret123")
    page = client.get("/inventory/locations").get_data(as_text=True)
    assert "Warehouse computer" in page and "additional place of business" in page
    client.post(f"/warehouse/locations/{wh.a.id}", data={"computer_user_id": staff.id, "address_registered": "1"})
    db.session.refresh(wh.a)
    assert wh.a.branch_user_id == staff.id and wh.a.address_registered
    from app.warehouse.routes import computer_location

    assert computer_location(staff).id == wh.a.id
    store = ledger.default_location(wh.tenant.id)
    client.post(f"/warehouse/locations/{store.id}", data={"default_dispatch_location_id": wh.b.id})
    db.session.refresh(store)
    assert store.default_dispatch_location_id == wh.b.id
