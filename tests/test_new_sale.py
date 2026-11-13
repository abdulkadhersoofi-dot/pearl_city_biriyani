"""New sale (app/sell): one form, and where the stock is decides what is made.
One test for every row of the table in app/sell/decide.py."""

from datetime import date
from decimal import Decimal

import pytest

from app.inventory import ledger
from app.inventory import services as inv
from app.invoicing.services import LineInput
from app.models.customer import Customer
from app.models.inventory import AdjustmentReason, LocationKind, ProductSerial, SerialStatus, StockLocation
from app.models.invoice import Invoice
from app.models.invoice_series import DocumentType
from app.models.product import Product
from app.models.sales import SalesDocument, SalesDocumentKind
from app.models.warehouse import DispatchStatus, PincodeGeo
from app.sell import decide

D = Decimal
TODAY = date.today()


@pytest.fixture()
def shop(db, tenant, client_admin, staff):
    """A showroom (the main store) with 3 fans and 1 cooler; a warehouse at
    Madurai with 10 fans and 5 mixers; another at Tirunelveli with 4 mixers.
    Nobody has a geyser."""
    for key in ("inventory", "sales_documents", "warehouse_dispatch"):
        tenant.set_feature(key, True)
    for pin, lat, lng, district in (("625001", 9.9252, 78.1198, "Madurai"), ("627001", 8.7139, 77.7567, "Tirunelveli"),
                                    ("628002", 8.8, 78.15, "Thoothukudi")):
        db.session.add(PincodeGeo(pincode=pin, lat=lat, lng=lng, district=district, state="Tamil Nadu"))
    madurai = StockLocation(tenant_id=tenant.id, name="Madurai warehouse", kind=LocationKind.GODOWN, pincode="625001")
    nellai = StockLocation(tenant_id=tenant.id, name="Tirunelveli warehouse", kind=LocationKind.GODOWN,
                           pincode="627001")
    db.session.add_all([madurai, nellai])

    def item(name, price):
        p = Product(tenant_id=tenant.id, name=name, hsn_or_sac_code="8414", gst_rate=D("18"), unit="PCS",
                    sale_price=D(price), allow_negative_stock=False)
        db.session.add(p)
        return p

    fan, cooler, mixer, geyser = item("Fan", "2500"), item("Cooler", "8000"), item("Mixer", "3000"), item("Geyser", "9000")
    fitting = Product(tenant_id=tenant.id, name="Fitting charge", hsn_or_sac_code="9987", gst_rate=D("18"),
                      unit="NOS", sale_price=D("500"), is_service=True)
    buyer = Customer(tenant_id=tenant.id, name="Ravi Stores", state_code="27", state_name="Maharashtra",
                     address_line1="12 Beach Road", city="Thoothukudi", pincode="628002")
    db.session.add_all([fitting, buyer])
    db.session.commit()
    store = ledger.default_location(tenant.id)
    for loc, product, qty in ((store, fan, 3), (store, cooler, 1), (madurai, fan, 10), (madurai, mixer, 5),
                              (nellai, mixer, 4)):
        inv.create_adjustment(tenant, client_admin, loc.id, AdjustmentReason.OPENING,
                              [inv.StockLineInput(product.id, D(qty), D("1000"))])
    db.session.commit()
    return type("Shop", (), dict(tenant=tenant, admin=client_admin, staff=staff, store=store, madurai=madurai,
                                 nellai=nellai, fan=fan, cooler=cooler, mixer=mixer, geyser=geyser, fitting=fitting,
                                 buyer=buyer))


def line(product, qty, source=decide.AUTO, serials=()):
    return decide.SaleLine(LineInput(description=product.name, hsn_or_sac_code=product.hsn_or_sac_code, qty=D(qty),
                                     rate=D(product.sale_price), discount_percent=D("0"), gst_rate=D("18"),
                                     product_id=product.id, unit=product.unit, serials=list(serials)), source)


def plan_for(s, lines, user=None, **kw):
    user = user or s.admin
    return decide.decide(s.tenant, user, decide.seller_location(s.tenant, user), lines, pincode="628002", **kw)


def header(s, **kw):
    return decide.Header(gstin_id=s.tenant.gstins.first().id, customer_id=s.buyer.id,
                         place_of_supply_state_code="27", on=TODAY, document_type=DocumentType.TAX_INVOICE, **kw)


