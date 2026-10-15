"""Phase 5 margin scheme (Rule 32(5)) end to end: purchases without ITC,
lots (FIFO / by serial), margin-scheme invoices at the POS and in
Invoicing (own series, never mixed with regular goods), voids, refunds,
returns, GSTR-1 HSN disclosure, the margin register, printing, settings,
the module flag and tenant isolation."""

from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader

from app.inventory import ledger
from app.invoicing.services import InvoiceInput, InvoiceValidationError, LineInput, create_invoice, void_invoice
from app.margin import services as ms
from app.margin.reports import register_data
from app.models.customer import Customer
from app.models.invoice_series import DocumentType
from app.models.margin import MarginAllocation
from app.models.pos_bill import PaymentMode, POSBill
from app.models.product import Product
from app.models.purchase import ItcEligibility, Supplier
from app.models.tenant import Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.notes.services import NoteInput, NoteValidationError, create_note
from app.pos.returns import create_return, void_return
from app.pos.services import CartInput, CartLineInput, POSValidationError, checkout, refund_bill, void_bill
from app.purchases import services as ps
from app.reports.gstr import gstr1_data
from app.utils.tenant_scope import tenant_query

D = Decimal
TODAY = date.today()
PERIOD = TODAY.strftime("%Y-%m")


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _on(db, tenant, *keys):
    for key in keys:
        tenant.set_feature(key, True)
    db.session.commit()


@pytest.fixture()
def setup(db, tenant, client_admin):
    _on(db, tenant, "margin_scheme", "purchases")
    phone = Product(tenant_id=tenant.id, name="Used iPhone 12", hsn_or_sac_code="85171300", gst_rate=D("18"), unit="NOS",
                    sale_price=D("12000"), price_includes_tax=True, is_margin_scheme=True)
    charger = Product(tenant_id=tenant.id, name="New charger", hsn_or_sac_code="85044030", gst_rate=D("18"), unit="NOS",
                      sale_price=D("590"), price_includes_tax=True)
    seller = Supplier(tenant_id=tenant.id, name="Walk-in seller (unregistered)", state_code="27", state_name="Maharashtra")
    db.session.add_all([phone, charger, seller])
    db.session.commit()
    return {"phone": phone, "charger": charger, "seller": seller}


def _buy(db, tenant, user, supplier, product, qty="1", rate="10000", gst="0", invoice_no="BUY-1", serials=None):
    bill = ps.create_purchase_bill(tenant, user, ps.PurchaseInput(
        supplier_id=supplier.id, gstin_id=tenant.gstins.first().id, supplier_invoice_no=invoice_no,
        supplier_invoice_date=TODAY, location_id=ledger.default_location(tenant.id).id,
        lines=[ps.PurchaseLineInput(product_id=product.id, description=product.name, hsn_or_sac_code=product.hsn_or_sac_code,
                                    qty=D(qty), unit="NOS", rate=D(rate), gst_rate=D(gst), serials=list(serials or []))],
    ))
    db.session.commit()
    return bill


def _line(product, qty="1", rate=None, serials=()):
    return CartLineInput(description=product.name, hsn_or_sac_code=product.hsn_or_sac_code, qty=D(qty),
                         rate=D(rate) if rate else product.sale_price, gst_rate=product.gst_rate, product_id=product.id,
                         unit=product.unit, serials=tuple(serials))


def _sell(tenant, user, *lines, customer_id=None):
    bill = checkout(tenant, user, CartInput(lines=list(lines), customer_id=customer_id), PaymentMode.CASH)
    from app.extensions import db

    db.session.commit()
    return bill


# --- purchases ------------------------------------------------------------------------------

def test_margin_purchase_never_gives_itc(db, tenant, client_admin, setup):
    bill = _buy(db, tenant, client_admin, setup["seller"], setup["phone"], rate="10000", gst="18", invoice_no="P-18")
    line = bill.lines[0]
    assert line.is_margin_scheme and line.itc_eligibility == ItcEligibility.INELIGIBLE and bill.itc_total == 0
    assert line.margin_unit_price == D("11800.00")  # what was paid - the GST included, as no credit is taken


# --- POS --------------------------------------------------------------------------------------

