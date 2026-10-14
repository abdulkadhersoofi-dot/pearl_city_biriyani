"""POS for every business: discounts, price edits, price lists, split
payments, cash tendered, credit sales, customers on a bill, UPI QR,
favourites, sharing and duplicate copies."""

from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from pypdf import PdfReader

from app.models.customer import Customer
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus, POSPayment
from app.models.price_list import PriceList, PriceListItem
from app.models.product import Product
from app.models.tenant import Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.pos.services import (
    CartInput,
    CartLineInput,
    PaymentInput,
    POSValidationError,
    checkout,
    hold_cart,
    z_report,
)

D = Decimal


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def tea(db, tenant):
    p = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="0902", gst_rate=D("5"), unit="NOS",
                sale_price=D("100"), sku="TEA-1", barcode="8901234567890")
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture()
def rice(db, tenant):
    p = Product(tenant_id=tenant.id, name="Rice", hsn_or_sac_code="1006", gst_rate=D("5"), unit="KGS",
                sale_price=D("80"), price_includes_tax=True)
    db.session.add(p)
    db.session.commit()
    return p


def _line(product, qty="1", rate=None, kind=None, value="0"):
    return CartLineInput(
        description=product.name, hsn_or_sac_code=product.hsn_or_sac_code, qty=D(qty),
        rate=D(rate if rate is not None else product.sale_price), gst_rate=product.gst_rate,
        product_id=product.id, unit=product.unit, discount_kind=kind, discount_value=D(value),
    )


def _cart(*lines, **kwargs):
    return CartInput(lines=list(lines), **kwargs)


def _staff_with(db, staff, **perms):
    for key, value in perms.items():
        setattr(staff, key, value)
    db.session.commit()
    return staff


# --- discounts -----------------------------------------------------------------

def test_rupee_line_discount_comes_off_what_the_customer_pays(db, tenant, client_admin, tea):
    bill = checkout(tenant, client_admin, _cart(_line(tea, kind="amount", value="21")), PaymentMode.CASH)
    # Rs. 21 off a Rs. 105 (100 + 5%) line = Rs. 20 off the pre-tax price
    line = bill.lines[0]
    assert line.discount_flat == D("20.00")
    assert line.taxable_value == D("80.00")
    assert bill.grand_total == D("84.00")
    assert (line.line_discount_kind, line.line_discount_value) == ("amount", D("21.00"))


def test_bill_discount_is_spread_over_the_lines(db, tenant, client_admin, tea, rice):
    cart = _cart(_line(tea, qty="2"), _line(rice, qty="1.5"), bill_discount_kind="percent", bill_discount_value=D("10"))
    bill = checkout(tenant, client_admin, cart, PaymentMode.CASH)
    # 210 (tea, 2 x 100 + 5%) + 120 (rice 1.5 kg x 80 incl.) = 330; 10% = 33
    assert sum((l.line_total for l in bill.lines), D("0")) == D("297.00")
    assert bill.grand_total == D("297.00")
    assert bill.bill_discount_kind == "percent" and bill.bill_discount_value == D("10")
    assert bill.lines[1].qty == D("1.500")


def test_staff_discount_cap_is_enforced_for_line_and_bill_discounts(db, tenant, staff, tea):
    with pytest.raises(POSValidationError, match="isn't allowed"):
        checkout(tenant, staff, _cart(_line(tea, kind="percent", value="5")), PaymentMode.CASH)
    _staff_with(db, staff, can_give_discount=True, max_discount_percent=D("10"))
    with pytest.raises(POSValidationError, match="at most 10%"):
        checkout(tenant, staff, _cart(_line(tea), bill_discount_kind="amount", bill_discount_value=D("20")), PaymentMode.CASH)
    bill = checkout(tenant, staff, _cart(_line(tea), bill_discount_kind="percent", bill_discount_value=D("10")), PaymentMode.CASH)
    assert bill.grand_total == D("95.00")  # 105 - 10.50 = 94.50 -> 95 (rupee round-off, half up)


def test_discount_bigger_than_the_line_is_refused(db, tenant, client_admin, tea):
    with pytest.raises(POSValidationError, match="more than its price"):
        checkout(tenant, client_admin, _cart(_line(tea, kind="amount", value="500")), PaymentMode.CASH)
    with pytest.raises(POSValidationError, match="more than the bill"):
        checkout(tenant, client_admin, _cart(_line(tea), bill_discount_kind="amount", bill_discount_value=D("500")), PaymentMode.CASH)


