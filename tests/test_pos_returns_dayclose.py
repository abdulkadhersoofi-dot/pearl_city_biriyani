"""Item-wise sales returns / exchanges, same-day voids, and day open /
close per counter - and how they show up in stock, reports and GSTR."""

from datetime import date
from decimal import Decimal

import pytest

from app.models.audit_log import AuditLog
from app.models.cash_session import CashSession
from app.models.customer import Customer
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus
from app.models.pos_return import POSReturn, POSReturnStatus
from app.models.product import Product
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.pos.returns import create_return, returnable_lines, void_return
from app.pos.services import CartInput, CartLineInput, PaymentInput, POSValidationError, checkout, refund_bill, void_bill, z_report
from app.pos.sessions import add_movement, close_session, open_session, session_activity
from app.pos.stock import branch_stock_status
from app.reports.gstr import gstr1_data, gstr3b_data
from app.reports.items import item_sales_data
from app.reports.sales import sales_report_data
from app.utils.financial_year import current_financial_year

D = Decimal


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def shirt(db, tenant):
    p = Product(tenant_id=tenant.id, name="Shirt", hsn_or_sac_code="6205", gst_rate=D("12"), unit="PCS",
                sale_price=D("500"), price_includes_tax=True)
    db.session.add(p)
    db.session.commit()
    return p


def _cart(product, qty="3", **kwargs):
    return CartInput(lines=[CartLineInput(
        description=product.name, hsn_or_sac_code=product.hsn_or_sac_code, qty=D(qty), rate=product.sale_price,
        gst_rate=product.gst_rate, product_id=product.id, unit=product.unit,
    )], **kwargs)


@pytest.fixture()
def sold(db, tenant, staff, shirt):
    bill = checkout(tenant, staff, _cart(shirt), PaymentMode.CASH)
    db.session.commit()
    return bill


# --- returns -------------------------------------------------------------------

def test_part_return_gets_its_own_number_and_leaves_the_bill_alone(db, tenant, staff, sold):
    line = sold.lines[0]
    ret = create_return(sold, staff, {line.id: D("1")}, "Wrong size", PaymentMode.CASH)
    db.session.commit()
    assert ret.return_number == f"SR/{current_financial_year()}/0001"
    assert ret.grand_total == D("500.00")
    assert ret.total_cgst + ret.total_sgst + ret.total_taxable_value == D("500.00")
    db.session.refresh(sold)
    assert sold.status == POSBillStatus.COMPLETED and sold.grand_total == D("1500.00")
    second = create_return(sold, staff, {line.id: D("2")}, "Didn't like", PaymentMode.CASH)
    assert second.return_number.endswith("/0002")
    assert ret.total_taxable_value + second.total_taxable_value == line.taxable_value
    assert ret.total_cgst + second.total_cgst == line.cgst_amount


def test_cannot_return_more_than_was_sold(db, staff, sold):
    line = sold.lines[0]
    create_return(sold, staff, {line.id: D("2")}, "x", PaymentMode.CASH)
    with pytest.raises(POSValidationError, match="Only 1"):
        create_return(sold, staff, {line.id: D("2")}, "x", PaymentMode.CASH)
    assert returnable_lines(sold)[0].remaining_qty == D("1")


def test_return_needs_a_reason_quantities_and_a_valid_mode(db, staff, sold):
    line = sold.lines[0]
    with pytest.raises(POSValidationError, match="reason"):
        create_return(sold, staff, {line.id: D("1")}, " ", PaymentMode.CASH)
    with pytest.raises(POSValidationError, match="at least one"):
        create_return(sold, staff, {line.id: D("0")}, "x", PaymentMode.CASH)
    with pytest.raises(POSValidationError, match="Pick how"):
        create_return(sold, staff, {line.id: D("1")}, "x", PaymentMode.CREDIT)  # bill wasn't on credit
    with pytest.raises(POSValidationError, match="isn't on this bill"):
        create_return(sold, staff, {999999: D("1")}, "x", PaymentMode.CASH)


def test_returned_goods_go_back_into_the_branch_stock_count(db, tenant, staff, shirt, sold):
    before = {s.product_id: s.sold for s in branch_stock_status(tenant.id, staff.id)}[shirt.id]
    create_return(sold, staff, {sold.lines[0].id: D("1")}, "x", PaymentMode.CASH)
    db.session.commit()
    after = {s.product_id: s.sold for s in branch_stock_status(tenant.id, staff.id)}[shirt.id]
    assert (before, after) == (D("3"), D("2"))