def test_pos_margin_sale_is_its_own_invoice_with_tax_on_the_margin_only(db, tenant, client_admin, setup):
    _buy(db, tenant, client_admin, setup["seller"], setup["phone"])
    bill = _sell(tenant, client_admin, _line(setup["phone"], rate="12000"))
    assert bill.is_margin_scheme and bill.bill_number.startswith("MSI/")
    line = bill.lines[0]
    assert (line.line_total, line.margin_purchase_value, line.margin_value) == (D("12000.00"), D("10000.00"), D("2000.00"))
    assert (line.taxable_value, line.cgst_amount, line.sgst_amount) == (D("1694.92"), D("152.54"), D("152.54"))
    assert bill.grand_total == D("12000.00") and bill.total_taxable_value == D("1694.92")
    alloc = MarginAllocation.query.one()
    assert (alloc.qty, alloc.purchase_value, alloc.sale_value, alloc.document_number) == (D("1"), D("10000.00"), D("12000.00"), bill.bill_number)
    # A regular sale keeps the normal POS series.
    regular = _sell(tenant, client_admin, _line(setup["charger"]))
    assert not regular.is_margin_scheme and not regular.bill_number.startswith("MSI/")


def test_mixed_cart_is_refused_before_any_number_is_used(db, tenant, client_admin, setup):
    _buy(db, tenant, client_admin, setup["seller"], setup["phone"])
    with pytest.raises(POSValidationError, match="separate invoice"):
        checkout(tenant, client_admin, CartInput(lines=[_line(setup["phone"]), _line(setup["charger"])]), PaymentMode.CASH)
    db.session.rollback()
    assert POSBill.query.count() == 0


def test_no_purchase_on_record_means_it_cant_be_sold_under_the_scheme(db, tenant, client_admin, setup):
    with pytest.raises(POSValidationError, match="no margin-scheme purchase on record"):
        _sell(tenant, client_admin, _line(setup["phone"]))
    db.session.rollback()
    _buy(db, tenant, client_admin, setup["seller"], setup["phone"], qty="1")
    with pytest.raises(POSValidationError, match="only 1 unit"):
        _sell(tenant, client_admin, _line(setup["phone"], qty="2"))


def test_fifo_lots_and_a_loss_never_cancels_tax_on_another_unit(db, tenant, client_admin, setup):
    _buy(db, tenant, client_admin, setup["seller"], setup["phone"], rate="9000", invoice_no="A")
    _buy(db, tenant, client_admin, setup["seller"], setup["phone"], rate="13000", invoice_no="B")
    bill = _sell(tenant, client_admin, _line(setup["phone"], qty="2", rate="12000"))
    line = bill.lines[0]
    # unit 1: 12000 - 9000 = 3000 margin; unit 2: 12000 - 13000 = loss -> 0
    assert line.margin_value == D("3000.00") and line.margin_purchase_value == D("22000.00")
    assert line.taxable_value == D("2542.37") and line.cgst_amount + line.sgst_amount == D("457.63")
    assert [a.purchase_value for a in MarginAllocation.query.order_by(MarginAllocation.id)] == [D("9000.00"), D("13000.00")]


def test_serial_unit_takes_its_own_purchase_price(db, tenant, client_admin, setup):
    _on(db, tenant, "serial_imei")
    phone = setup["phone"]
    phone.track_serial = True
    db.session.commit()
    _buy(db, tenant, client_admin, setup["seller"], phone, rate="8000", invoice_no="S1", serials=["IMEI-A"])
    _buy(db, tenant, client_admin, setup["seller"], phone, rate="11000", invoice_no="S2", serials=["IMEI-B"])
    bill = _sell(tenant, client_admin, _line(phone, rate="12000", serials=["IMEI-B"]))
    assert bill.lines[0].margin_purchase_value == D("11000.00") and bill.lines[0].margin_value == D("1000.00")
    assert MarginAllocation.query.one().serial.serial_no == "IMEI-B"
    with pytest.raises(POSValidationError, match="serial"):
        _sell(tenant, client_admin, _line(phone, rate="12000"))


def test_void_and_refund_give_units_back_to_their_lots(db, tenant, client_admin, setup):
    bought = _buy(db, tenant, client_admin, setup["seller"], setup["phone"])
    lot = bought.lines[0]
    bill = _sell(tenant, client_admin, _line(setup["phone"]))
    assert ms.lot_remaining(lot) == 0
    void_bill(bill, client_admin, "wrong customer")
    db.session.commit()
    assert ms.lot_remaining(lot) == 1
    bill2 = _sell(tenant, client_admin, _line(setup["phone"]))
    refund_bill(bill2, client_admin, bill2.grand_total, "unhappy")
    db.session.commit()
    assert ms.lot_remaining(lot) == 1