def test_zero_or_negative_quantity_is_refused(db, tenant, client_admin, tea):
    with pytest.raises(POSValidationError, match="more than 0"):
        checkout(tenant, client_admin, _cart(_line(tea, qty="0")), PaymentMode.CASH)


# --- price edit and price lists -------------------------------------------------

def test_price_edit_needs_the_permission(db, tenant, staff, tea):
    bill = checkout(tenant, staff, _cart(_line(tea, rate="50")), PaymentMode.CASH)
    assert bill.lines[0].rate == D("100")
    _staff_with(db, staff, can_edit_price_at_pos=True)
    bill = checkout(tenant, staff, _cart(_line(tea, rate="50")), PaymentMode.CASH)
    assert bill.lines[0].rate == D("50")


@pytest.fixture()
def wholesale(db, tenant, tea):
    pl = PriceList(tenant_id=tenant.id, name="Wholesale")
    db.session.add(pl)
    db.session.flush()
    db.session.add(PriceListItem(tenant_id=tenant.id, price_list_id=pl.id, product_id=tea.id, price=D("90")))
    db.session.commit()
    return pl


def test_price_list_only_applies_with_the_module_on(db, tenant, staff, tea, wholesale):
    staff.price_list_id = wholesale.id
    db.session.commit()
    assert checkout(tenant, staff, _cart(_line(tea)), PaymentMode.CASH).lines[0].rate == D("100")
    tenant.set_feature("multi_price_list", True)
    db.session.commit()
    bill = checkout(tenant, staff, _cart(_line(tea)), PaymentMode.CASH)
    assert bill.lines[0].rate == D("90") and bill.price_list_id == wholesale.id


def test_staff_cannot_switch_price_lists_without_price_edit(db, tenant, staff, tea, wholesale):
    tenant.set_feature("multi_price_list", True)
    db.session.commit()
    with pytest.raises(POSValidationError, match="own price list"):
        checkout(tenant, staff, _cart(_line(tea), price_list_id=wholesale.id), PaymentMode.CASH)


def test_switched_off_counter_list_falls_back_to_normal_prices(db, tenant, staff, tea, wholesale):
    tenant.set_feature("multi_price_list", True)
    staff.price_list_id = wholesale.id
    wholesale.is_active = False
    db.session.commit()
    assert checkout(tenant, staff, _cart(_line(tea)), PaymentMode.CASH).lines[0].rate == D("100")


def test_another_clients_price_list_is_never_used(db, tenant, client_admin, tea):
    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR)
    db.session.add(other)
    db.session.flush()
    foreign = PriceList(tenant_id=other.id, name="Theirs")
    db.session.add(foreign)
    tenant.set_feature("multi_price_list", True)
    db.session.commit()
    with pytest.raises(POSValidationError):
        checkout(tenant, client_admin, _cart(_line(tea), price_list_id=foreign.id), PaymentMode.CASH)


def test_price_list_screens_are_404_with_the_module_off(client, db, tenant, client_admin, wholesale):
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get("/products/price-lists").status_code == 404
    tenant.set_feature("multi_price_list", True)
    db.session.commit()
    assert client.get("/products/price-lists").status_code == 200
    resp = client.post(f"/products/price-lists/{wholesale.id}", data={f"price_{wholesale.items[0].product_id}": "85"},
                       follow_redirects=True)
    assert b"1 price(s) updated" in resp.data
    assert PriceListItem.query.one().price == D("85.00")


def test_price_list_of_another_client_is_404(client, db, tenant, client_admin):
    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR)
    db.session.add(other)
    db.session.flush()
    foreign = PriceList(tenant_id=other.id, name="Theirs")
    db.session.add(foreign)
    tenant.set_feature("multi_price_list", True)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get(f"/products/price-lists/{foreign.id}").status_code == 404
    assert client.post(f"/products/price-lists/{foreign.id}/deactivate").status_code == 404
    db.session.refresh(foreign)
    assert foreign.is_active


# --- payments ------------------------------------------------------------------

