"""Home dashboard and Reports hub (redesign stage 4): the 14-day trend
adds up to the same sales figure as before, comparisons, Indian money
formatting, and the hub showing only reports whose modules are on."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.finance.dashboard import _sales, change, daily_sales, dashboard
from app.invoicing.services import InvoiceInput, LineInput, create_invoice
from app.models.customer import Customer
from app.models.invoice_series import DocumentType
from app.models.pos_bill import PaymentMode
from app.models.product import Product
from app.models.tenant import Gstin, RegistrationType, Tenant
from app.pos.returns import create_return, returnable_lines
from app.pos.services import CartInput, CartLineInput, checkout
from app.utils.money import inr, inr_compact

D = Decimal
TODAY = date.today()


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def fan(db, tenant):
    p = Product(tenant_id=tenant.id, name="Table fan", hsn_or_sac_code="8414", gst_rate=D("18"), unit="NOS",
                sale_price=D("200"))
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture()
def buyer(db, tenant):
    c = Customer(tenant_id=tenant.id, name="Ravi Stores", gstin="27AAAPR1234A1Z5", state_code="27",
                 state_name="Maharashtra")
    db.session.add(c)
    db.session.commit()
    return c


def _invoice(db, tenant, user, customer, on, rate="1000"):
    inv = create_invoice(tenant, user, InvoiceInput(
        gstin_id=tenant.gstins.first().id, document_type=DocumentType.TAX_INVOICE, customer_id=customer.id,
        place_of_supply_state_code="27", invoice_date=on, notes=None,
        lines=[LineInput(description="Service", hsn_or_sac_code="998719", qty=D("1"), rate=D(rate),
                         discount_percent=D("0"), gst_rate=D("18"), product_id=None, unit="NOS")]))
    db.session.commit()
    return inv


def test_money_reads_the_indian_way():
    assert inr(D("164944")) == "1,64,944.00"
    assert inr(D("12345678.005")) == "1,23,45,678.01"
    assert inr(D("-1234567.5")) == "-12,34,567.50"
    assert inr(D("999")) == "999.00" and inr(None) == "0.00"
    assert inr(D("1234.5"), 0) == "1,235"
    assert inr_compact(D("164944")) == "1.6L"
    assert inr_compact(D("45000")) == "45K"
    assert inr_compact(D("21000000")) == "2.1Cr"
    assert inr_compact(D("950")) == "950"


def test_change_against_an_earlier_figure():
    assert change(D("110"), D("100")) == {"pct": D("10"), "up": True, "same": False}
    assert change(D("75"), D("100")) == {"pct": D("25"), "up": False, "same": False}
    assert change(D("100"), D("0")) is None


def test_trend_days_add_up_to_the_sales_figure(db, tenant, client_admin, staff, fan, buyer):
    _invoice(db, tenant, client_admin, buyer, TODAY - timedelta(days=3), rate="1000")   # 1,180
    _invoice(db, tenant, client_admin, buyer, TODAY, rate="500")                        # 590
    cart = CartInput(lines=[CartLineInput(description="Table fan", hsn_or_sac_code="8414", qty=D("2"), rate=D("236"),
                                          gst_rate=D("18"), product_id=fan.id, unit="NOS", price_includes_tax=True)])
    bill = checkout(tenant, staff, cart, PaymentMode.CASH)                               # 472 today
    db.session.commit()
    create_return(bill, staff, {returnable_lines(bill)[0].line.id: D("1")}, "Faulty", PaymentMode.CASH)
    db.session.commit()
    start = TODAY - timedelta(days=13)
    series = daily_sales(tenant, start, TODAY)
    assert len(series) == 14 and series[0][0] == start and series[-1][0] == TODAY
    by_day = dict(series)
    for day, value in series:
        assert value == _sales(tenant, day, day), day
    assert by_day[TODAY - timedelta(days=3)] == D("1180.00")
    assert sum(by_day.values()) == _sales(tenant, start, TODAY)
    d = dashboard(tenant)
    assert d["series"] == series and d["series"][-1][1] == d["sales_today"]


def test_trend_never_counts_another_clients_sales(db, tenant, client_admin, buyer, super_admin):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id)
    db.session.add(other)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other.id, gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka",
                         is_primary=True))
    db.session.commit()
    _invoice(db, tenant, client_admin, buyer, TODAY, rate="1000")
    assert dict(daily_sales(other, TODAY - timedelta(days=13), TODAY))[TODAY] == D("0")


def test_dashboard_page_shows_the_trend_and_needs_you(client, db, tenant, client_admin, buyer):
    tenant.set_feature("business_reports", True)
    db.session.commit()
    _invoice(db, tenant, client_admin, buyer, TODAY, rate="1000")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    html = client.get("/reports/dashboard").get_data(as_text=True)
    assert "Sales, last 14 days" in html and html.count('class="bar-slot') == 14
    assert "Rs. 1,180.00" in html          # Indian grouping on the figures
    assert "<caption>Sales by day</caption>" in html   # the chart's table for screen readers
    assert "Needs you" in html and "All clear" in html


def test_reports_hub_shows_only_reports_whose_modules_are_on(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    html = client.get("/reports/").get_data(as_text=True)
    for always in ("/reports/sales", "/reports/items", "/reports/stock", "/live/", "GSTR-1 and GSTR-3B"):
        assert always in html, always
    for hidden in ("/reports/profit-loss", "/inventory/movement", "/pharmacy/register", "/accounts/receivables",
                   "/reports/margin-register", "/einvoice/log", "Registers for your auditor"):
        assert hidden not in html, hidden
    for flag in ("business_reports", "inventory", "pharmacy", "receivables", "expenses"):
        tenant.set_feature(flag, True)
    db.session.commit()
    html = client.get("/reports/").get_data(as_text=True)
    for shown in ("/reports/profit-loss", "/inventory/movement", "/pharmacy/register", "/accounts/receivables",
                  "/expenses/report", "Registers for your auditor"):
        assert shown in html, shown
    # Every tile opens.
    import re

    for url in set(re.findall(r'class="report-tile"[^>]*?href="([^"]+)"|href="([^"]+)" class="report-tile"', html)):
        target = url[0] or url[1]
        assert client.get(target).status_code in (200, 302), target