def kinds(plan):
    return [(p.kind, p.location.name if p.location is not None else None,
             [f"{D(l.qty).normalize():f}" for l in p.lines])
            for p in plan.parts]


def stock(s, product, location):
    return ledger.stock_on_hand(s.tenant.id, [product.id], location.id).get(product.id, D("0"))


# --- the table ------------------------------------------------------------------------------------------

def test_everything_here_is_a_sale_billed_now(db, shop):
    plan = plan_for(shop, [line(shop.fan, 2), line(shop.cooler, 1)])
    assert kinds(plan) == [("invoice", "Main store", ["2", "1"])]
    assert plan.sentence == "This will bill 2 items now." and plan.button == "Bill now"
    result = decide.save(shop.tenant, shop.admin, header(shop), plan)
    db.session.commit()
    assert result.invoice.grand_total == D("15340.00") and result.documents == [] and result.ref is None
    assert stock(shop, shop.fan, shop.store) == 1 and stock(shop, shop.cooler, shop.store) == 0
    assert SalesDocument.query.count() == 0


def test_everything_at_a_warehouse_is_a_sales_order(db, shop):
    # Both warehouses have mixers; Tirunelveli is nearer the customer (Thoothukudi).
    plan = plan_for(shop, [line(shop.mixer, 2)])
    assert kinds(plan) == [("order", "Tirunelveli warehouse", ["2"])]
    assert plan.sentence == "This will order 1 item from Tirunelveli warehouse."
    assert plan.button == "Send order to Tirunelveli warehouse"
    result = decide.save(shop.tenant, shop.admin, header(shop), plan)
    db.session.commit()
    order = result.documents[0]
    assert result.invoice is None and order.kind == SalesDocumentKind.SALES_ORDER
    assert order.dispatch_location_id == shop.nellai.id and order.dispatch_status == DispatchStatus.TO_PICK
    assert stock(shop, shop.mixer, shop.nellai) == 4 and Invoice.query.count() == 0      # held, not yet gone


def test_some_here_some_at_a_warehouse_is_both_as_one_sale(db, shop):
    plan = plan_for(shop, [line(shop.fan, 5), line(shop.mixer, 1)])
    assert kinds(plan) == [("invoice", "Main store", ["3"]), ("order", "Madurai warehouse", ["2", "1"])]
    assert plan.sentence == "This will bill 1 item now from Main store, and order 2 items from Madurai warehouse."
    assert plan.button == "Bill 1 now, order 2"
    assert (plan.notes[0].here, plan.notes[0].here_qty, plan.notes[0].order_qty) == (D("3"), D("3"), D("2"))
    result = decide.save(shop.tenant, shop.admin, header(shop), plan)
    db.session.commit()
    invoice, order = result.invoice, result.documents[0]
    assert invoice.lines[0].qty == D("3") and [l.qty for l in order.lines] == [D("2"), D("1")]
    assert result.ref and invoice.sale_ref == order.sale_ref == result.ref
    assert stock(shop, shop.fan, shop.store) == 0
    found = decide.siblings(shop.tenant.id, result.ref)
    assert [i.id for i in found["invoices"]] == [invoice.id] and [d.id for d in found["documents"]] == [order.id]


def test_items_in_two_warehouses_make_an_order_each(db, shop):
    plan = plan_for(shop, [line(shop.mixer, 8)])
    parts = sorted(kinds(plan))
    assert [p[0] for p in parts] == ["order", "order"]
    assert {p[1] for p in parts} == {"Madurai warehouse", "Tirunelveli warehouse"}
    assert sum(D(p[2][0]) for p in parts) == 8 and plan.button == "Send 2 orders"
    result = decide.save(shop.tenant, shop.admin, header(shop), plan)
    db.session.commit()
    assert len(result.documents) == 2 and len({d.sale_ref for d in result.documents}) == 1


