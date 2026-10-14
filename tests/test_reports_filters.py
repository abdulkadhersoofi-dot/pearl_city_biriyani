from datetime import date, timedelta
from decimal import Decimal

from app.invoicing.services import InvoiceInput, LineInput, create_invoice
from app.models.invoice_series import DocumentType
from app.models.pos_bill import PaymentMode
from app.models.product import Product
from app.models.user import User, UserRole
from app.pos.services import CartInput, CartLineInput, checkout
from app.reports.filters import parse_report_filters
from app.reports.items import item_sales_data
from app.reports.sales import sales_report_data
from app.reports.stock import stock_report_data


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _args(**kwargs):
    # werkzeug MultiDict-like object good enough for parse_report_filters:
    # it only calls .get() and .getlist().
    from werkzeug.datastructures import MultiDict

    return MultiDict(kwargs)


# --- parse_report_filters ----------------------------------------------------

def test_default_mode_is_a_30_day_range_ending_today():
    filters = parse_report_filters(_args())
    assert filters.mode == "range"
    assert filters.end == date.today()
    assert filters.start == date.today() - timedelta(days=30)
    assert filters.branch_ids is None


def test_day_mode_pins_start_and_end_to_the_same_date():
    filters = parse_report_filters(_args(mode="day", date="2026-10-05"))
    assert filters.mode == "day"
    assert filters.start == filters.end == date(2026, 10, 5)
    assert filters.day_value == "2026-10-05"


def test_month_mode_spans_the_whole_calendar_month():
    filters = parse_report_filters(_args(mode="month", month="2026-02"))
    assert filters.start == date(2026, 2, 1)
    assert filters.end == date(2026, 2, 28)
    assert filters.month_value == "2026-02"


def test_range_mode_swaps_a_backwards_range():
    filters = parse_report_filters(_args(mode="range", start="2026-10-10", end="2026-10-01"))
    assert filters.start == date(2026, 10, 1)
    assert filters.end == date(2026, 10, 10)


def test_branch_ids_parsed_from_repeated_query_param():
    md = _args(mode="range")
    md_list = [("branch_id", "3"), ("branch_id", "5"), ("branch_id", "not-a-number")]
    from werkzeug.datastructures import MultiDict

    args = MultiDict(list(md.items()) + md_list)
    filters = parse_report_filters(args)
    assert filters.branch_ids == [3, 5]


def test_no_branch_id_means_all_branches():
    filters = parse_report_filters(_args())
    assert filters.branch_ids is None


# --- report data: branch filtering -------------------------------------------