def test_item_return_and_its_void(db, tenant, client_admin, setup):
    bought = _buy(db, tenant, client_admin, setup["seller"], setup["phone"], qty="2")
    bill = _sell(tenant, client_admin, _line(setup["phone"], qty="2", rate="12000"))
    line = bill.lines[0]
    ret = create_return(bill, client_admin, {line.id: D("1")}, "screen issue", PaymentMode.CASH)
    db.session.commit()
    assert ret.grand_total == D("12000.00")  # the price, not taxable + tax
    assert ret.lines[0].taxable_value == (line.taxable_value / 2).quantize(D("0.01"), rounding="ROUND_HALF_UP")
    assert ms.lot_remaining(bought.lines[0]) == 1
    void_return(ret, client_admin, "keyed wrong")
    db.session.commit()
    assert ms.lot_remaining(bought.lines[0]) == 0


# --- Invoicing ----------------------------------------------------------------------------------

def _invoice(tenant, product, customer, qty="1", rate="12000", document_type=DocumentType.TAX_INVOICE, pos=None):
    gstin = tenant.gstins.first()
    return InvoiceInput(gstin_id=gstin.id, document_type=document_type, place_of_supply_state_code=pos or customer.state_code,
                        invoice_date=TODAY, notes=None, customer_id=customer.id,
                        lines=[LineInput(description=product.name, hsn_or_sac_code=product.hsn_or_sac_code, qty=D(qty),
                                         rate=D(rate), discount_percent=D("0"), gst_rate=product.gst_rate,
                                         product_id=product.id, unit=product.unit, price_includes_tax=True)])


@pytest.fixture()
def buyer(db, tenant):
    c = Customer(tenant_id=tenant.id, name="Rahul Mobiles", gstin="29AAAAA9999A1Z5", state_code="29", state_name="Karnataka")
    db.session.add(c)
    db.session.commit()
    return c


def test_invoice_margin_inter_state_is_igst_in_its_own_series(db, tenant, client_admin, setup, buyer):
    _buy(db, tenant, client_admin, setup["seller"], setup["phone"])
    inv = create_invoice(tenant, client_admin, _invoice(tenant, setup["phone"], buyer))
    db.session.commit()
    assert inv.document_type == DocumentType.MARGIN_SCHEME_INVOICE and inv.invoice_number.startswith("MSI/")
    line = inv.lines[0]
    assert (line.igst_amount, line.cgst_amount, line.taxable_value) == (D("305.08"), D("0.00"), D("1694.92"))
    assert inv.grand_total == D("12000.00")
    # Same series as the counter's margin bills.
    pos_bill = None
    _buy(db, tenant, client_admin, setup["seller"], setup["phone"], invoice_no="BUY-2")
    pos_bill = _sell(tenant, client_admin, _line(setup["phone"]))
    assert pos_bill.bill_number.rsplit("/", 1)[0] == inv.invoice_number.rsplit("/", 1)[0]
    assert int(pos_bill.bill_number.rsplit("/", 1)[1]) == int(inv.invoice_number.rsplit("/", 1)[1]) + 1
    with pytest.raises(NoteValidationError, match="margin-scheme"):
        create_note(tenant, client_admin, NoteInput(document_type=DocumentType.CREDIT_NOTE, original_invoice_id=inv.id,
                                                    note_date=TODAY, reason="x", lines=_invoice(tenant, setup["phone"], buyer).lines))
    db.session.rollback()
    void_invoice(inv, client_admin, "re-bill")
    db.session.commit()
    assert MarginAllocation.query.filter_by(source_type="invoice").one().released_at is not None


def test_invoice_refusals(db, tenant, client_admin, setup, buyer):
    _buy(db, tenant, client_admin, setup["seller"], setup["phone"])
    mixed = _invoice(tenant, setup["phone"], buyer)
    mixed.lines.append(LineInput(description="Charger", hsn_or_sac_code="85044030", qty=D("1"), rate=D("590"),
                                 discount_percent=D("0"), gst_rate=D("18"), product_id=setup["charger"].id))
    with pytest.raises(InvoiceValidationError, match="separate invoice"):
        create_invoice(tenant, client_admin, mixed)
    db.session.rollback()
    with pytest.raises(InvoiceValidationError, match="margin-scheme invoice"):
        create_invoice(tenant, client_admin, _invoice(tenant, setup["phone"], buyer, document_type=DocumentType.EXPORT_INVOICE_LUT))
    db.session.rollback()
    _on(db, tenant, "serial_imei")
    setup["phone"].track_serial = True
    db.session.commit()
    with pytest.raises(InvoiceValidationError, match="counter"):
        create_invoice(tenant, client_admin, _invoice(tenant, setup["phone"], buyer))