def test_single_mode_checkout_records_one_payment_like_before(db, tenant, client_admin, tea):
    bill = checkout(tenant, client_admin, _cart(_line(tea)), PaymentMode.UPI)
    assert bill.payment_mode == PaymentMode.UPI
    assert [(p.mode, p.amount) for p in bill.payments] == [(PaymentMode.UPI, D("105.00"))]


def test_split_payment_must_add_up(db, tenant, client_admin, tea):
    with pytest.raises(POSValidationError, match="add up"):
        checkout(tenant, client_admin, _cart(_line(tea)),
                 [PaymentInput(PaymentMode.CASH, D("50")), PaymentInput(PaymentMode.UPI, D("50"))])
    bill = checkout(tenant, client_admin, _cart(_line(tea)),
                    [PaymentInput(PaymentMode.CASH, D("55")), PaymentInput(PaymentMode.UPI, D("50"), "UTR123")])
    assert bill.payment_mode == PaymentMode.MIXED
    assert bill.payment_summary() == "Cash 55.00 + UPI 50.00"
    report = z_report(tenant.id, date.today())
    assert report["by_mode"][PaymentMode.CASH]["total"] == D("55.00")
    assert report["by_mode"][PaymentMode.UPI]["total"] == D("50.00")
    assert report["bill_count"] == 1


def test_only_switched_on_modes_are_accepted(db, tenant, client_admin, tea):
    with pytest.raises(POSValidationError, match="isn't switched on"):
        checkout(tenant, client_admin, _cart(_line(tea)), PaymentMode.WALLET)
    tenant.pos_payment_modes = ["cash", "wallet"]
    db.session.commit()
    assert checkout(tenant, client_admin, _cart(_line(tea)), PaymentMode.WALLET).payment_mode == PaymentMode.WALLET


def test_credit_needs_the_module_and_a_customer(db, tenant, client_admin, tea):
    tenant.pos_payment_modes = ["cash", "credit"]
    db.session.commit()
    with pytest.raises(POSValidationError, match="isn't switched on"):
        checkout(tenant, client_admin, _cart(_line(tea)), PaymentMode.CREDIT)
    tenant.set_feature("credit_sales", True)
    db.session.commit()
    with pytest.raises(POSValidationError, match="needs a customer"):
        checkout(tenant, client_admin, _cart(_line(tea)), PaymentMode.CREDIT)
    ravi = Customer(tenant_id=tenant.id, name="Ravi", phone="9876543210")
    db.session.add(ravi)
    db.session.commit()
    bill = checkout(tenant, client_admin, _cart(_line(tea), customer_id=ravi.id), PaymentMode.CREDIT)
    assert bill.credit_amount == D("105.00")
    assert bill.customer_snapshot["name"] == "Ravi"


def test_cash_tendered_gives_change(db, tenant, client_admin, tea):
    bill = checkout(tenant, client_admin, _cart(_line(tea)), PaymentMode.CASH, cash_tendered=D("200"))
    assert (bill.cash_tendered, bill.change_due) == (D("200.00"), D("95.00"))
    with pytest.raises(POSValidationError, match="less than"):
        checkout(tenant, client_admin, _cart(_line(tea)), PaymentMode.CASH, cash_tendered=D("100"))
    # tendered against a non-cash bill is ignored
    assert checkout(tenant, client_admin, _cart(_line(tea)), PaymentMode.UPI, cash_tendered=D("500")).cash_tendered is None


def _form(product, **extra):
    data = {
        "line_description[]": product.name, "line_hsn[]": product.hsn_or_sac_code, "line_qty[]": "1",
        "line_rate[]": str(product.sale_price), "line_gst_rate[]": str(product.gst_rate),
        "line_product_id[]": str(product.id), "line_unit[]": product.unit, "line_incl[]": "0",
    }
    data.update(extra)
    return data