def test_not_in_stock_anywhere_is_an_order_that_waits(db, shop):
    plan = plan_for(shop, [line(shop.geyser, 1)])
    assert kinds(plan) == [("waiting", None, ["1"])]
    assert "waits for stock" in plan.sentence and plan.button == "Take the order"
    assert plan.notes[0].waiting_qty == D("1")
    result = decide.save(shop.tenant, shop.admin, header(shop), plan)
    db.session.commit()
    order = result.documents[0]
    assert order.awaiting_stock and order.dispatch_location_id is None and order.dispatch_status is None
    # Stock arrives at Madurai: the order goes there and is held like any other.
    inv.create_adjustment(shop.tenant, shop.admin, shop.madurai.id, AdjustmentReason.OPENING,
                          [inv.StockLineInput(shop.geyser.id, D("2"), D("5000"))])
    from app.warehouse import dispatch

    dispatch.send_waiting(shop.tenant, order, shop.madurai)
    db.session.commit()
    assert not order.awaiting_stock and order.dispatch_status == DispatchStatus.TO_PICK
    assert dispatch.held(shop.tenant.id, [shop.geyser.id])[(shop.geyser.id, shop.madurai.id)] == 1


def test_only_a_price_makes_a_quotation_and_moves_nothing(db, shop):
    plan = plan_for(shop, [line(shop.fan, 50)], mode=decide.QUOTE)
    assert plan.button == "Save quote" and "Nothing is billed" in plan.sentence
    result = decide.save(shop.tenant, shop.admin, header(shop), plan)
    db.session.commit()
    assert result.documents[0].kind == SalesDocumentKind.QUOTATION and result.invoice is None
    assert stock(shop, shop.fan, shop.store) == 3
    proforma = decide.save(shop.tenant, shop.admin, header(shop), plan_for(shop, [line(shop.fan, 1)],
                                                                          mode=decide.PROFORMA))
    assert proforma.documents[0].kind == SalesDocumentKind.PROFORMA


def test_goods_without_a_bill_make_a_delivery_challan(db, shop):
    from app.models.sales import ChallanPurpose

    plan = plan_for(shop, [line(shop.fan, 1)], mode=decide.CHALLAN)
    assert plan.button == "Send goods"
    result = decide.save(shop.tenant, shop.admin, header(shop, challan_purpose=list(ChallanPurpose)[0]), plan)
    assert result.documents[0].kind == SalesDocumentKind.DELIVERY_CHALLAN


def test_an_emi_sale_is_one_order_never_split(db, shop):
    from app.warehouse.emi import add_financier

    financier = add_financier(shop.tenant, "Bajaj Finance")
    db.session.commit()
    finance = {"is_finance_sale": True, "financier_id": financier.id, "finance_amount": D("2000"),
               "loan_ref": "DO-1", "down_payment": D("950")}
    plan = plan_for(shop, [line(shop.fan, 1)], finance=True)
    assert kinds(plan) == [("order", "Madurai warehouse", ["1"])]        # not billed from the showroom
    assert plan.button == "Take EMI order" and plan.finance
    result = decide.save(shop.tenant, shop.admin, header(shop, finance=finance), plan)
    db.session.commit()
    assert result.documents[0].is_finance_sale and result.invoice is None
    # 8 mixers need two warehouses: an EMI sale can't be split.
    split = plan_for(shop, [line(shop.mixer, 8)], finance=True)
    assert split.problems and "one order from one warehouse" in split.problems[0]
    with pytest.raises(decide.SellError):
        decide.save(shop.tenant, shop.admin, header(shop, finance=finance), split)


# --- the details that keep it safe -----------------------------------------------------------------------

def test_a_line_can_be_pinned_to_here_or_to_a_warehouse(db, shop):
    # The fan is here, but the customer wants it delivered from Madurai.
    plan = plan_for(shop, [line(shop.fan, 1, source=str(shop.madurai.id))])
    assert kinds(plan) == [("order", "Madurai warehouse", ["1"])]
    # Pinned here with too few: refused on save, nothing made.
    short = plan_for(shop, [line(shop.fan, 9, source=decide.HERE)])
    assert kinds(short) == [("invoice", "Main store", ["9"])]
    with pytest.raises(decide.SellError):
        decide.save(shop.tenant, shop.admin, header(shop), short)
    db.session.rollback()
    assert Invoice.query.count() == 0 and SalesDocument.query.count() == 0


def test_a_pinned_line_joins_the_order_already_going_to_that_warehouse(db, shop):
    plan = plan_for(shop, [line(shop.cooler, 1), line(shop.fan, 1, source=str(shop.madurai.id)), line(shop.fan, 9)])
    # 1 cooler and 3 fans billed here; the pinned fan and the 6 left over go as ONE order to Madurai.
    assert kinds(plan) == [("invoice", "Main store", ["1", "3"]), ("order", "Madurai warehouse", ["1", "6"])]
    assert plan.button == "Bill 2 now, order 2"


