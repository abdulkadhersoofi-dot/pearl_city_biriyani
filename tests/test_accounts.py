"""Phase 6 accounts: GST on advances (one tax module), customer accounts
(invoices, notes, counter credit, returns, receipts, allocations, cheques,
ageing, credit-limit warning, statements, WhatsApp reminder), supplier
accounts and payment vouchers, the cash & bank book, module flags and
tenant isolation for every new table."""

from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import unquote

import pytest

from app.accounts import books, payables, receivables
from app.accounts import services as acs
from app.expenses import services as es
from app.invoicing.services import InvoiceInput, LineInput, create_invoice, void_invoice
from app.models.accounts import (
    ChequeStatus,
    CustomerReceipt,
    MoneyMode,
    ReceiptAllocation,
    RefundVoucher,
    SupplierPayment,
    VoucherStatus,
)
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.expense import ExpenseCategory, ExpensePaymentMode
from app.models.invoice_series import DocumentType
from app.models.pos_bill import PaymentMode
from app.models.product import Product
from app.models.purchase import Supplier
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.notes.services import NoteInput, create_note
from app.pos.returns import create_return, returnable_lines
from app.pos.services import CartInput, CartLineInput, PaymentInput, checkout, void_bill
from app.utils.advance_gst import advance_portion, compute_advance_gst

D = Decimal
TODAY = date.today()


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _on(db, tenant, *keys):
    for key in keys:
        tenant.set_feature(key, True)
    if "credit_sales" in keys:
        tenant.pos_payment_modes = ["cash", "upi", "card", "credit"]
    db.session.commit()


# --- GST on advances (app.utils.advance_gst) ----------------------------------------------

def test_advance_for_services_intra_state_backs_out_gst():
    g = compute_advance_gst(D("11800"), D("18"), True)
    assert (g["taxable_value"], g["cgst_amount"], g["sgst_amount"], g["igst_amount"]) == (D("10000.00"), D("900.00"), D("900.00"), D("0.00"))


def test_advance_inter_state_is_igst():
    g = compute_advance_gst(D("5600"), D("12"), False)
    assert (g["taxable_value"], g["igst_amount"], g["cgst_amount"]) == (D("5000.00"), D("600.00"), D("0.00"))


def test_advance_for_goods_or_zero_rate_has_no_gst():
    assert compute_advance_gst(D("1000"), D("18"), True, for_services=False)["tax"] == 0
    zero = compute_advance_gst(D("1000"), D("0"), True)
    assert zero["tax"] == 0 and zero["taxable_value"] == D("1000.00")


def test_advance_rounding_half_up_and_odd_paisa_to_sgst():
    g = compute_advance_gst(D("100"), D("5"), True)
    # 100 x 100/105 = 95.238 -> 95.24; tax 4.76 -> CGST 2.38 + SGST 2.38
    assert g["taxable_value"] == D("95.24") and g["cgst_amount"] + g["sgst_amount"] == D("4.76")
    g = compute_advance_gst(D("100.03"), D("18"), True)
    assert g["cgst_amount"] + g["sgst_amount"] == g["tax"] and g["sgst_amount"] - g["cgst_amount"] in (D("0"), D("0.01"), D("-0.01"))