def test_purchase_void_and_return_respect_sold_units(db, tenant, client_admin, setup):
    bought = _buy(db, tenant, client_admin, setup["seller"], setup["phone"], qty="2")
    _sell(tenant, client_admin, _line(setup["phone"]))
    with pytest.raises(ps.PurchaseError, match="margin scheme"):
        ps.void_purchase_bill(bought, client_admin, "x")
    with pytest.raises(ps.PurchaseError, match="unsold"):
        ps.create_purchase_return(bought, client_admin, {bought.lines[0].id: D("2")}, "x")
    ret = ps.create_purchase_return(bought, client_admin, {bought.lines[0].id: D("1")}, "faulty")
    assert ret.itc_cgst + ret.itc_sgst + ret.itc_igst == 0
    assert ms.lot_remaining(bought.lines[0]) == 0


# --- module flag ------------------------------------------------------------------------------

def test_with_the_module_off_the_item_sells_as_regular_goods(db, tenant, client_admin, setup):
    tenant.set_feature("margin_scheme", False)
    db.session.commit()
    bill = _sell(tenant, client_admin, _line(setup["phone"]))
    assert not bill.is_margin_scheme and bill.lines[0].taxable_value == D("10169.49")
    assert MarginAllocation.query.count() == 0


def test_screens_404_when_off_and_render_when_on(client, db, tenant, client_admin, setup):
    tenant.set_feature("margin_scheme", False)
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for url in ("/reports/margin-register", "/reports/margin-register.xlsx", "/reports/margin-register.pdf"):
        assert client.get(url).status_code == 404, url
    assert b"is_margin_scheme" not in client.get(f"/products/{setup['phone'].id}/edit").data
    assert b"margin_scheme_declaration" not in client.get("/settings/").data
    _on(db, tenant, "margin_scheme")
    assert b"is_margin_scheme" in client.get(f"/products/{setup['phone'].id}/edit").data
    assert b"Margin register" in client.get("/reports/").data
    for url in ("/reports/margin-register", "/reports/margin-register.xlsx", "/reports/margin-register.pdf"):
        assert client.get(url).status_code == 200, url


def test_product_flag_forces_an_inclusive_price(client, db, tenant, client_admin, setup):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    resp = client.post("/products/new", data={"name": "Used Bike", "hsn_or_sac_code": "87112019", "gst_rate": "18",
                                              "sale_price": "45000", "unit": "NOS", "is_margin_scheme": "y",
                                              "allow_negative_stock": "y"})
    assert resp.status_code == 302
    bike = Product.query.filter_by(name="Used Bike").one()
    assert bike.is_margin_scheme and bike.price_includes_tax


# --- printing + settings --------------------------------------------------------------------------