def test_split_checkout_over_http(client, db, tenant, client_admin, tea):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post("/pos/checkout", data=_form(tea, **{
        "pay_mode[]": ["cash", "card"], "pay_amount[]": ["5", "100"], "pay_ref[]": ["", "slip 42"], "cash_tendered": "10",
    }), headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["change_due"] == 5.0
    bill = db.session.get(POSBill, resp.get_json()["bill_id"])
    assert [(p.mode.value, p.amount, p.reference) for p in bill.payments] == [("cash", D("5.00"), None), ("card", D("100.00"), "slip 42")]


def test_legacy_single_mode_post_still_works(client, db, tenant, client_admin, tea):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post("/pos/checkout", data=_form(tea, payment_mode="cash"), headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 200
    assert POSPayment.query.count() == 1


# --- customers ----------------------------------------------------------------

def test_quick_add_customer_and_b2b_bill(client, db, tenant, staff, tea):
    _login(client, staff.email, "CashierSecret123")
    resp = client.post("/pos/customers/quick-add", data={"name": "Ravi Traders", "phone": "98765 43210", "gstin": "29abcde1234f1z5"})
    data = resp.get_json()
    assert resp.status_code == 200 and data["gstin"] == "29ABCDE1234F1Z5"
    customer = db.session.get(Customer, data["id"])
    assert (customer.tenant_id, customer.state_code, customer.phone) == (tenant.id, "29", "9876543210")
    # same phone again finds the existing customer
    assert client.post("/pos/customers/quick-add", data={"name": "Ravi", "phone": "9876543210"}).get_json()["id"] == customer.id
    assert client.post("/pos/customers/quick-add", data={"name": "X", "gstin": "NIL"}).status_code == 400
    bill = checkout(tenant, staff, _cart(_line(tea), customer_id=customer.id), PaymentMode.CASH)
    assert bill.is_b2b


def test_customer_search_never_shows_another_clients_customers(client, db, tenant, staff):
    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR)
    db.session.add(other)
    db.session.flush()
    db.session.add(Customer(tenant_id=other.id, name="Secret Buyer", phone="9000000001"))
    db.session.add(Customer(tenant_id=tenant.id, name="Our Buyer", phone="9000000002"))
    db.session.commit()
    _login(client, staff.email, "CashierSecret123")
    names = [c["name"] for c in client.get("/pos/customers/search?q=9000").get_json()]
    assert names == ["Our Buyer"]


def test_another_clients_customer_cannot_be_put_on_a_bill(db, tenant, client_admin, tea):
    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR)
    db.session.add(other)
    db.session.flush()
    theirs = Customer(tenant_id=other.id, name="Theirs")
    db.session.add(theirs)
    db.session.commit()
    with pytest.raises(POSValidationError):
        checkout(tenant, client_admin, _cart(_line(tea), customer_id=theirs.id), PaymentMode.CASH)


# --- holding keeps everything ---------------------------------------------------

def test_held_bill_keeps_discounts_customer_and_list(db, tenant, client_admin, tea):
    ravi = Customer(tenant_id=tenant.id, name="Ravi")
    db.session.add(ravi)
    db.session.commit()
    held = hold_cart(tenant, client_admin, _cart(_line(tea, kind="amount", value="10"), customer_id=ravi.id,
                                                 bill_discount_kind="percent", bill_discount_value=D("5")), "Table 4")
    db.session.commit()
    assert held.status == POSBillStatus.HELD and held.customer_id == ravi.id
    assert (held.lines[0].line_discount_kind, held.lines[0].line_discount_value) == ("amount", D("10.00"))
    assert held.bill_discount_kind == "percent"


# --- UPI QR ------------------------------------------------------------------------