def test_returns_reduce_sales_items_and_gstr(db, tenant, staff, sold):
    ret = create_return(sold, staff, {sold.lines[0].id: D("1")}, "x", PaymentMode.CASH)
    db.session.commit()
    today = date.today()
    sales = sales_report_data(tenant, today, today)
    assert sales["totals"]["total"] == D("1000.00")
    assert any(r.doc_type == "POS Sales Return" and r.sign == -1 for r in sales["rows"])
    items = item_sales_data(tenant, today, today)
    assert items["rows"][0].qty == D("2")
    gstin = tenant.gstins.first()
    period = today.strftime("%Y-%m")
    g1 = gstr1_data(tenant, gstin, period)
    assert g1["totals"]["total"] == D("1000.00")
    assert g1["credit_debit_notes"] == []  # walk-in: B2C, reduces the B2C bucket
    assert any(s.label == "POS Sales Return" and s.total_count == 1 for s in g1["document_summary"])
    g3 = gstr3b_data(tenant, gstin, period)
    assert g3["outward_taxable"]["taxable_value"] == sold.total_taxable_value - ret.total_taxable_value


def test_b2b_return_is_reported_like_a_credit_note(db, tenant, staff, shirt):
    buyer = Customer(tenant_id=tenant.id, name="Ravi Traders", gstin="29ABCDE1234F1Z5", state_code="29")
    db.session.add(buyer)
    db.session.commit()
    bill = checkout(tenant, staff, _cart(shirt, customer_id=buyer.id), PaymentMode.CASH)
    create_return(bill, staff, {bill.lines[0].id: D("1")}, "x", PaymentMode.CASH)
    db.session.commit()
    g1 = gstr1_data(tenant, tenant.gstins.first(), date.today().strftime("%Y-%m"))
    notes = g1["credit_debit_notes"]
    assert len(notes) == 1 and notes[0]["document_type"] == "pos_return" and notes[0]["customer_gstin"] == "29ABCDE1234F1Z5"
    docs = g1["b2b"][0]["documents"]
    assert sum((d["taxable_value"] for d in docs), D("0")) == bill.total_taxable_value - notes[0]["taxable_value"]


def test_exchange_credit_pays_for_the_next_bill_once(db, tenant, staff, shirt, sold):
    ret = create_return(sold, staff, {sold.lines[0].id: D("1")}, "Size swap", PaymentMode.EXCHANGE)
    db.session.commit()
    assert ret.exchange_balance == D("500.00")
    new_bill = checkout(tenant, staff, _cart(shirt, qty="2"),
                        [PaymentInput(PaymentMode.EXCHANGE, D("500"), return_number=ret.return_number),
                         PaymentInput(PaymentMode.CASH, D("500"))])
    db.session.commit()
    assert ret.exchange_used == D("500.00") and ret.exchange_balance == 0
    assert new_bill.payment_mode == PaymentMode.MIXED
    with pytest.raises(POSValidationError, match="Only Rs. 0.00"):
        checkout(tenant, staff, _cart(shirt, qty="1"), [PaymentInput(PaymentMode.EXCHANGE, D("500"), return_number=ret.return_number)])


def test_exchange_slip_of_another_client_is_refused(db, tenant, staff, shirt):
    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR)
    db.session.add(other)
    db.session.flush()
    with pytest.raises(POSValidationError, match="isn't an exchange credit"):
        checkout(tenant, staff, _cart(shirt, qty="1"), [PaymentInput(PaymentMode.EXCHANGE, D("500"), return_number="SR/2026-27/0001")])


def test_void_return_same_day_unless_its_credit_was_used(db, tenant, staff, shirt, sold):
    ret = create_return(sold, staff, {sold.lines[0].id: D("1")}, "x", PaymentMode.EXCHANGE)
    checkout(tenant, staff, _cart(shirt, qty="1"), [PaymentInput(PaymentMode.EXCHANGE, D("500"), return_number=ret.return_number)])
    with pytest.raises(POSValidationError, match="already used"):
        void_return(ret, staff, "mistake")
    ret2 = create_return(sold, staff, {sold.lines[0].id: D("1")}, "x", PaymentMode.CASH)
    void_return(ret2, staff, "keyed twice")
    assert ret2.status == POSReturnStatus.VOIDED
    assert returnable_lines(sold)[0].remaining_qty == D("2")