def test_send_everything_as_one_order(db, shop):
    plan = plan_for(shop, [line(shop.fan, 2), line(shop.mixer, 1)], together=True)
    assert kinds(plan) == [("order", "Madurai warehouse", ["2", "1"])]


def test_services_ride_with_the_invoice_or_the_only_order(db, shop):
    both = plan_for(shop, [line(shop.fan, 1), line(shop.fitting, 1)])
    assert kinds(both) == [("invoice", "Main store", ["1", "1"])]
    order_only = plan_for(shop, [line(shop.mixer, 1), line(shop.fitting, 1)])
    assert kinds(order_only) == [("order", "Tirunelveli warehouse", ["1", "1"])]
    alone = plan_for(shop, [line(shop.fitting, 2)])
    assert kinds(alone) == [("invoice", "Main store", ["2"])]


def test_two_lines_of_one_item_share_what_is_here(db, shop):
    plan = plan_for(shop, [line(shop.fan, 2), line(shop.fan, 2)])
    assert kinds(plan) == [("invoice", "Main store", ["2", "1"]), ("order", "Madurai warehouse", ["1"])]


def test_all_or_nothing_when_one_part_fails(db, shop):
    plan = plan_for(shop, [line(shop.fan, 5)])
    # Someone else takes the warehouse's fans between the plan and the save.
    inv.create_adjustment(shop.tenant, shop.admin, shop.madurai.id, AdjustmentReason.DAMAGE,
                          [inv.StockLineInput(shop.fan.id, D("10"), D("1000"))])
    db.session.commit()
    with pytest.raises(decide.SellError):
        decide.save(shop.tenant, shop.admin, header(shop), plan)
    db.session.rollback()
    assert Invoice.query.count() == 0 and SalesDocument.query.count() == 0
    assert stock(shop, shop.fan, shop.store) == 3


def test_a_serial_numbered_item_billed_here_needs_its_serials(db, shop):
    shop.tenant.set_feature("serial_imei", True)
    phone = Product(tenant_id=shop.tenant.id, name="Phone", hsn_or_sac_code="8517", gst_rate=D("18"), unit="PCS",
                    sale_price=D("15000"), track_serial=True)
    db.session.add(phone)
    db.session.commit()
    inv.create_adjustment(shop.tenant, shop.admin, shop.store.id, AdjustmentReason.OPENING,
                          [inv.StockLineInput(phone.id, D("2"), D("12000"))])
    for no in ("IMEI-1", "IMEI-2"):
        db.session.add(ProductSerial(tenant_id=shop.tenant.id, product_id=phone.id, serial_no=no,
                                     status=SerialStatus.IN_STOCK, location_id=shop.store.id))
    db.session.commit()
    missing = plan_for(shop, [line(phone, 1)])
    assert missing.notes[0].needs_serials == 1 and "scan 1 serial number" in missing.problems[0]
    with pytest.raises(decide.SellError):
        decide.save(shop.tenant, shop.admin, header(shop), missing)
    db.session.rollback()
    wrong = plan_for(shop, [line(phone, 1, serials=["IMEI-9"])])
    with pytest.raises(decide.SellError, match="isn't in your stock"):
        decide.save(shop.tenant, shop.admin, header(shop), wrong)
    db.session.rollback()
    good = plan_for(shop, [line(phone, 1, serials=["IMEI-1"])])
    assert not good.problems
    result = decide.save(shop.tenant, shop.admin, header(shop), good)
    db.session.commit()
    unit = ProductSerial.query.filter_by(serial_no="IMEI-1").one()
    assert unit.status == SerialStatus.SOLD and unit.sold_invoice_id == result.invoice.id


def test_without_stock_in_arfa_everything_is_billed(db, tenant, client_admin):
    buyer = Customer(tenant_id=tenant.id, name="Ravi", state_code="27", state_name="Maharashtra")
    tea = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="0902", gst_rate=D("5"), unit="PCS",
                  sale_price=D("100"))
    db.session.add_all([buyer, tea])
    db.session.commit()
    assert decide.seller_location(tenant, client_admin) is None
    plan = decide.decide(tenant, client_admin, None, [line(tea, 3)])
    assert [(p.kind, p.location) for p in plan.parts] == [("invoice", None)] and plan.button == "Bill now"