def test_receipt_and_invoice_pdf_show_no_gst_and_the_declaration(client, db, tenant, client_admin, setup, buyer):
    _buy(db, tenant, client_admin, setup["seller"], setup["phone"], qty="2")
    bill = _sell(tenant, client_admin, _line(setup["phone"]))
    inv = create_invoice(tenant, client_admin, _invoice(tenant, setup["phone"], buyer))
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    resp = client.post("/settings/", data={"margin_settings_shown": "1",
                                           "margin_scheme_declaration": "Tax under margin scheme - Rule 32(5).",
                                           "default_receipt_format": "3in", "business_type": tenant.business_type.value,
                                           "feature_margin_scheme": "y", "feature_purchases": "y"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert Tenant.query.get(tenant.id).margin_scheme_declaration == "Tax under margin scheme - Rule 32(5)."
    for url in (f"/pos/bills/{bill.id}/receipt.pdf?format=a4", f"/invoices/{inv.id}/pdf"):
        r = client.get(url)
        assert r.status_code == 200, url
        text = "".join(p.extract_text() for p in PdfReader(BytesIO(r.data)).pages)
        assert "Rule 32(5)" in text and "CGST" not in text and "IGST" not in text, url
        assert "MSI/" in text
    assert b"Margin scheme invoice" in client.get(f"/pos/bills/{bill.id}/receipt").data
    assert b"Taxable margin" in client.get(f"/invoices/{inv.id}").data


# --- GSTR-1 + register ---------------------------------------------------------------------------

def test_gstr1_hsn_has_a_margin_row_and_a_zero_rate_remainder_row(db, tenant, client_admin, setup, buyer):
    _buy(db, tenant, client_admin, setup["seller"], setup["phone"], qty="3")
    _sell(tenant, client_admin, _line(setup["phone"], rate="12000"))                       # B2C, intra
    create_invoice(tenant, client_admin, _invoice(tenant, setup["phone"], buyer))           # B2B, inter
    _sell(tenant, client_admin, _line(setup["charger"]))                                     # a regular sale
    db.session.commit()
    data = gstr1_data(tenant, tenant.gstins.first(), PERIOD)
    rows = [h for h in data["hsn_summary"] if h.hsn == "85171300"]
    margin_row = next(h for h in rows if not h.margin_remainder)
    rest_row = next(h for h in rows if h.margin_remainder)
    assert margin_row.gst_rate == D("18") and margin_row.qty == 2
    assert margin_row.taxable_value == D("3389.84") and margin_row.cgst + margin_row.sgst + margin_row.igst == D("610.16")
    assert rest_row.gst_rate == 0 and rest_row.qty == 0 and rest_row.taxable_value == D("20000.00")
    assert margin_row.total + rest_row.total == D("24000.00")  # the full sale value is disclosed
    # B2B by the buyer's GSTIN, at the taxable margin.
    b2b = {e["customer_gstin"]: e for e in data["b2b"]}
    assert b2b["29AAAAA9999A1Z5"]["documents"][0]["taxable_value"] == D("1694.92")
    labels = {s.label for s in data["document_summary"]}
    assert "Invoice - Margin Scheme" in labels


def test_margin_register_report(client, db, tenant, client_admin, setup):
    _buy(db, tenant, client_admin, setup["seller"], setup["phone"], rate="13000")
    _sell(tenant, client_admin, _line(setup["phone"], rate="12000"))
    data = register_data(tenant, TODAY, TODAY)
    row = data["rows"][0]
    assert (row.purchase_value, row.sale_value, row.margin, row.raw_margin, row.taxable_value) == (
        D("13000.00"), D("12000.00"), D("0.00"), D("-1000.00"), D("0.00"))
    assert data["totals"]["losses"] == D("1000.00")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    wb = load_workbook(BytesIO(client.get("/reports/margin-register.xlsx").data))
    assert wb.active["A5"].value == "Used iPhone 12"
    page = client.get("/reports/margin-register").data.decode()
    assert "loss 1000.00" in page


# --- tenant isolation -----------------------------------------------------------------------------

def test_lots_and_register_never_cross_tenants(app, client, db, tenant, client_admin, setup):
    b = Tenant(legal_name="Beta Used Phones", registration_type=RegistrationType.REGULAR,
               features={"margin_scheme": True, "purchases": True})
    db.session.add(b)
    db.session.flush()
    db.session.add(Gstin(tenant_id=b.id, gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka", is_primary=True))
    admin_b = User(tenant_id=b.id, name="Beta Admin", email="admin@beta.example.com", role=UserRole.CLIENT_ADMIN,
                   must_change_password=False)
    admin_b.set_password("BetaSecret123")
    phone_b = Product(tenant_id=b.id, name="Beta used phone", hsn_or_sac_code="85171300", gst_rate=D("18"),
                      sale_price=D("5000"), price_includes_tax=True, is_margin_scheme=True)
    seller_b = Supplier(tenant_id=b.id, name="Beta seller", state_code="29", state_name="Karnataka")
    db.session.add_all([admin_b, phone_b, seller_b])
    db.session.commit()
    _buy(db, b, admin_b, seller_b, phone_b, rate="4000", invoice_no="B-1")
    _sell(b, admin_b, _line(phone_b, rate="5000"))
    # A's own phone has no lot, even though B has one: lots are per client.
    assert ms.available_lots(tenant.id, phone_b.id) == []
    with pytest.raises(POSValidationError):
        _sell(tenant, client_admin, _line(setup["phone"]))
    db.session.rollback()
    assert register_data(tenant, TODAY, TODAY)["rows"] == []
    _login(client, "admin@acme.example.com", "ClientSecret123")
    assert b"Beta used phone" not in client.get("/reports/margin-register").data
    from flask_login import login_user

    with app.test_request_context():
        login_user(client_admin)
        assert tenant_query(MarginAllocation).all() == []