def test_amount_refund_and_item_returns_dont_mix(db, staff, sold):
    create_return(sold, staff, {sold.lines[0].id: D("1")}, "x", PaymentMode.CASH)
    with pytest.raises(POSValidationError, match="already returned"):
        refund_bill(sold, staff, D("100"), "goodwill")


def test_return_over_http_needs_refund_permission_and_is_audited(client, db, tenant, staff, sold):
    _login(client, staff.email, "CashierSecret123")
    line = sold.lines[0]
    resp = client.post(f"/pos/bills/{sold.id}/return", data={f"qty_{line.id}": "1", "refund_mode": "cash", "reason": "Torn"})
    assert resp.status_code == 302
    assert AuditLog.query.filter_by(action="pos_return_created").count() == 1
    staff.can_refund = False
    db.session.commit()
    assert client.get(f"/pos/bills/{sold.id}/return").status_code == 403


def test_another_clients_bill_cannot_be_returned(client, db, tenant, sold):
    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    owner = User(tenant_id=other.id, name="O", email="o@other.example.com", role=UserRole.CLIENT_ADMIN, must_change_password=False)
    owner.set_password("OtherSecret123")
    db.session.add(owner)
    db.session.commit()
    _login(client, owner.email, "OtherSecret123")
    assert client.get(f"/pos/bills/{sold.id}/return").status_code == 404
    resp = client.post(f"/pos/bills/{sold.id}/return", data={f"qty_{sold.lines[0].id}": "1", "refund_mode": "cash", "reason": "x"})
    assert resp.status_code == 404
    assert POSReturn.query.count() == 0


# --- voids -----------------------------------------------------------------------

def test_void_needs_permission_and_a_reason_and_drops_out_of_sales(client, db, tenant, staff, sold):
    _login(client, staff.email, "CashierSecret123")
    assert client.post(f"/pos/bills/{sold.id}/void", data={"reason": "wrong item"}).status_code == 403
    staff.can_void = True
    db.session.commit()
    client.post(f"/pos/bills/{sold.id}/void", data={"reason": "wrong item"})
    db.session.refresh(sold)
    assert sold.status == POSBillStatus.VOIDED and sold.void_reason == "wrong item"
    report = z_report(tenant.id, date.today())
    assert report["bill_count"] == 0 and report["voided_count"] == 1
    g1 = gstr1_data(tenant, tenant.gstins.first(), date.today().strftime("%Y-%m"))
    pos = next(s for s in g1["document_summary"] if s.label == "POS Bill")
    assert (pos.total_count, pos.cancelled_count) == (1, 1)
    assert g1["totals"]["total"] == 0


def test_void_refused_with_returns_or_without_reason(db, staff, sold):
    with pytest.raises(POSValidationError, match="reason"):
        void_bill(sold, staff, "")
    create_return(sold, staff, {sold.lines[0].id: D("1")}, "x", PaymentMode.CASH)
    with pytest.raises(POSValidationError, match="already returned"):
        void_bill(sold, staff, "oops")


def test_only_todays_bill_can_be_voided(db, staff, sold):
    with pytest.raises(POSValidationError, match="today"):
        void_bill(sold, staff, "late", today=date(2000, 1, 1))


# --- day open / close --------------------------------------------------------------

def test_day_close_off_means_no_session_and_404_routes(client, db, tenant, staff, shirt):
    bill = checkout(tenant, staff, _cart(shirt, qty="1"), PaymentMode.CASH)
    assert bill.cash_session_id is None
    _login(client, staff.email, "CashierSecret123")
    assert client.get("/pos/day/close").status_code == 404
    assert client.post("/pos/day/open", data={"opening_cash": "100"}).status_code == 404