# --- the form -------------------------------------------------------------------------------------------

def _login(client, email="admin@acme.example.com", password="ClientSecret123"):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _form(s, rows, **extra):
    data = {"gstin_id": str(s.tenant.gstins.first().id), "document_type": "tax_invoice",
            "customer_id": str(s.buyer.id), "place_of_supply_state_code": "27", "invoice_date": TODAY.isoformat(),
            "line_description[]": [], "line_hsn[]": [], "line_qty[]": [], "line_rate[]": [], "line_discount[]": [],
            "line_gst_rate[]": [], "line_product_id[]": [], "line_unit[]": [], "line_incl[]": [], "line_from[]": [],
            "line_serials[]": []}
    for product, qty, source in rows:
        for key, value in (("line_description[]", product.name if product else ""), ("line_hsn[]", "8414"),
                           ("line_qty[]", str(qty)), ("line_rate[]", str(product.sale_price) if product else "0"),
                           ("line_discount[]", "0"), ("line_gst_rate[]", "18"),
                           ("line_product_id[]", str(product.id) if product else ""), ("line_unit[]", "PCS"),
                           ("line_incl[]", "0"), ("line_from[]", source), ("line_serials[]", "")):
            data[key].append(value)
    data.update(extra)
    return data


def test_the_form_says_what_it_will_do_and_then_does_it(client, db, shop):
    _login(client)
    page = client.get("/sell/new").get_data(as_text=True)
    assert "New sale" in page and "Selling from" in page and 'name="line_from[]"' in page
    assert "Madurai warehouse" in page and "data-sale-sentence" in page and "js/new_sale.js" in page
    # A blank row in the middle must not shift "from" onto the wrong line.
    rows = [(shop.fan, 5, "auto"), (None, 1, "here"), (shop.mixer, 1, "auto")]
    plan = client.post("/sell/new/plan.json", data=_form(shop, rows)).get_json()
    assert plan["ok"] and plan["button"] == "Bill 1 now, order 2"
    assert plan["sentence"] == "This will bill 1 item now from Main store, and order 2 items from Madurai warehouse."
    assert plan["lines"][0] == {"here": "3", "elsewhere": [{"name": "Madurai warehouse", "free": "10"}],
                                "here_qty": "3", "order_qty": "2", "waiting_qty": "0", "needs_serials": 0}
    assert Invoice.query.count() == 0 and SalesDocument.query.count() == 0           # asking saves nothing
    resp = client.post("/sell/new", data=_form(shop, rows))
    assert resp.status_code == 302 and "/sell/sale/" in resp.headers["Location"]
    done = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Billed now" in done and "Ordered from Madurai warehouse" in done
    invoice = Invoice.query.one()
    assert f"/sell/invoice/{invoice.id}.pdf" in done
    assert client.get(f"/sell/invoice/{invoice.id}.pdf").status_code == 200
    # The invoice and the order each point back to the whole sale.
    assert "/sell/sale/" in client.get(f"/invoices/{invoice.id}").get_data(as_text=True)
    order = SalesDocument.query.one()
    assert "see the whole sale" in client.get(f"/sales/documents/{order.id}").get_data(as_text=True)


def test_a_single_document_opens_itself(client, db, shop):
    _login(client)
    resp = client.post("/sell/new", data=_form(shop, [(shop.fan, 1, "auto")]))
    assert resp.headers["Location"].endswith(f"/invoices/{Invoice.query.one().id}")
    resp = client.post("/sell/new", data=_form(shop, [(shop.mixer, 1, "auto")]))
    assert resp.headers["Location"].endswith(f"/sales/documents/{SalesDocument.query.one().id}")


def test_a_refused_sale_keeps_the_form_and_saves_nothing(client, db, shop):
    _login(client)
    resp = client.post("/sell/new", data=_form(shop, [(shop.fan, 9, "here")]))
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "New sale" in page and Invoice.query.count() == 0
    bad = client.post("/sell/new/plan.json", data=_form(shop, [])).get_json()
    assert not bad["ok"] and bad["problems"]


