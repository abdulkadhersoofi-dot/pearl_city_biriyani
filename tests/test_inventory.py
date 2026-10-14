"""Phase 3 inventory: the append-only stock ledger, how every sale/return/
void posts to it, the no-negative-stock rule (only with Inventory on),
batches (FEFO, expired stock blocked), serial numbers, adjustments,
transfers with a Delivery Challan, the Stock screens and their flags."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from flask import g

from app.inventory import hooks, ledger, reports, services
from app.invoicing.services import InvoiceInput, LineInput, create_invoice, void_invoice
from app.models.audit_log import AuditLog
from app.models.stock import BranchStockAllocation
from app.models.inventory import (
    AdjustmentReason,
    LocationKind,
    MovementType,
    ProductBatch,
    ProductSerial,
    SerialStatus,
    StockAdjustment,
    StockLocation,
    StockMovement,
    StockTransfer,
)
from app.models.invoice_series import DocumentType
from app.models.pos_bill import PaymentMode
from app.models.product import Product
from app.models.user import BranchType, User, UserRole
from app.pos.returns import create_return, void_return
from app.pos.services import CartInput, CartLineInput, POSValidationError, checkout, refund_bill, void_bill

D = Decimal


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _on(db, tenant, *keys):
    for key in keys:
        tenant.set_feature(key, True)
    db.session.commit()


def _product(db, tenant, name="Soap", **kw):
    p = Product(tenant_id=tenant.id, name=name, hsn_or_sac_code="3401", gst_rate=D("18"), unit="PCS",
                sale_price=D("50"), **kw)
    db.session.add(p)
    db.session.commit()
    return p


def _line(product, qty="1", serials=()):
    return CartLineInput(description=product.name, hsn_or_sac_code=product.hsn_or_sac_code, qty=D(qty),
                         rate=product.sale_price, gst_rate=product.gst_rate, product_id=product.id, unit=product.unit,
                         serials=tuple(serials))


def _sell(tenant, user, *lines):
    return checkout(tenant, user, CartInput(lines=list(lines)), PaymentMode.CASH)


def _opening(tenant, user, product, qty, cost="20", **line_kw):
    loc = ledger.default_location(tenant.id)
    adj = services.create_adjustment(tenant, user, loc.id, AdjustmentReason.OPENING,
                                     [services.StockLineInput(product.id, D(qty), D(cost), **line_kw)])
    from app.extensions import db

    db.session.commit()
    return adj


def _on_hand(tenant, product, location_id=None):
    return ledger.stock_on_hand(tenant.id, [product.id], location_id).get(product.id, D("0"))


@pytest.fixture()
def soap(db, tenant):
    return _product(db, tenant)


# --- ledger posting for every client (flag on or off) ---------------------------------------

def test_pos_sale_posts_to_ledger_even_with_inventory_off(db, tenant, client_admin, soap):
    assert not tenant.has_feature("inventory")
    bill = _sell(tenant, client_admin, _line(soap, "3"))
    moves = StockMovement.query.filter_by(source_type="pos_bill", source_id=bill.id).all()
    assert [(m.movement_type, m.qty) for m in moves] == [(MovementType.SALE, D("-3.000"))]
    assert _on_hand(tenant, soap) == D("-3")


def test_selling_below_zero_is_allowed_with_inventory_off_even_if_item_says_stop(db, tenant, client_admin, soap):
    soap.allow_negative_stock = False
    db.session.commit()
    _sell(tenant, client_admin, _line(soap, "2"))  # no error: the module is off - zero behaviour change
    assert _on_hand(tenant, soap) == D("-2")


def test_negative_stock_blocked_only_with_inventory_on_and_item_set_to_stop(db, tenant, client_admin, soap):
    _on(db, tenant, "inventory")
    soap.allow_negative_stock = False
    db.session.commit()
    _opening(tenant, client_admin, soap, "2")
    with pytest.raises(POSValidationError, match="Only 2"):
        _sell(tenant, client_admin, _line(soap, "3"))
    db.session.rollback()
    _sell(tenant, client_admin, _line(soap, "2"))
    assert _on_hand(tenant, soap) == D("0")
    # An item left on "allow negative" keeps selling past zero.
    soap.allow_negative_stock = True
    db.session.commit()
    _sell(tenant, client_admin, _line(soap, "1"))
    assert _on_hand(tenant, soap) == D("-1")


def test_services_and_variant_parents_never_move_stock(db, tenant, client_admin):
    service = _product(db, tenant, "Delivery", is_service=True)
    _sell(tenant, client_admin, _line(service, "1"))
    assert StockMovement.query.filter_by(product_id=service.id).count() == 0


def test_void_and_full_refund_put_stock_back(db, tenant, client_admin, soap):
    bill = _sell(tenant, client_admin, _line(soap, "4"))
    void_bill(bill, client_admin, "wrong item")
    db.session.commit()
    assert _on_hand(tenant, soap) == D("0")
    # Reversal entries point at what they reverse; nothing is deleted.
    moves = StockMovement.query.filter_by(source_type="pos_bill", source_id=bill.id).order_by(StockMovement.id).all()
    assert len(moves) == 2 and moves[1].reversal_of_id == moves[0].id

    bill2 = _sell(tenant, client_admin, _line(soap, "2"))
    refund_bill(bill2, client_admin, bill2.grand_total, "customer unhappy")
    db.session.commit()
    assert _on_hand(tenant, soap) == D("0")


def test_reverse_source_is_idempotent(db, tenant, client_admin, soap):
    bill = _sell(tenant, client_admin, _line(soap, "1"))
    assert ledger.reverse_source(tenant.id, "pos_bill", bill.id, client_admin.id, "x") == 1
    assert ledger.reverse_source(tenant.id, "pos_bill", bill.id, client_admin.id, "x") == 0
    assert _on_hand(tenant, soap) == D("0")


def test_item_return_restores_stock_and_voiding_the_return_takes_it_out_again(db, tenant, client_admin, soap):
    bill = _sell(tenant, client_admin, _line(soap, "5"))
    ret = create_return(bill, client_admin, {bill.lines[0].id: D("2")}, "damaged", PaymentMode.CASH)
    db.session.commit()
    assert _on_hand(tenant, soap) == D("-3")
    void_return(ret, client_admin, "entered by mistake")
    db.session.commit()
    assert _on_hand(tenant, soap) == D("-5")


def test_branch_counter_sells_from_its_own_location(db, tenant, staff, client_admin, soap):
    _sell(tenant, staff, _line(soap, "2"))
    branch_loc = StockLocation.query.filter_by(tenant_id=tenant.id, branch_user_id=staff.id).one()
    assert branch_loc.kind == LocationKind.BRANCH
    assert _on_hand(tenant, soap, branch_loc.id) == D("-2")
    assert _on_hand(tenant, soap, ledger.default_location(tenant.id).id) == D("0")


# --- invoices, branch invoices, and the documented branch-void gap ---------------------------

@pytest.fixture()
def owned_branch(db, tenant):
    u = User(tenant_id=tenant.id, name="Anna Nagar", email="an@acme.example.com", role=UserRole.STAFF,
             branch_type=BranchType.OWNED, must_change_password=False)
    u.set_password("BranchSecret123")
    db.session.add(u)
    db.session.commit()
    return u


def _invoice(tenant, product, qty, branch_user_id=None, customer_id=None):
    gstin = tenant.gstins.first()
    return InvoiceInput(
        gstin_id=gstin.id, document_type=DocumentType.TAX_INVOICE, place_of_supply_state_code=gstin.state_code,
        invoice_date=date.today(), notes=None, customer_id=customer_id, branch_user_id=branch_user_id,
        lines=[LineInput(description=product.name, hsn_or_sac_code=product.hsn_or_sac_code, qty=D(qty),
                         rate=D("100"), discount_percent=D("0"), gst_rate=product.gst_rate, product_id=product.id)],
    )


def test_branch_invoice_transfers_main_store_to_branch_and_void_reverses_both(db, tenant, client_admin, owned_branch, soap):
    invoice = create_invoice(tenant, client_admin, _invoice(tenant, soap, "10", branch_user_id=owned_branch.id))
    db.session.commit()
    store = ledger.default_location(tenant.id)
    branch = ledger.branch_location(tenant.id, owned_branch)
    assert _on_hand(tenant, soap, store.id) == D("-10")
    assert _on_hand(tenant, soap, branch.id) == D("10")
    # The daily branch counter is fed exactly as before.
    assert sum(a.qty for a in BranchStockAllocation.query.filter_by(branch_user_id=owned_branch.id)) == D("10")

    void_invoice(invoice, client_admin, "sent to wrong branch")
    db.session.commit()
    assert _on_hand(tenant, soap, store.id) == D("0")
    assert _on_hand(tenant, soap, branch.id) == D("0")
    # ...and the branch's daily allocation is taken back too (the gap).
    assert sum(a.qty for a in BranchStockAllocation.query.filter_by(branch_user_id=owned_branch.id)) == D("0")


def test_customer_invoice_sells_from_main_store_and_void_restores(db, tenant, client_admin, soap):
    from app.models.customer import Customer

    c = Customer(tenant_id=tenant.id, name="Ravi", state_code="27", state_name="Maharashtra")
    db.session.add(c)
    db.session.commit()
    invoice = create_invoice(tenant, client_admin, _invoice(tenant, soap, "3", customer_id=c.id))
    db.session.commit()
    assert _on_hand(tenant, soap) == D("-3")
    void_invoice(invoice, client_admin, "duplicate")
    db.session.commit()
    assert _on_hand(tenant, soap) == D("0")


# --- weighted average cost ------------------------------------------------------------------

def test_weighted_average_cost_moves_with_receipts(db, tenant, client_admin, soap):
    _opening(tenant, client_admin, soap, "10", cost="20")
    assert soap.cost_price == D("20.00")
    _opening(tenant, client_admin, soap, "10", cost="30")
    assert soap.cost_price == D("25.00")  # (10 x 20 + 10 x 30) / 20


# --- batches: FEFO, expired stock blocked ------------------------------------------------------

def test_fefo_sells_earliest_expiry_first_and_blocks_expired(db, tenant, client_admin):
    _on(db, tenant, "inventory", "batch_expiry")
    med = _product(db, tenant, "Paracetamol", track_batch=True)
    today = date.today()
    _opening(tenant, client_admin, med, "5", batch_no="LATE", expiry_date=today + timedelta(days=200))
    _opening(tenant, client_admin, med, "3", batch_no="SOON", expiry_date=today + timedelta(days=20))
    _opening(tenant, client_admin, med, "4", batch_no="OLD", expiry_date=today - timedelta(days=1))
    db.session.commit()

    bill = _sell(tenant, client_admin, _line(med, "4"))
    moves = StockMovement.query.filter_by(source_type="pos_bill", source_id=bill.id).order_by(StockMovement.id).all()
    taken = {m.batch.batch_no: -m.qty for m in moves}
    assert taken == {"SOON": D("3"), "LATE": D("1")}  # the expired batch is skipped

    # 4 left in date (LATE), 4 expired: asking for 5 is refused.
    with pytest.raises(POSValidationError, match="expired"):
        _sell(tenant, client_admin, _line(med, "5"))
    db.session.rollback()

    expired, soon = reports.expiry_report(tenant.id, 30)
    assert [r.batch.batch_no for r in expired] == ["OLD"]


def test_batches_ignored_without_the_flag(db, tenant, client_admin):
    _on(db, tenant, "inventory")
    med = _product(db, tenant, "Cough syrup", track_batch=True)
    _opening(tenant, client_admin, med, "2", batch_no="B1", expiry_date=date.today() - timedelta(days=5))
    assert ProductBatch.query.count() == 0  # batch_expiry off: no batches recorded
    _sell(tenant, client_admin, _line(med, "1"))  # and nothing blocks the sale


# --- serial numbers ---------------------------------------------------------------------------

def _serial_stock(db, tenant, user, product, *serials):
    from app.purchases import services as purchases
    from app.models.purchase import Supplier

    supplier = Supplier(tenant_id=tenant.id, name="Phone Distributor", state_code="27", state_name="Maharashtra")
    db.session.add(supplier)
    db.session.flush()
    bill = purchases.create_purchase_bill(tenant, user, purchases.PurchaseInput(
        supplier_id=supplier.id, gstin_id=tenant.gstins.first().id, supplier_invoice_no=f"INV-{serials[0]}",
        supplier_invoice_date=date.today(), location_id=ledger.default_location(tenant.id).id,
        lines=[purchases.PurchaseLineInput(product_id=product.id, description=product.name, hsn_or_sac_code="8517",
                                           qty=D(len(serials)), unit="PCS", rate=D("10000"), gst_rate=D("18"),
                                           serials=list(serials))],
    ))
    db.session.commit()
    return bill


def test_serial_sale_needs_a_valid_in_stock_serial_per_unit(db, tenant, client_admin):
    _on(db, tenant, "inventory", "purchases", "serial_imei")
    phone = _product(db, tenant, "Phone X", track_serial=True, warranty_months=12)
    _serial_stock(db, tenant, client_admin, phone, "IMEI1", "IMEI2")
    with pytest.raises(POSValidationError, match="one serial"):
        _sell(tenant, client_admin, _line(phone, "1"))
    db.session.rollback()
    with pytest.raises(POSValidationError, match="isn't in your stock"):
        _sell(tenant, client_admin, _line(phone, "1", serials=["NOPE"]))
    db.session.rollback()

    bill = _sell(tenant, client_admin, _line(phone, "1", serials=["IMEI2"]))
    db.session.commit()
    unit = ProductSerial.query.filter_by(serial_no="IMEI2").one()
    assert unit.status == SerialStatus.SOLD and unit.sold_pos_bill_id == bill.id
    assert unit.warranty_end is not None
    with pytest.raises(POSValidationError, match="already sold"):
        _sell(tenant, client_admin, _line(phone, "1", serials=["IMEI2"]))
    db.session.rollback()

    void_bill(db.session.get(type(bill), bill.id), client_admin, "mistake")
    db.session.commit()
    assert ProductSerial.query.filter_by(serial_no="IMEI2").one().status == SerialStatus.IN_STOCK


# --- adjustments + transfers ----------------------------------------------------------------------

def test_physical_count_sets_stock_to_the_counted_number(db, tenant, client_admin, soap):
    _opening(tenant, client_admin, soap, "10")
    _sell(tenant, client_admin, _line(soap, "3"))
    loc = ledger.default_location(tenant.id)
    adj = services.create_adjustment(tenant, client_admin, loc.id, AdjustmentReason.COUNT,
                                     [services.StockLineInput(soap.id, D("5"))])
    assert adj.lines[0].qty == D("-2") and adj.lines[0].counted_qty == D("5")
    assert _on_hand(tenant, soap) == D("5")
    services.void_adjustment(adj, client_admin, "recount")
    assert _on_hand(tenant, soap) == D("7")
    with pytest.raises(ledger.StockError):
        services.void_adjustment(adj, client_admin, "again")


def test_adjustments_need_a_reason_note_for_other_and_numbers_never_repeat(db, tenant, client_admin, soap):
    loc = ledger.default_location(tenant.id)
    with pytest.raises(ledger.StockError, match="Say what"):
        services.create_adjustment(tenant, client_admin, loc.id, AdjustmentReason.OTHER,
                                   [services.StockLineInput(soap.id, D("-1"))])
    a1 = services.create_adjustment(tenant, client_admin, loc.id, AdjustmentReason.DAMAGE,
                                    [services.StockLineInput(soap.id, D("1"))])
    a2 = services.create_adjustment(tenant, client_admin, loc.id, AdjustmentReason.WASTAGE,
                                    [services.StockLineInput(soap.id, D("1"))])
    assert a1.number != a2.number and a1.number.startswith("ADJ")
    assert _on_hand(tenant, soap) == D("-2")


def test_transfer_moves_stock_and_void_moves_it_back(db, tenant, client_admin, soap):
    _on(db, tenant, "inventory")
    soap.allow_negative_stock = False
    db.session.commit()
    _opening(tenant, client_admin, soap, "10")
    store = ledger.default_location(tenant.id)
    godown = services.create_location(tenant, "Back godown", LocationKind.GODOWN, "Plot 4, MIDC")
    db.session.commit()
    with pytest.raises(ledger.StockError, match="Only 10"):
        services.create_transfer(tenant, client_admin, store.id, godown.id, [services.StockLineInput(soap.id, D("11"))])
    db.session.rollback()
    t = services.create_transfer(tenant, client_admin, store.id, godown.id, [services.StockLineInput(soap.id, D("4"))],
                                 vehicle_no="MH12AB1234")
    assert t.number.startswith("TDC")
    assert _on_hand(tenant, soap, store.id) == D("6") and _on_hand(tenant, soap, godown.id) == D("4")
    assert _on_hand(tenant, soap) == D("10")  # a transfer doesn't change the total
    with pytest.raises(ledger.StockError, match="must be empty"):
        services.deactivate_location(godown)
    services.void_transfer(t, client_admin, "wrong godown")
    assert _on_hand(tenant, soap, godown.id) == D("0")
    services.deactivate_location(godown)
    assert not godown.is_active


# --- reports -------------------------------------------------------------------------------------

def test_low_stock_reorder_and_movement_analysis(db, tenant, client_admin, soap):
    shampoo = _product(db, tenant, "Shampoo", min_stock_level=D("5"))
    dead = _product(db, tenant, "Old stock")
    soap.min_stock_level = D("2")
    db.session.commit()
    _opening(tenant, client_admin, soap, "10")
    _opening(tenant, client_admin, shampoo, "6")
    _opening(tenant, client_admin, dead, "3")
    _sell(tenant, client_admin, _line(soap, "4"), _line(shampoo, "2"))
    low = reports.low_stock(tenant.id)
    assert [r.product.name for r in low] == ["Shampoo"]
    reorder = reports.reorder_report(tenant.id)
    assert reorder[0].suggested == D("6")  # back up to 2 x 5 = 10, from 4
    classes = {r.product.name: r.klass for r in reports.movement_analysis(tenant.id, 90)}
    assert classes == {"Soap": "fast", "Shampoo": "slow", "Old stock": "dead"}
    rows, value = reports.stock_summary(tenant.id)
    assert value == D("6") * 20 + D("4") * 20 + D("3") * 20


def test_item_ledger_has_running_balance(db, tenant, client_admin, soap):
    _opening(tenant, client_admin, soap, "10")
    _sell(tenant, client_admin, _line(soap, "3"))
    opening, rows, closing = reports.item_ledger(tenant.id, soap.id)
    assert [r.balance for r in rows] == [D("10"), D("7")] and closing == D("7") and opening == 0


# --- screens and flags -------------------------------------------------------------------------------

STOCK_PAGES = ["/inventory/", "/inventory/locations", "/inventory/adjustments", "/inventory/adjustments/new",
               "/inventory/transfers", "/inventory/transfers/new", "/inventory/reorder", "/inventory/movement",
               "/inventory/by-location", "/inventory/labels"]


def test_stock_screens_are_404_and_hidden_with_inventory_off(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for url in STOCK_PAGES + ["/inventory/expiry", "/inventory/serials"]:
        assert client.get(url).status_code == 404, url
    assert b'href="/inventory/"' not in client.get("/").data


def test_stock_screens_render_with_inventory_on(client, db, tenant, client_admin, soap, staff):
    _on(db, tenant, "inventory")
    _opening(tenant, client_admin, soap, "3")
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for url in STOCK_PAGES + [f"/inventory/items/{soap.id}"]:
        assert client.get(url).status_code == 200, url
    assert b'href="/inventory/"' in client.get("/inventory/").data
    # Batch / serial screens need their own flags too.
    assert client.get("/inventory/expiry").status_code == 404
    _on(db, tenant, "batch_expiry", "serial_imei")
    assert client.get("/inventory/expiry").status_code == 200
    assert client.get("/inventory/serials?q=X").status_code == 200


def test_staff_cannot_open_stock_screens(client, db, tenant, staff):
    _on(db, tenant, "inventory")
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    assert client.get("/inventory/").status_code == 403


def test_adjustment_and_transfer_through_the_screens(client, db, tenant, client_admin, soap):
    _on(db, tenant, "inventory")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    store = ledger.default_location(tenant.id)
    db.session.commit()
    resp = client.post("/inventory/adjustments/new", data={
        "reason": "opening", "location_id": store.id, "adjusted_on": date.today().isoformat(),
        "line_product_id[]": [str(soap.id), ""], "line_qty[]": ["12", ""], "line_cost[]": ["18", ""],
    })
    assert resp.status_code == 302
    adj = StockAdjustment.query.one()
    assert AuditLog.query.filter_by(action="stock_adjusted", entity_id=adj.id).count() == 1

    client.post("/inventory/locations", data={"name": "Godown 2", "kind": "godown"})
    godown = StockLocation.query.filter_by(name="Godown 2").one()
    resp = client.post("/inventory/transfers/new", data={
        "from_location_id": store.id, "to_location_id": godown.id, "line_product_id[]": [str(soap.id)],
        "line_qty[]": ["5"], "vehicle_no": "TN01X1",
    })
    assert resp.status_code == 302
    transfer = StockTransfer.query.one()
    pdf = client.get(f"/inventory/transfers/{transfer.id}/challan.pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")

    # A refused form keeps what was typed and says why.
    resp = client.post("/inventory/adjustments/new", data={"reason": "other", "location_id": store.id,
                                                            "line_product_id[]": [str(soap.id)], "line_qty[]": ["-1"]})
    assert resp.status_code == 400 and b"Say what the correction is for" in resp.data

    resp = client.post(f"/inventory/adjustments/{adj.id}/void", data={"reason": "typo"})
    assert resp.status_code == 302 and adj.status.value == "voided"


def test_labels_pdf(client, db, tenant, client_admin, soap):
    _on(db, tenant, "inventory")
    soap.barcode = "8901234567890"  # valid EAN-13
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for layout in ("a4", "roll"):
        resp = client.post("/inventory/labels", data={f"copies_{soap.id}": "3", "layout": layout, "show_price": "1"})
        assert resp.status_code == 200 and resp.data.startswith(b"%PDF"), layout


def test_label_code_falls_back_to_sku_then_internal_code(db, tenant, soap):
    from app.inventory.labels import _ean13_ok, label_code

    assert label_code(soap) == f"ARFA{soap.id:06d}"
    soap.sku = "SOAP-1"
    assert label_code(soap) == "SOAP-1"
    assert _ean13_ok("8901234567890") and not _ean13_ok("8901234567891")


def test_live_dashboard_shows_low_stock_only_with_inventory(client, db, tenant, client_admin, soap):
    soap.min_stock_level = D("5")
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    assert b"Low stock" not in client.get("/live/").data
    _on(db, tenant, "inventory")
    db.session.remove()
    g.pop("_login_user", None)
    assert b"Low stock" in client.get("/live/").data


# --- variants + product-form module fields -----------------------------------------------------------

def test_variant_matrix_makes_sellable_items_and_hides_the_design(client, db, tenant, client_admin):
    from app.products import variants as vs

    _on(db, tenant, "variants")
    shirt = _product(db, tenant, "Shirt", sku="SH")
    created = vs.generate(shirt, vs.parse_attributes(["Size", "Colour"], ["S, M", "Red, Blue"]))
    db.session.commit()
    assert len(created) == 4 and shirt.has_variants
    assert {v.name for v in created} == {"Shirt - S / Red", "Shirt - S / Blue", "Shirt - M / Red", "Shirt - M / Blue"}
    assert {v.sku for v in created} == {"SH-S-RED", "SH-S-BLUE", "SH-M-RED", "SH-M-BLUE"}
    assert all(v.hsn_or_sac_code == shirt.hsn_or_sac_code and v.gst_rate == shirt.gst_rate for v in created)
    # Re-running only adds what's new.
    assert len(vs.generate(shirt, vs.parse_attributes(["Size", "Colour"], ["S, M, L", "Red, Blue"]))) == 2
    db.session.commit()
    with pytest.raises(vs.VariantError):
        vs.generate(created[0], vs.parse_attributes(["Size"], ["XL"]))

    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.get("/pos/").data.decode()
    assert '"Shirt - S / Red"' in page and 'name: "Shirt",' not in page  # the design isn't a POS tile
    assert client.get(f"/products/{shirt.id}/variants").status_code == 200
    v = created[0]
    resp = client.post(f"/products/{shirt.id}/variants", data={"action": "update", f"sku_{v.id}": "NEW-1",
                                                               f"price_{v.id}": "499", f"active_{v.id}": "1"})
    assert resp.status_code == 302
    db.session.refresh(v)
    assert v.sku == "NEW-1" and v.sale_price == D("499.00")


def test_design_with_stock_must_be_emptied_before_variants(db, tenant, client_admin):
    from app.products import variants as vs

    shirt = _product(db, tenant, "Shirt")
    _opening(tenant, client_admin, shirt, "3")
    with pytest.raises(vs.VariantError, match="adjust it to 0"):
        vs.generate(shirt, vs.parse_attributes(["Size"], ["S"]))


def test_variants_screen_is_404_without_the_flag(client, db, tenant, client_admin, soap):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    assert client.get(f"/products/{soap.id}/variants").status_code == 404
    assert b"/variants" not in client.get(f"/products/{soap.id}/edit").data


def test_tracking_fields_kept_when_their_module_is_off(client, db, tenant, client_admin):
    phone = _product(db, tenant, "Phone", track_serial=True, warranty_months=12, track_batch=True)
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.get(f"/products/{phone.id}/edit").data
    assert b"track_serial" not in page and b"track_batch" not in page
    resp = client.post(f"/products/{phone.id}/edit", data={
        "name": "Phone", "hsn_or_sac_code": "3401", "gst_rate": "18", "sale_price": "50", "unit": "PCS",
        "allow_negative_stock": "y",
    })
    assert resp.status_code == 302
    db.session.refresh(phone)
    assert phone.track_serial and phone.warranty_months == 12 and phone.track_batch
    _on(db, tenant, "serial_imei", "batch_expiry")
    assert b"track_serial" in client.get(f"/products/{phone.id}/edit").data


def test_receipt_prints_serial_numbers(client, db, tenant, client_admin):
    _on(db, tenant, "inventory", "purchases", "serial_imei")
    phone = _product(db, tenant, "Phone Y", track_serial=True)
    _serial_stock(db, tenant, client_admin, phone, "SN-111", "SN-222")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.get("/pos/").data.decode()
    assert 'id="serials-' in page and "SN-111" in page
    resp = client.post("/pos/checkout", data={
        "payment_mode": "cash", "line_product_id[]": [str(phone.id)], "line_description[]": ["Phone Y"],
        "line_hsn[]": ["3401"], "line_qty[]": ["1"], "line_rate[]": ["50"], "line_gst_rate[]": ["18"],
        "line_incl[]": ["0"], "line_unit[]": ["PCS"], "line_discount_kind[]": [""], "line_discount_value[]": [""],
        "line_serials[]": ["SN-222"],
    }, headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 200, resp.data
    from app.models.pos_bill import POSBill

    bill = POSBill.query.filter(POSBill.bill_number.isnot(None)).one()
    assert bill.lines[0].serial_numbers == ["SN-222"]
    assert b"S/N: SN-222" in client.get(f"/pos/bills/{bill.id}/receipt").data