def test_upi_qr_only_with_a_upi_id(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get("/pos/upi-qr.svg?amount=105").status_code == 404
    tenant.upi_vpa = "acme@okhdfcbank"
    db.session.commit()
    resp = client.get("/pos/upi-qr.svg?amount=105")
    assert resp.status_code == 200 and resp.mimetype == "image/svg+xml" and b"<svg" in resp.data
    assert client.get("/pos/upi-qr.svg?amount=0").status_code == 400


def test_upi_uri_shape():
    from app.pos.share import upi_uri

    uri = upi_uri("acme@okhdfcbank", "Acme Traders", D("105"), "POS/2026-27/0001")
    assert uri.startswith("upi://pay?pa=acme%40okhdfcbank&pn=Acme%20Traders&am=105.00&cu=INR&tn=POS%2F2026-27%2F0001")


def test_settings_saves_payment_modes_and_validates_upi_id(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    base = {"primary_color": "#1f7a4d", "background_color": "#f6f7f5", "text_color": "#1f2a24", "font_family": "system",
            "default_receipt_format": "3in", "business_type": "other"}
    client.post("/settings/", data={**base, "pos_settings_shown": "1", "pos_payment_modes": ["cash", "wallet"], "upi_vpa": "bad id"})
    db.session.refresh(tenant)
    assert tenant.pos_payment_modes == ["cash", "upi", "card"]  # rejected - invalid UPI ID
    client.post("/settings/", data={**base, "pos_settings_shown": "1", "pos_payment_modes": ["wallet", "cash"], "upi_vpa": "acme@okicici"})
    db.session.refresh(tenant)
    assert tenant.pos_payment_modes == ["cash", "wallet"] and tenant.upi_vpa == "acme@okicici"
    # a post without the counter card keeps what's there
    client.post("/settings/", data=base)
    db.session.refresh(tenant)
    assert tenant.pos_payment_modes == ["cash", "wallet"]


# --- favourites, barcode data -----------------------------------------------------

def test_favourites_row_only_with_the_module_and_pins_first(client, db, tenant, client_admin, tea, rice):
    rice.is_favourite = True
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    page = client.get("/pos/").data.decode()
    assert "const FAVOURITES = [];" in page
    tenant.set_feature("favourites", True)
    db.session.commit()
    checkout(tenant, client_admin, _cart(_line(tea, qty="5")), PaymentMode.CASH)
    db.session.commit()
    page = client.get("/pos/").data.decode()
    favs = page.split("const FAVOURITES = [", 1)[1].split("];", 1)[0]
    assert favs.index('"Rice"') < favs.index('"Tea"')
    assert '"8901234567890"' in page  # barcode available to the scanner


def test_loose_items_are_marked_for_the_keypad(client, db, client_admin, rice):
    _login(client, client_admin.email, "ClientSecret123")
    assert "loose: true" in client.get("/pos/").data.decode()


# --- sharing and copies ----------------------------------------------------------

def _pdf_text(data: bytes) -> str:
    return "".join(page.extract_text() for page in PdfReader(BytesIO(data)).pages)


def test_original_prints_once_then_duplicates(client, db, tenant, client_admin, tea):
    bill = checkout(tenant, client_admin, _cart(_line(tea)), PaymentMode.CASH)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    first = client.get(f"/pos/bills/{bill.id}/receipt.pdf?format=a4&original=1")
    assert "DUPLICATE" not in _pdf_text(first.data)
    again = client.get(f"/pos/bills/{bill.id}/receipt.pdf?format=a4&original=1")
    assert "DUPLICATE COPY" in _pdf_text(again.data)
    assert "DUPLICATE COPY" in _pdf_text(client.get(f"/pos/bills/{bill.id}/receipt.pdf?format=a4").data)


def test_share_link_opens_without_sign_in_and_cant_be_forged(client, db, tenant, client_admin, tea):
    bill = checkout(tenant, client_admin, _cart(_line(tea)), PaymentMode.CASH)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    data = client.get(f"/pos/bills/{bill.id}/share?phone=9876543210").get_json()
    assert data["whatsapp"].startswith("https://wa.me/919876543210?text=")
    client.get("/auth/logout")
    path = data["url"].split("localhost", 1)[1]
    resp = client.get(path)
    assert resp.status_code == 200 and resp.mimetype == "application/pdf"
    assert "CUSTOMER COPY" in _pdf_text(resp.data)
    assert client.get(path.replace(".pdf", "x.pdf")).status_code == 404


def test_share_of_another_clients_bill_is_404(client, db, tenant, tea):
    from app.models.tenant import BillingCycle

    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other.id, state_code="27", state_name="Maharashtra"))
    owner = User(tenant_id=other.id, name="O", email="o@other.example.com", role=UserRole.CLIENT_ADMIN, must_change_password=False)
    owner.set_password("OtherSecret123")
    db.session.add(owner)
    db.session.commit()
    admin = User(tenant_id=tenant.id, name="A", email="a2@acme.example.com", role=UserRole.CLIENT_ADMIN, must_change_password=False)
    admin.set_password("AcmeSecret123")
    db.session.add(admin)
    db.session.commit()
    bill = checkout(tenant, admin, _cart(_line(tea)), PaymentMode.CASH)
    db.session.commit()
    _login(client, owner.email, "OtherSecret123")
    assert client.get(f"/pos/bills/{bill.id}/share").status_code == 404
    assert client.get(f"/pos/bills/{bill.id}/receipt.pdf").status_code == 404