def _branch(db, tenant, name, email):
    user = User(tenant_id=tenant.id, name=name, email=email, role=UserRole.STAFF, must_change_password=False)
    user.set_password("BranchSecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _product(db, tenant, name="Chicken Biriyani"):
    product = Product(
        tenant_id=tenant.id, name=name, hsn_or_sac_code="996331", gst_rate=Decimal("18"), unit="plate",
        sale_price=Decimal("150"),
    )
    db.session.add(product)
    db.session.commit()
    return product


def test_sales_report_branch_filter_only_counts_that_branchs_pos_bills(db, tenant, staff):
    biriyani = _product(db, tenant)
    other_branch = _branch(db, tenant, "Other Branch", "other@acme.example.com")

    checkout(
        tenant, staff,
        CartInput(lines=[CartLineInput(description=biriyani.name, hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("150"), gst_rate=Decimal("18"), unit="plate", product_id=biriyani.id)]),
        PaymentMode.CASH,
    )
    checkout(
        tenant, other_branch,
        CartInput(lines=[CartLineInput(description=biriyani.name, hsn_or_sac_code="996331", qty=Decimal("2"), rate=Decimal("150"), gst_rate=Decimal("18"), unit="plate", product_id=biriyani.id)]),
        PaymentMode.CASH,
    )
    db.session.commit()

    today = date.today()
    all_data = sales_report_data(tenant, today, today)
    assert all_data["pos_bill_count"] == 2

    staff_only = sales_report_data(tenant, today, today, branch_ids=[staff.id])
    assert staff_only["pos_bill_count"] == 1
    assert staff_only["rows"][0].customer_name == "Walk-in"
    assert staff_only["totals"]["total"] == Decimal("177.00")  # 150 + 18% GST


def test_stock_report_branch_filter_separates_allocation_and_sales_per_branch(db, tenant, client_admin, staff):
    biriyani = _product(db, tenant)
    other_branch = _branch(db, tenant, "Other Branch", "other2@acme.example.com")

    gstin = tenant.gstins.first()
    create_invoice(
        tenant, client_admin,
        InvoiceInput(
            gstin_id=gstin.id, document_type=DocumentType.TAX_INVOICE, place_of_supply_state_code=gstin.state_code,
            invoice_date=date.today(), notes=None, customer_id=None, branch_user_id=staff.id,
            lines=[LineInput(description=biriyani.name, hsn_or_sac_code="996331", qty=Decimal("20"), rate=Decimal("150"), discount_percent=Decimal("0"), gst_rate=Decimal("18"), product_id=biriyani.id)],
        ),
    )
    create_invoice(
        tenant, client_admin,
        InvoiceInput(
            gstin_id=gstin.id, document_type=DocumentType.TAX_INVOICE, place_of_supply_state_code=gstin.state_code,
            invoice_date=date.today(), notes=None, customer_id=None, branch_user_id=other_branch.id,
            lines=[LineInput(description=biriyani.name, hsn_or_sac_code="996331", qty=Decimal("5"), rate=Decimal("150"), discount_percent=Decimal("0"), gst_rate=Decimal("18"), product_id=biriyani.id)],
        ),
    )
    db.session.commit()

    checkout(
        tenant, staff,
        CartInput(lines=[CartLineInput(description=biriyani.name, hsn_or_sac_code="996331", qty=Decimal("3"), rate=Decimal("150"), gst_rate=Decimal("18"), unit="plate", product_id=biriyani.id)]),
        PaymentMode.CASH,
    )
    db.session.commit()

    today = date.today()
    staff_only = stock_report_data(tenant, today, today, branch_ids=[staff.id])
    row = next(r for r in staff_only["rows"] if r.product_id == biriyani.id)
    assert row.allocated == Decimal("20")
    assert row.sold == Decimal("3")

    other_only = stock_report_data(tenant, today, today, branch_ids=[other_branch.id])
    other_row = next(r for r in other_only["rows"] if r.product_id == biriyani.id)
    assert other_row.allocated == Decimal("5")
    assert other_row.sold == Decimal("0")

    combined = stock_report_data(tenant, today, today)
    combined_row = next(r for r in combined["rows"] if r.product_id == biriyani.id)
    assert combined_row.allocated == Decimal("25")
    assert combined_row.sold == Decimal("3")


def test_item_sales_data_merges_invoice_and_pos_lines_by_product(db, tenant, client_admin, staff):
    biriyani = _product(db, tenant)
    gstin = tenant.gstins.first()
    create_invoice(
        tenant, client_admin,
        InvoiceInput(
            gstin_id=gstin.id, document_type=DocumentType.TAX_INVOICE, place_of_supply_state_code=gstin.state_code,
            invoice_date=date.today(), notes=None, customer_id=None, branch_user_id=staff.id,
            lines=[LineInput(description=biriyani.name, hsn_or_sac_code="996331", qty=Decimal("10"), rate=Decimal("150"), discount_percent=Decimal("0"), gst_rate=Decimal("18"), product_id=biriyani.id)],
        ),
    )
    db.session.commit()
    checkout(
        tenant, staff,
        CartInput(lines=[CartLineInput(description=biriyani.name, hsn_or_sac_code="996331", qty=Decimal("4"), rate=Decimal("150"), gst_rate=Decimal("18"), unit="plate", product_id=biriyani.id)]),
        PaymentMode.CASH,
    )
    db.session.commit()

    today = date.today()
    data = item_sales_data(tenant, today, today)
    assert len(data["rows"]) == 1
    row = data["rows"][0]
    assert row.product_id == biriyani.id
    assert row.qty == Decimal("14")


def test_item_sales_data_groups_productless_lines_by_description(db, tenant, client_admin, staff):
    gstin = tenant.gstins.first()
    create_invoice(
        tenant, client_admin,
        InvoiceInput(
            gstin_id=gstin.id, document_type=DocumentType.TAX_INVOICE, place_of_supply_state_code=gstin.state_code,
            invoice_date=date.today(), notes=None, customer_id=None, branch_user_id=staff.id,
            lines=[LineInput(description="Custom item", hsn_or_sac_code="996331", qty=Decimal("2"), rate=Decimal("50"), discount_percent=Decimal("0"), gst_rate=Decimal("18"), product_id=None)],
        ),
    )
    db.session.commit()

    today = date.today()
    data = item_sales_data(tenant, today, today)
    assert len(data["rows"]) == 1
    assert data["rows"][0].product_id is None
    assert data["rows"][0].product_name == "Custom item"


# --- routes: filter bar renders, branch filter round-trips through the URL --

def test_sales_view_renders_the_filter_bar(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/reports/sales")
    assert resp.status_code == 200
    assert b'name="mode"' in resp.data
    assert b'name="branch_id"' in resp.data


def test_stock_view_and_items_view_respect_branch_filter(client, db, tenant, client_admin, staff):
    biriyani = _product(db, tenant)
    other_branch = _branch(db, tenant, "Other Branch", "other3@acme.example.com")

    checkout(
        tenant, staff,
        CartInput(lines=[CartLineInput(description=biriyani.name, hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("150"), gst_rate=Decimal("18"), unit="plate", product_id=biriyani.id)]),
        PaymentMode.CASH,
    )
    checkout(
        tenant, other_branch,
        CartInput(lines=[CartLineInput(description=biriyani.name, hsn_or_sac_code="996331", qty=Decimal("9"), rate=Decimal("150"), gst_rate=Decimal("18"), unit="plate", product_id=biriyani.id)]),
        PaymentMode.CASH,
    )
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")

    resp = client.get(f"/reports/stock?mode=day&date={date.today().isoformat()}&branch_id={staff.id}")
    assert resp.status_code == 200
    assert b"1.000" in resp.data or b"1.00" in resp.data

    resp = client.get(f"/reports/items?mode=day&date={date.today().isoformat()}&branch_id={other_branch.id}")
    assert resp.status_code == 200
    assert b"9.000" in resp.data or b"9.00" in resp.data


def test_reports_index_links_to_all_three_reports(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/reports/")
    assert resp.status_code == 200
    assert b"/reports/sales" in resp.data
    assert b"/reports/stock" in resp.data
    assert b"/reports/items" in resp.data