def test_billing_needs_an_open_day_when_switched_on(client, db, tenant, staff, shirt):
    tenant.set_feature("day_close", True)
    db.session.commit()
    _login(client, staff.email, "CashierSecret123")
    data = {"payment_mode": "cash", "line_description[]": "Shirt", "line_hsn[]": "6205", "line_qty[]": "1",
            "line_rate[]": "500", "line_gst_rate[]": "12", "line_product_id[]": str(shirt.id), "line_unit[]": "PCS"}
    resp = client.post("/pos/checkout", data=data, headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 400 and "Open the day" in resp.get_json()["error"]
    client.post("/pos/day/open", data={"opening_cash": "1000"})
    resp = client.post("/pos/checkout", data=data, headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 200
    bill = db.session.get(POSBill, resp.get_json()["bill_id"])
    assert bill.cash_session_id == CashSession.query.one().id


def test_close_counts_the_drawer_and_locks_the_day(db, tenant, staff, shirt):
    tenant.set_feature("day_close", True)
    db.session.commit()
    session = open_session(tenant, staff, "1000")
    cash_bill = checkout(tenant, staff, _cart(shirt, qty="2"), PaymentMode.CASH, cash_session=session)
    split_bill = checkout(tenant, staff, _cart(shirt, qty="1"),
                          [PaymentInput(PaymentMode.CASH, D("200")), PaymentInput(PaymentMode.UPI, D("300"))], cash_session=session)
    create_return(cash_bill, staff, {cash_bill.lines[0].id: D("1")}, "x", PaymentMode.CASH, cash_session=session)
    add_movement(session, staff, "out", "50", "Tea")
    add_movement(session, staff, "in", "20", "Float")
    activity = session_activity(session)
    # 1000 + 1000 + 200 - 500 (return) - 50 + 20
    assert activity["expected_cash"] == D("1670.00")
    assert activity["by_mode"][PaymentMode.UPI]["total"] == D("300.00")
    assert activity["total_returns"] == D("500.00")
    close_session(session, staff, "1660", "short 10")
    db.session.commit()
    assert (session.expected_cash, session.counted_cash, session.difference) == (D("1670.00"), D("1660.00"), D("-10.00"))
    with pytest.raises(POSValidationError, match="day is closed"):
        void_bill(split_bill, staff, "too late")
    with pytest.raises(POSValidationError, match="already closed"):
        close_session(session, staff, "0")


def test_one_open_day_per_counter(db, tenant, staff):
    tenant.set_feature("day_close", True)
    open_session(tenant, staff, "0")
    with pytest.raises(POSValidationError, match="already open"):
        open_session(tenant, staff, "0")


def test_refund_after_close_lands_in_the_new_day(db, tenant, staff, shirt):
    tenant.set_feature("day_close", True)
    day1 = open_session(tenant, staff, "0")
    bill = checkout(tenant, staff, _cart(shirt, qty="1"), PaymentMode.CASH, cash_session=day1)
    close_session(day1, staff, "500")
    day2 = open_session(tenant, staff, "500")
    refund_bill(bill, staff, D("500"), "faulty", cash_session=day2)
    assert session_activity(day2)["expected_cash"] == D("0.00")
    assert session_activity(day1)["total_sales"] == D("500.00")


def test_counter_day_page_is_tenant_and_counter_scoped(client, db, tenant, staff, client_admin):
    tenant.set_feature("day_close", True)
    other_staff = User(tenant_id=tenant.id, name="Counter 2", email="c2@acme.example.com", role=UserRole.STAFF, must_change_password=False)
    other_staff.set_password("CounterSecret123")
    db.session.add(other_staff)
    db.session.commit()
    session = open_session(tenant, other_staff, "0")
    db.session.commit()
    _login(client, staff.email, "CashierSecret123")
    assert client.get(f"/pos/day/{session.id}").status_code == 403
    client.get("/auth/logout")
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get(f"/pos/day/{session.id}").status_code == 200

    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY,
                   features={"day_close": True})
    db.session.add(other)
    db.session.flush()
    owner = User(tenant_id=other.id, name="O", email="o@other.example.com", role=UserRole.CLIENT_ADMIN, must_change_password=False)
    owner.set_password("OtherSecret123")
    db.session.add(owner)
    db.session.commit()
    client.get("/auth/logout")
    _login(client, owner.email, "OtherSecret123")
    assert client.get(f"/pos/day/{session.id}").status_code == 404


def test_existing_style_z_report_is_unchanged_without_day_close(db, tenant, staff, shirt):
    a = checkout(tenant, staff, _cart(shirt, qty="1"), PaymentMode.CASH)
    b = checkout(tenant, staff, _cart(shirt, qty="1"), PaymentMode.CARD)
    refund_bill(b, staff, b.grand_total, "x")
    report = z_report(tenant.id, date.today())
    assert report["bill_count"] == 1 and report["total_sales"] == a.grand_total
    assert report["total_refunds"] == b.grand_total