def test_advance_parts_add_back_to_exactly_the_tax_paid():
    whole = compute_advance_gst(D("1000"), D("18"), True)
    tax = {k: whole[k] for k in ("taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "tax")}
    used, parts = D("0"), []
    for piece in (D("333.33"), D("333.33"), D("333.34")):
        parts.append(advance_portion(D("1000"), tax, used, piece, D("18"), True))
        used += piece
    for key in ("taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "tax"):
        assert sum(p[key] for p in parts) == whole[key]
    with pytest.raises(ValueError):
        advance_portion(D("1000"), tax, D("900"), D("200"), D("18"), True)


def test_advance_refund_portion_is_a_credit_against_the_receipt_not_more():
    whole = compute_advance_gst(D("590"), D("18"), False)
    tax = {k: whole[k] for k in ("taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "tax")}
    refund = advance_portion(D("590"), tax, D("0"), D("590"), D("18"), False)
    assert refund["igst_amount"] == whole["igst_amount"] == D("90.00") and refund["taxable_value"] == D("500.00")
    assert advance_portion(D("590"), tax, D("0"), D("0"), D("18"), False)["tax"] == 0


# --- fixtures -------------------------------------------------------------------------------

@pytest.fixture()
def buyer(db, tenant):
    c = Customer(tenant_id=tenant.id, name="Ravi Stores", state_code="27", state_name="Maharashtra", phone="9876543210")
    db.session.add(c)
    db.session.commit()
    return c


@pytest.fixture()
def shirt(db, tenant):
    p = Product(tenant_id=tenant.id, name="Shirt", hsn_or_sac_code="6205", gst_rate=D("12"), unit="PCS",
                sale_price=D("560"), price_includes_tax=True)
    db.session.add(p)
    db.session.commit()
    return p


def _invoice(db, tenant, user, customer, rate="1000", gst="18", on=None, doc=DocumentType.TAX_INVOICE):
    inv = create_invoice(tenant, user, InvoiceInput(
        gstin_id=tenant.gstins.first().id, document_type=doc, customer_id=customer.id,
        place_of_supply_state_code="27", invoice_date=on or TODAY, notes=None,
        lines=[LineInput(description="AMC", hsn_or_sac_code="998719", qty=D("1"), rate=D(rate),
                         discount_percent=D("0"), gst_rate=D(gst), unit="NOS")],
    ))
    db.session.commit()
    return inv


def _credit_bill(db, tenant, user, product, customer, qty="2", credit=None):
    total = D(product.sale_price) * D(qty)
    cart = CartInput(lines=[CartLineInput(description=product.name, hsn_or_sac_code=product.hsn_or_sac_code, qty=D(qty),
                                          rate=product.sale_price, gst_rate=product.gst_rate, product_id=product.id,
                                          unit=product.unit)], customer_id=customer.id)
    credit = D(credit) if credit is not None else total
    payments = [PaymentInput(PaymentMode.CREDIT, credit)]
    if credit < total:
        payments.append(PaymentInput(PaymentMode.CASH, total - credit))
    bill = checkout(tenant, user, cart, payments)
    db.session.commit()
    return bill


def _receipt(db, tenant, user, customer, amount, allocations=(), mode=MoneyMode.CASH, on=None, **kw):
    r = acs.create_receipt(tenant, user, acs.VoucherInput(
        party_id=customer.id, voucher_date=on or TODAY, amount=D(amount), mode=mode,
        allocations=[acs.Allocation(*a) for a in allocations], **kw))
    db.session.commit()
    return r


# --- customer accounts ----------------------------------------------------------------------

def test_invoice_and_partial_receipt_leave_the_rest_outstanding(db, tenant, client_admin, buyer):
    inv = _invoice(db, tenant, client_admin, buyer)  # 1000 + 18% = 1180
    r = _receipt(db, tenant, client_admin, buyer, "500", [("invoice", inv.id, D("500"))])
    assert r.number.startswith("RCT/")
    account = receivables.customer_account(tenant, buyer)
    assert account.balance == D("680.00")
    item = account.items[0]
    assert (item.amount, item.settled, item.outstanding) == (D("1180.00"), D("500.00"), D("680.00"))
    assert account.entries[-1].balance == D("680.00")


def test_receipt_cannot_be_set_against_more_than_is_owed(db, tenant, client_admin, buyer):
    inv = _invoice(db, tenant, client_admin, buyer)
    with pytest.raises(acs.AccountsError):
        _receipt(db, tenant, client_admin, buyer, "2000", [("invoice", inv.id, D("1500"))])
    db.session.rollback()
    with pytest.raises(acs.AccountsError):
        _receipt(db, tenant, client_admin, buyer, "100", [("invoice", inv.id, D("200"))])
    db.session.rollback()
    assert CustomerReceipt.query.count() == 0


def test_on_account_receipt_and_later_allocation(db, tenant, client_admin, buyer):
    inv = _invoice(db, tenant, client_admin, buyer)
    r = _receipt(db, tenant, client_admin, buyer, "1500")
    account = receivables.customer_account(tenant, buyer)
    assert account.on_account == D("1500") and account.balance == D("-320.00")
    acs.allocate_receipt(tenant, r, [acs.Allocation("invoice", inv.id, D("1180"))], TODAY)
    db.session.commit()
    account = receivables.customer_account(tenant, buyer)
    assert account.open_items == [] and account.on_account == D("320.00") and account.balance == D("-320.00")


def test_credit_note_debit_note_and_rcm_invoice_amounts(db, tenant, client_admin, buyer):
    inv = _invoice(db, tenant, client_admin, buyer)
    line = LineInput(description="AMC", hsn_or_sac_code="998719", qty=D("1"), rate=D("100"), discount_percent=D("0"),
                     gst_rate=D("18"), unit="NOS")
    create_note(tenant, client_admin, NoteInput(inv.id, DocumentType.CREDIT_NOTE, TODAY, "Discount", [line]))
    create_note(tenant, client_admin, NoteInput(inv.id, DocumentType.DEBIT_NOTE, TODAY, "Extra visit", [line, line]))
    db.session.commit()
    rcm = _invoice(db, tenant, client_admin, buyer, doc=DocumentType.RCM_INVOICE)
    account = receivables.customer_account(tenant, buyer)
    by_id = {i.id: i for i in account.items}
    assert by_id[inv.id].amount == D("1180.00") - D("118.00") + D("236.00")
    # Under reverse charge the customer pays the tax to the government.
    assert by_id[rcm.id].amount == D("1000.00")


def test_counter_credit_returns_and_voids(db, tenant, client_admin, staff, buyer, shirt):
    _on(db, tenant, "credit_sales")
    bill = _credit_bill(db, tenant, staff, shirt, buyer, qty="2", credit="700")  # 1120 total, 700 on credit
    account = receivables.customer_account(tenant, buyer)
    assert account.balance == D("700.00") and account.items[0].kind == "pos_bill"
    line = returnable_lines(bill)[0].line
    create_return(bill, staff, {line.id: D("1")}, "Size", PaymentMode.CREDIT)
    db.session.commit()
    assert receivables.customer_account(tenant, buyer).balance == D("140.00")  # 700 - 560
    other = _credit_bill(db, tenant, staff, shirt, buyer, qty="1")
    r = _receipt(db, tenant, client_admin, buyer, "560", [("pos_bill", other.id, D("560"))])
    void_bill(other, staff, "Billed twice")
    db.session.commit()
    # The voided bill owes nothing and what was received on it is back on account.
    assert ReceiptAllocation.query.filter_by(receipt_id=r.id).count() == 0
    account = receivables.customer_account(tenant, buyer)
    assert account.on_account == D("560") and account.balance == D("140.00") - D("560")


def test_void_invoice_releases_its_allocations(db, tenant, client_admin, buyer):
    inv = _invoice(db, tenant, client_admin, buyer)
    _receipt(db, tenant, client_admin, buyer, "1180", [("invoice", inv.id, D("1180"))])
    void_invoice(inv, client_admin, "Wrong customer")
    db.session.commit()
    account = receivables.customer_account(tenant, buyer)
    assert account.items == [] and account.on_account == D("1180") and account.balance == D("-1180")


def test_cheque_pending_settles_bounced_reopens_the_bill(db, tenant, client_admin, buyer):
    inv = _invoice(db, tenant, client_admin, buyer)
    r = _receipt(db, tenant, client_admin, buyer, "1180", [("invoice", inv.id, D("1180"))], mode=MoneyMode.CHEQUE,
                 cheque_no="000123", bank_name="SBI")
    assert r.cheque_status == ChequeStatus.PENDING
    assert receivables.customer_account(tenant, buyer).balance == 0
    acs.set_receipt_cheque_status(r, ChequeStatus.BOUNCED, TODAY)
    db.session.commit()
    account = receivables.customer_account(tenant, buyer)
    assert account.balance == D("1180.00") and account.open_items[0].outstanding == D("1180.00")
    assert [e.kind for e in account.entries][-1] == "bounce"
    with pytest.raises(acs.AccountsError):
        acs.set_receipt_cheque_status(r, ChequeStatus.CLEARED, TODAY)
    with pytest.raises(acs.AccountsError):  # a cheque needs its number
        acs.create_receipt(tenant, client_admin, acs.VoucherInput(buyer.id, TODAY, D("10"), MoneyMode.CHEQUE))


def test_opening_balance_books_date_and_ageing(db, tenant, client_admin, buyer):
    tenant.books_opening_date = TODAY - timedelta(days=100)
    buyer.opening_balance = D("5000")
    buyer.opening_balance_date = TODAY - timedelta(days=100)
    db.session.commit()
    _invoice(db, tenant, client_admin, buyer, on=TODAY - timedelta(days=200))  # before the books - left out
    _invoice(db, tenant, client_admin, buyer, rate="500", gst="0", on=TODAY - timedelta(days=45))
    _invoice(db, tenant, client_admin, buyer, rate="100", gst="0", on=TODAY)
    account = receivables.customer_account(tenant, buyer)
    ageing = account.ageing(TODAY)
    assert (ageing["0-30"], ageing["31-60"], ageing["61-90"], ageing["90+"]) == (D("100.00"), D("500.00"), 0, D("5000"))
    assert account.balance == D("5600.00")
    r = _receipt(db, tenant, client_admin, buyer, "5000", [("opening", None, D("5000"))])
    assert receivables.customer_account(tenant, buyer).ageing(TODAY)["90+"] == 0
    rows = receivables.customer_balances(tenant, TODAY)
    assert rows[0]["customer"].id == buyer.id and rows[0]["balance"] == D("600.00")


def test_credit_limit_is_a_warning_only(db, tenant, client_admin, buyer):
    buyer.credit_limit = D("1000")
    db.session.commit()
    assert receivables.credit_warning(tenant, buyer) is None  # module off
    _on(db, tenant, "receivables")
    _invoice(db, tenant, client_admin, buyer)  # 1180 - still issued
    warning = receivables.credit_warning(tenant, buyer)
    assert warning and "credit limit" in warning


def test_credit_warning_flashes_on_invoice_and_pos(db, client, tenant, client_admin, staff, buyer, shirt):
    _on(db, tenant, "receivables", "credit_sales")
    buyer.credit_limit = D("100")
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.post("/invoices/new", data={
        "gstin_id": tenant.gstins.first().id, "document_type": "tax_invoice", "customer_id": str(buyer.id),
        "place_of_supply_state_code": "27", "invoice_date": TODAY.isoformat(),
        "line_description[]": "AMC", "line_hsn[]": "998719", "line_qty[]": "1", "line_rate[]": "1000",
        "line_discount[]": "0", "line_gst_rate[]": "18", "line_product_id[]": "", "line_unit[]": "NOS",
    }, follow_redirects=True)
    assert b"over their credit limit" in page.data


def test_advance_receipt_refund_and_adjustment(db, tenant, client_admin, buyer):
    r = _receipt(db, tenant, client_admin, buyer, "11800", is_advance=True, for_services=True, gst_rate=D("18"))
    assert r.number.startswith("RV/") and (r.cgst_amount, r.sgst_amount, r.taxable_value) == (D("900.00"), D("900.00"), D("10000.00"))
    inv = _invoice(db, tenant, client_admin, buyer, rate="5000")  # 5900
    acs.allocate_receipt(tenant, r, [acs.Allocation("invoice", inv.id, D("5900"))], TODAY)
    db.session.commit()
    refund = acs.create_refund(tenant, client_admin, r, D("5900"), TODAY, MoneyMode.BANK, "UTR1", "Order cut")
    db.session.commit()
    assert refund.number.startswith("RFV/") and refund.total_tax == D("900.00")
    usage = acs.advance_usage(r)
    assert sum(u["tax"] for u in usage) == r.total_tax
    assert receivables.customer_account(tenant, buyer).balance == 0
    with pytest.raises(acs.AccountsError):  # nothing left to refund
        acs.create_refund(tenant, client_admin, r, D("1"), TODAY, MoneyMode.CASH, None, "x")
    with pytest.raises(acs.AccountsError):  # void the refund first
        acs.void_receipt(r, client_admin, "mistake")


def test_unregistered_tenant_advance_carries_no_gst(db, tenant, client_admin, buyer):
    tenant.registration_type = RegistrationType.UNREGISTERED
    db.session.commit()
    r = _receipt(db, tenant, client_admin, buyer, "1180", is_advance=True, for_services=True, gst_rate=D("18"))
    assert r.total_tax == 0 and not r.for_services


def test_void_receipt_needs_reason_and_keeps_the_number(db, tenant, client_admin, buyer):
    r = _receipt(db, tenant, client_admin, buyer, "100")
    with pytest.raises(acs.AccountsError):
        acs.void_receipt(r, client_admin, " ")
    acs.void_receipt(r, client_admin, "Duplicate entry")
    db.session.commit()
    assert db.session.get(CustomerReceipt, r.id).status == VoucherStatus.VOIDED
    assert receivables.customer_account(tenant, buyer).balance == 0
    r2 = _receipt(db, tenant, client_admin, buyer, "100")
    assert r2.number.endswith("0002")


# --- supplier accounts ----------------------------------------------------------------------

@pytest.fixture()
def vendor(db, tenant):
    s = Supplier(tenant_id=tenant.id, name="Metro Wholesale", state_code="27", opening_balance=D("2000"),
                 payment_terms_days=30)
    db.session.add(s)
    db.session.commit()
    return s


def test_supplier_payment_against_opening_and_unpaid_expense(db, tenant, client_admin, vendor):
    es.ensure_categories(tenant.id)
    cat = ExpenseCategory.query.filter_by(tenant_id=tenant.id, name="Rent").one()
    exp = es.create_expense(tenant, client_admin, es.ExpenseInput(
        expense_date=TODAY, category_id=cat.id, amount=D("3000"), supplier_id=vendor.id,
        payment_mode=ExpensePaymentMode.UNPAID))
    db.session.commit()
    account = payables.supplier_account(tenant, vendor)
    assert account.balance == D("5000.00")
    pay = acs.create_payment(tenant, client_admin, acs.VoucherInput(
        vendor.id, TODAY, D("4000"), MoneyMode.BANK, reference="UTR9",
        allocations=[acs.Allocation("opening", None, D("2000")), acs.Allocation("expense", exp.id, D("1500"))]))
    db.session.commit()
    assert pay.number.startswith("PAY/")
    account = payables.supplier_account(tenant, vendor)
    assert account.balance == D("1000.00") and account.on_account == D("500")
    es.void_expense(exp, client_admin, "Entered twice")
    db.session.commit()
    account = payables.supplier_account(tenant, vendor)
    assert account.balance == D("-2000.00") and account.on_account == D("2000")
    with pytest.raises(acs.AccountsError):
        acs.allocate_payment(tenant, pay, [acs.Allocation("expense", exp.id, D("1"))], TODAY)


def test_supplier_cheque_bounce(db, tenant, client_admin, vendor):
    pay = acs.create_payment(tenant, client_admin, acs.VoucherInput(
        vendor.id, TODAY, D("2000"), MoneyMode.CHEQUE, cheque_no="77",
        allocations=[acs.Allocation("opening", None, D("2000"))]))
    db.session.commit()
    assert payables.supplier_account(tenant, vendor).balance == 0
    acs.set_payment_cheque_status(pay, ChequeStatus.BOUNCED, TODAY)
    db.session.commit()
    assert payables.supplier_account(tenant, vendor).balance == D("2000")


# --- cash & bank book -----------------------------------------------------------------------

def test_cash_and_bank_book(db, tenant, client_admin, staff, buyer, vendor, shirt):
    _on(db, tenant, "credit_sales")
    tenant.cash_opening_balance = D("1000")
    tenant.bank_opening_balance = D("50000")
    db.session.commit()
    cart = CartInput(lines=[CartLineInput(description="Shirt", hsn_or_sac_code="6205", qty=D("1"), rate=D("560"),
                                          gst_rate=D("12"), product_id=shirt.id, unit="PCS")])
    checkout(tenant, staff, cart, [PaymentInput(PaymentMode.CASH, D("300")), PaymentInput(PaymentMode.UPI, D("260"))])
    db.session.commit()
    _receipt(db, tenant, client_admin, buyer, "400")
    chq = _receipt(db, tenant, client_admin, buyer, "999", mode=MoneyMode.CHEQUE, cheque_no="1")
    acs.create_payment(tenant, client_admin, acs.VoucherInput(vendor.id, TODAY, D("250"), MoneyMode.CASH))
    db.session.commit()
    cash = books.book(tenant, "cash", TODAY, TODAY)
    assert cash["opening"] == D("1000") and cash["total_in"] == D("700") and cash["total_out"] == D("250")
    assert cash["closing"] == D("1450")
    bank = books.book(tenant, "bank", TODAY, TODAY)
    assert bank["total_in"] == D("260") and bank["closing"] == D("50260")  # the cheque hasn't cleared
    acs.set_receipt_cheque_status(chq, ChequeStatus.CLEARED, TODAY)
    db.session.commit()
    assert books.book(tenant, "bank", TODAY, TODAY)["closing"] == D("51259")


# --- routes, flags, isolation ---------------------------------------------------------------

ROUTES = ["/accounts/receivables", "/accounts/receipts", "/accounts/receipts/new", "/accounts/payables",
          "/accounts/payments", "/accounts/payments/new", "/accounts/cash-book", "/accounts/bank-book"]


def test_flags_off_means_404_and_no_nav(db, client, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for url in ROUTES:
        assert client.get(url).status_code == 404, url
    assert b"/accounts/" not in client.get("/invoices/").data
    form = client.get("/customers/new").data
    assert b"credit_limit" not in form


def test_pages_render_with_flags_on_and_staff_is_refused(db, client, tenant, client_admin, staff, buyer, vendor):
    _on(db, tenant, "receivables", "payables", "cash_bank_book")
    inv = _invoice(db, tenant, client_admin, buyer)
    r = _receipt(db, tenant, client_admin, buyer, "100", [("invoice", inv.id, D("100"))])
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    assert client.get("/accounts/receivables").status_code == 403
    client.get("/auth/logout")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for url in ROUTES + [f"/accounts/customers/{buyer.id}", f"/accounts/receipts/{r.id}",
                         f"/accounts/receipts/new?customer_id={buyer.id}", f"/accounts/receipts/new?customer_id={buyer.id}&advance=1",
                         f"/accounts/suppliers/{vendor.id}", f"/accounts/payments/new?supplier_id={vendor.id}",
                         f"/customers/{buyer.id}/edit"]:
        assert client.get(url).status_code == 200, url
    pdf = client.get(f"/accounts/receipts/{r.id}/pdf")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
    stmt = client.get(f"/accounts/customers/{buyer.id}/statement.pdf")
    assert stmt.status_code == 200 and stmt.data[:4] == b"%PDF"
    assert b"credit_limit" in client.get(f"/customers/{buyer.id}/edit").data


def test_receipt_form_posts_allocations_and_audits(db, client, tenant, client_admin, buyer):
    _on(db, tenant, "receivables")
    inv = _invoice(db, tenant, client_admin, buyer)
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.post("/accounts/receipts/new", data={
        "customer_id": buyer.id, "voucher_date": TODAY.isoformat(), "amount": "1000", "mode": "upi",
        "reference": "UPI123", f"alloc-invoice-{inv.id}": "800",
    }, follow_redirects=True)
    assert b"Receipt RCT/" in page.data
    receipt = CustomerReceipt.query.one()
    assert services_used(receipt) == D("800")
    assert AuditLog.query.filter_by(action="receipt_created", entity_id=receipt.id).count() == 1


def services_used(receipt):
    return acs.receipt_used(receipt)


def test_whatsapp_reminder_and_shared_statement(db, client, tenant, client_admin, buyer):
    _on(db, tenant, "receivables")
    _invoice(db, tenant, client_admin, buyer)
    _login(client, "admin@acme.example.com", "ClientSecret123")
    res = client.get(f"/accounts/customers/{buyer.id}/remind")
    assert res.status_code == 302 and res.location.startswith("https://wa.me/919876543210?text=")
    text = unquote(res.location.split("text=", 1)[1])
    assert "1,180.00" in text
    link = text.split("Statement: ", 1)[1]
    client.get("/auth/logout")
    shared = client.get(link.replace("http://localhost", ""))
    assert shared.status_code == 200 and shared.data[:4] == b"%PDF"
    assert client.get("/accounts/shared/statement/forged.pdf").status_code == 404


@pytest.fixture()
def other(db, super_admin):
    t = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
               billing_cycle=BillingCycle.MONTHLY)
    db.session.add(t)
    db.session.flush()
    db.session.add(Gstin(tenant_id=t.id, gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka", is_primary=True))
    admin = User(tenant_id=t.id, name="Other", email="other@x.example.com", role=UserRole.CLIENT_ADMIN, must_change_password=False)
    admin.set_password("OtherSecret123")
    db.session.add(admin)
    t.set_feature("receivables", True)
    t.set_feature("payables", True)
    db.session.commit()
    customer = Customer(tenant_id=t.id, name="Their customer", state_code="29")
    supplier = Supplier(tenant_id=t.id, name="Their supplier", state_code="29")
    db.session.add_all([customer, supplier])
    db.session.commit()
    return {"tenant": t, "admin": admin, "customer": customer, "supplier": supplier}


def test_tenant_isolation_for_receipts_refunds_payments_and_allocations(db, client, tenant, client_admin, buyer, vendor, other):
    _on(db, tenant, "receivables", "payables")
    inv = _invoice(db, tenant, client_admin, buyer)
    r = _receipt(db, tenant, client_admin, buyer, "1180", is_advance=True)
    refund = acs.create_refund(tenant, client_admin, r, D("100"), TODAY, MoneyMode.CASH, None, "Partly cancelled")
    pay = acs.create_payment(tenant, client_admin, acs.VoucherInput(vendor.id, TODAY, D("10"), MoneyMode.CASH))
    db.session.commit()
    ot = other["tenant"]
    # Their services refuse our ids.
    with pytest.raises(acs.AccountsError):
        acs.create_receipt(ot, other["admin"], acs.VoucherInput(buyer.id, TODAY, D("1"), MoneyMode.CASH))
    with pytest.raises(acs.AccountsError):
        acs.create_payment(ot, other["admin"], acs.VoucherInput(vendor.id, TODAY, D("1"), MoneyMode.CASH))
    theirs = acs.create_receipt(ot, other["admin"], acs.VoucherInput(other["customer"].id, TODAY, D("50"), MoneyMode.CASH))
    db.session.commit()
    with pytest.raises(acs.AccountsError):  # our invoice isn't on their customer's account
        acs.allocate_receipt(ot, theirs, [acs.Allocation("invoice", inv.id, D("10"))], TODAY)
    db.session.rollback()
    # Their login can't read or touch ours by guessing ids.
    _login(client, "other@x.example.com", "OtherSecret123")
    for url in (f"/accounts/receipts/{r.id}", f"/accounts/receipts/{r.id}/pdf", f"/accounts/refunds/{refund.id}",
                f"/accounts/payments/{pay.id}", f"/accounts/customers/{buyer.id}", f"/accounts/suppliers/{vendor.id}",
                f"/accounts/customers/{buyer.id}/statement.pdf", f"/accounts/customers/{buyer.id}/remind"):
        assert client.get(url).status_code == 404, url
    for url in (f"/accounts/receipts/{r.id}/void", f"/accounts/refunds/{refund.id}/void", f"/accounts/payments/{pay.id}/void",
                f"/accounts/receipts/{r.id}/allocate", f"/accounts/receipts/{r.id}/cheque"):
        assert client.post(url, data={"reason": "x", "status": "bounced"}).status_code == 404, url
    assert client.post(f"/accounts/receipts/{theirs.id}/allocate", data={f"alloc-invoice-{inv.id}": "10"},
                       follow_redirects=True).status_code == 200
    assert ReceiptAllocation.query.filter_by(invoice_id=inv.id).count() == 0
    page = client.get("/accounts/receipts").data
    assert r.number.encode() not in page
    assert db.session.get(CustomerReceipt, r.id).status == VoucherStatus.POSTED
    assert db.session.get(RefundVoucher, refund.id).status == VoucherStatus.POSTED
    assert db.session.get(SupplierPayment, pay.id).status == VoucherStatus.POSTED


# --- advances in the GST returns ------------------------------------------------------------

def test_gstr1_advances_and_3b_net(db, tenant, client_admin, buyer):
    from app.reports.gstr import gstr1_data, gstr3b_data

    gstin = tenant.gstins.first()
    period = TODAY.strftime("%Y-%m")
    before = gstr3b_data(tenant, gstin, period)
    assert "advances" not in before and "advances" not in gstr1_data(tenant, gstin, period)
    _on(db, tenant, "receivables")
    r = _receipt(db, tenant, client_admin, buyer, "11800", is_advance=True, for_services=True, gst_rate=D("18"))
    _receipt(db, tenant, client_admin, buyer, "5000", is_advance=True, for_services=False)  # goods: nothing
    inv = _invoice(db, tenant, client_admin, buyer, rate="5000")  # 5900
    acs.allocate_receipt(tenant, r, [acs.Allocation("invoice", inv.id, D("5900"))], TODAY)
    db.session.commit()
    g1 = gstr1_data(tenant, gstin, period)
    adv = g1["advances"]
    assert adv["received_totals"]["taxable_value"] == D("10000.00") and adv["received_totals"]["cgst"] == D("900.00")
    assert adv["adjusted_totals"]["taxable_value"] == D("5000.00") and adv["adjusted_totals"]["cgst"] == D("450.00")
    labels = {s.label for s in g1["document_summary"]}
    assert "Receipt Voucher" in labels
    g3 = gstr3b_data(tenant, gstin, period)
    # invoice 5000 + net advance 5000; tax 900 + 450*2 net
    assert g3["outward_taxable"]["taxable_value"] == D("10000.00")
    assert g3["gross_tax_payable"] == D("900.00") + D("900.00")