def test_a_waiting_order_is_sent_to_a_warehouse_from_its_page(client, db, shop):
    _login(client)
    client.post("/sell/new", data=_form(shop, [(shop.geyser, 1, "auto")]))
    order = SalesDocument.query.one()
    page = client.get(f"/sales/documents/{order.id}").get_data(as_text=True)
    assert "Waiting for stock" in page and "Send to warehouse" in page
    # Still nothing there: refused, and it keeps waiting.
    client.post(f"/sales/documents/{order.id}/warehouse", data={"dispatch_location_id": shop.madurai.id})
    db.session.refresh(order)
    assert order.awaiting_stock
    inv.create_adjustment(shop.tenant, shop.admin, shop.madurai.id, AdjustmentReason.OPENING,
                          [inv.StockLineInput(shop.geyser.id, D("1"), D("5000"))])
    db.session.commit()
    client.post(f"/sales/documents/{order.id}/warehouse", data={"dispatch_location_id": shop.madurai.id})
    db.session.refresh(order)
    assert not order.awaiting_stock and order.dispatch_location_id == shop.madurai.id


# --- the showroom (a branch login) -----------------------------------------------------------------------

def test_a_branch_bills_its_own_stock_and_orders_the_rest(client, db, shop):
    showroom = ledger.branch_location(shop.tenant.id, shop.staff)
    inv.create_adjustment(shop.tenant, shop.admin, showroom.id, AdjustmentReason.OPENING,
                          [inv.StockLineInput(shop.fan.id, D("1"), D("1000"))])
    db.session.commit()
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    page = client.get("/sell/new").get_data(as_text=True)
    assert showroom.name in page and 'name="selling_from"' not in page and 'name="sale_mode"' not in page
    rows = [(shop.fan, 3, "auto")]
    plan = client.post("/sell/new/plan.json", data=_form(shop, rows)).get_json()
    assert plan["here"] == showroom.name and plan["button"] == "Bill 1 now, order 1"
    # A branch can't choose another place to sell from, or make a quotation.
    resp = client.post("/sell/new", data=_form(shop, rows, selling_from=str(shop.store.id), sale_mode="quote"))
    assert "/sell/sale/" in resp.headers["Location"]
    invoice, order = Invoice.query.one(), SalesDocument.query.one()
    assert invoice.lines[0].qty == D("1") and order.lines[0].qty == D("2") and order.kind == SalesDocumentKind.SALES_ORDER
    assert stock(shop, shop.fan, showroom) == 0 and stock(shop, shop.fan, shop.store) == 3
    done = client.get(resp.headers["Location"])
    assert done.status_code == 200 and client.get(f"/sell/invoice/{invoice.id}.pdf").status_code == 200
    assert client.get(f"/invoices/{invoice.id}").status_code in (302, 403)       # the owner's page stays the owner's


def test_a_branch_sees_only_its_own_sales(client, db, shop):
    _login(client)
    resp = client.post("/sell/new", data=_form(shop, [(shop.fan, 5, "auto")]))
    ref = resp.headers["Location"].rsplit("/", 1)[1]
    invoice = Invoice.query.one()
    client.get("/auth/logout")
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    assert client.get(f"/sell/sale/{ref}").status_code == 404
    assert client.get(f"/sell/invoice/{invoice.id}.pdf").status_code == 404


def test_a_branch_without_warehouse_orders_keeps_to_its_counter(client, db, tenant, staff):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    assert client.get("/sell/new").status_code == 403


def test_another_business_never_sees_a_sale(client, db, shop):
    from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
    from app.models.user import User, UserRole

    _login(client)
    resp = client.post("/sell/new", data=_form(shop, [(shop.fan, 5, "auto")]))
    ref = resp.headers["Location"].rsplit("/", 1)[1]
    invoice = Invoice.query.one()
    client.get("/auth/logout")
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other.id, gstin="29AAAAA0000A1Z5", state_code="29", state_name="Karnataka",
                         is_primary=True))
    owner = User(tenant_id=other.id, name="Other", email="owner@other.example.com", role=UserRole.CLIENT_ADMIN,
                 must_change_password=False)
    owner.set_password("OtherSecret123")
    db.session.add(owner)
    db.session.commit()
    _login(client, "owner@other.example.com", "OtherSecret123")
    assert client.get(f"/sell/sale/{ref}").status_code == 404
    assert client.get(f"/sell/invoice/{invoice.id}.pdf").status_code == 404
