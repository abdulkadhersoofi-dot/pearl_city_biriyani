"""The simple menu: seven doors, a hub page behind each (app/doors.py), and
the switch back to the classic menu. Nothing a business could reach from the
classic menu may be lost, and a switched-off module must not show on a hub."""

import html as html_lib
import json
import re

import pytest

from app import doors
from app.models.tenant import Tenant
from app.models.user import User, UserRole

ALL_ON = ("inventory", "purchases", "expenses", "receivables", "payables", "cash_bank_book", "business_reports",
          "sales_documents", "recurring_invoices", "job_cards", "jewellery", "pharmacy", "wholesale", "loyalty",
          "batch_expiry", "einvoice", "eway_bill", "daily_summary", "categories", "multi_price_list", "offline_pos",
          "cashier_pin", "day_close", "weighing", "restaurant_mode", "doc_scan", "warehouse_dispatch",
          "margin_scheme", "serial_imei", "electronics")


@pytest.fixture()
def simple(app):
    app.config["MENU_DEFAULT"] = "simple"
    yield
    app.config["MENU_DEFAULT"] = "classic"


def _login(client, email="admin@acme.example.com", password="ClientSecret123"):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _menu(html):
    return [html_lib.unescape(t) for t in re.findall(r'class="nav-label">([^<]+)<', html)]


def _palette(html):
    raw = re.search(r'<script type="application/json" id="palette-data">(.*?)</script>', html, re.S).group(1)
    return json.loads(raw)


def _all_on(db, tenant):
    for flag in ALL_ON:
        tenant.set_feature(flag, True)
    db.session.commit()


# --- the menu ------------------------------------------------------------------------------------------

def test_a_small_shop_sees_six_doors_and_a_full_one_seven(client, db, tenant, client_admin, simple):
    _login(client)
    html = client.get("/invoices/").get_data(as_text=True)
    assert _menu(html) == ["Home", "Sell", "Stock", "Money", "Reports", "Setup"]     # nothing to buy with yet
    assert "nav-group-label" not in html                                              # no group headings
    _all_on(db, tenant)
    assert _menu(client.get("/invoices/").get_data(as_text=True)) == ["Home", "Sell", "Buy", "Stock", "Money",
                                                                      "Reports", "Setup"]


@pytest.mark.parametrize("path, door", [
    ("/reports/dashboard", "Home"), ("/live/", "Home"),
    ("/invoices/", "Sell"), ("/pos/", "Sell"), ("/sales/documents", "Sell"), ("/notes/", "Sell"), ("/sell", "Sell"),
    ("/purchases/", "Buy"), ("/scan/", "Buy"), ("/expenses/", "Buy"), ("/bulk/suppliers/", "Buy"), ("/buy", "Buy"),
    ("/products/", "Stock"), ("/inventory/", "Stock"), ("/warehouse/", "Stock"), ("/bulk/products/", "Stock"),
    ("/jewellery/rates", "Stock"),
    ("/customers/", "Money"), ("/accounts/receivables", "Money"), ("/bulk/customers/", "Money"),
    ("/warehouse/financiers", "Money"),
    ("/reports/", "Reports"), ("/reports/gstr1", "Reports"), ("/reports/books-check", "Reports"),
    ("/branches/", "Setup"), ("/settings/", "Setup"), ("/inventory/locations", "Setup"), ("/scan/teach", "Setup"),
    ("/warehouse/logins", "Setup"), ("/setup", "Setup"),
])
def test_the_right_door_lights_up(client, db, tenant, client_admin, simple, path, door):
    _all_on(db, tenant)
    _login(client)
    html = client.get(path, follow_redirects=True).get_data(as_text=True)
    active = re.findall(r'class="nav-link active"[^>]*>\s*<svg[^>]*>.*?</svg><span class="nav-label">([^<]+)<', html,
                        re.S)
    assert active == [door], path


def test_the_classic_menu_is_one_tap_away_and_is_remembered(client, db, tenant, client_admin, simple):
    _login(client)
    page = client.get("/invoices/").get_data(as_text=True)
    assert "Use the classic menu" in page and "/menu/classic" in page
    resp = client.post("/menu/classic", data={"next": "/invoices/"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/invoices/")
    assert "arfa_menu=classic" in resp.headers["Set-Cookie"] and "HttpOnly" in resp.headers["Set-Cookie"]
    classic = client.get("/invoices/").get_data(as_text=True)
    assert "Invoices" in _menu(classic) and "Use the simple menu" in classic
    assert [html_lib.unescape(g) for g in re.findall(r'class="nav-group-label">([^<]+)<', classic)][0] == "Home"
    client.post("/menu/simple", data={"next": "/invoices/"})
    assert _menu(client.get("/invoices/").get_data(as_text=True))[0:2] == ["Home", "Sell"]
    assert client.post("/menu/whatever").status_code == 404
    # "next" only ever leads back into this site.
    assert client.post("/menu/simple", data={"next": "https://evil.example/"}).headers["Location"] == "/"
    assert client.post("/menu/simple", data={"next": "//evil.example/"}).headers["Location"] == "/"


def test_branch_logins_and_the_console_keep_their_own_menus(client, db, tenant, staff, super_admin, simple):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    assert client.get("/sell").status_code in (302, 403)
    assert client.post("/menu/classic").status_code in (302, 403)
    client.get("/auth/logout")
    _login(client, "firm@example.com", "SuperSecret123")
    html = client.get("/admin/clients").get_data(as_text=True)
    assert _menu(html)[:2] == ["Today", "Clients"] and "Use the classic menu" not in html


# --- the hubs ------------------------------------------------------------------------------------------

def test_a_hub_shows_only_what_is_switched_on(client, db, tenant, client_admin, simple):
    _login(client)
    sell = client.get("/sell").get_data(as_text=True)
    assert "New invoice" in sell and "Invoices" in sell and "Credit &amp; debit notes" in sell
    for hidden in ("Sales orders", "Job cards", "Old gold", "Second-hand goods", "Ready to bill", "Tables"):
        assert hidden not in sell
    assert client.get("/buy").status_code == 404                       # nothing to buy with yet
    tenant.set_feature("sales_documents", True)
    tenant.set_feature("job_cards", True)
    tenant.set_feature("purchases", True)
    db.session.commit()
    sell = client.get("/sell").get_data(as_text=True)
    assert "Sales orders" in sell and "Job cards" in sell and "Quote or order" in sell
    buy = client.get("/buy").get_data(as_text=True)
    assert "Add a bill" in buy and "Purchase orders" in buy and "Scan bills" not in buy


def test_every_hub_opens_with_everything_on_and_every_tile_leads_somewhere(client, db, tenant, client_admin, simple):
    _all_on(db, tenant)
    _login(client)
    seen = set()
    for path in ("/home", "/sell", "/buy", "/stock", "/money", "/setup", "/reports/"):
        html = client.get(path).get_data(as_text=True)
        hub = html.split('<main id="main"', 1)[1]
        links = set(re.findall(r'href="(/[^"#]*)"', hub))
        assert links, path
        seen |= links
    for url in sorted(seen):
        if "logout" in url or url.endswith((".pdf", ".xlsx", ".csv")):
            continue
        status = client.get(html_lib.unescape(url)).status_code
        assert status in (200, 302), (url, status)


def test_waiting_for_you_counts_what_is_waiting(client, db, tenant, client_admin, simple):
    from datetime import date

    from app.models.purchase import PurchaseOrder, PurchaseOrderStatus, Supplier
    from app.models.scan import TO_CHECK, ScanDocument

    tenant.set_feature("purchases", True)
    tenant.set_feature("doc_scan", True)
    db.session.commit()
    _login(client)
    assert "Waiting for you" not in client.get("/buy").get_data(as_text=True)
    gstin = tenant.gstins.first()
    supplier = Supplier(tenant_id=tenant.id, name="Sri Balaji", state_code="27", state_name="Maharashtra")
    db.session.add(supplier)
    db.session.flush()
    from app.utils.numbering import get_or_create_series
    from app.models.invoice_series import DocumentType
    from app.utils.financial_year import financial_year_for

    series = get_or_create_series(tenant.id, gstin.id, DocumentType.PURCHASE_ORDER, financial_year_for(date.today()))
    for n in (1, 2):
        db.session.add(PurchaseOrder(tenant_id=tenant.id, gstin_id=gstin.id, invoice_series_id=series.id,
                                     number=f"PO/{n}", supplier_id=supplier.id, order_date=date.today(),
                                     status=PurchaseOrderStatus.OPEN, created_by_id=client_admin.id))
    db.session.add(ScanDocument(tenant_id=tenant.id, uploaded_by_id=client_admin.id, status=TO_CHECK,
                                filename="bill.jpg", content_type="image/jpeg", file_hash="0" * 64,
                                file_data=b"x"))
    db.session.commit()
    buy = client.get("/buy").get_data(as_text=True)
    assert "Waiting for you" in buy
    assert "1 scanned bill to check and post" in buy and "2 purchase orders waiting for goods or a bill" in buy


def test_one_broken_waiting_list_never_takes_a_hub_down(client, db, tenant, client_admin, simple, monkeypatch):
    def boom(_tenant):
        raise RuntimeError("no")

    monkeypatch.setattr(doors, "_sell_waiting", boom)
    _login(client)
    resp = client.get("/sell")
    assert resp.status_code == 200 and "New invoice" in resp.get_data(as_text=True)


def test_reports_pins_the_answers_asked_for_most_days(client, db, tenant, client_admin, simple):
    tenant.set_feature("business_reports", True)
    db.session.commit()
    _login(client)
    page = client.get("/reports/").get_data(as_text=True)
    pinned = page.split('class="report-daily"', 1)[1].split("</section>", 1)[0]
    assert "Sales report" in pinned and "Profit &amp; loss" in pinned and "Books check" in pinned
    assert "Stock on hand" not in pinned                                 # stock is not switched on


# --- nothing is lost -----------------------------------------------------------------------------------

def test_every_classic_screen_is_still_reachable_from_the_doors(app, client, db, tenant, client_admin, simple):
    """Everything the classic menu and its search reach with every module
    on is on a hub, on the Reports page, or found by the search box under
    the simple menu."""
    _all_on(db, tenant)
    _login(client)
    app.config["MENU_DEFAULT"] = "classic"
    classic = client.get("/invoices/").get_data(as_text=True)
    classic_urls = {e["url"] for e in _palette(classic)} | set(re.findall(r'class="nav-link[^"]*" href="([^"]+)"', classic))
    app.config["MENU_DEFAULT"] = "simple"
    simple_page = client.get("/invoices/").get_data(as_text=True)
    reachable = {e["url"] for e in _palette(simple_page)}
    for path in ("/home", "/sell", "/buy", "/stock", "/money", "/setup", "/reports/"):
        html = client.get(path).get_data(as_text=True).split('<main id="main"', 1)[1]
        reachable |= {html_lib.unescape(u) for u in re.findall(r'href="(/[^"#]*)"', html)}
    reachable |= set(re.findall(r'href="(/[^"#]*)"', simple_page))
    missing = sorted(u for u in classic_urls if u not in reachable)
    assert not missing, missing


def test_search_finds_screens_by_their_hub_names_too(client, db, tenant, client_admin, simple):
    _all_on(db, tenant)
    _login(client)
    entries = _palette(client.get("/invoices/").get_data(as_text=True))
    labels = {e["label"] for e in entries}
    assert {"Sell", "Buy", "Stock", "Money", "Setup"} <= labels
    assert {"Bill reader training", "Goods received", "Finance companies (EMI)", "Customers owe you"} <= labels
    urls = [e["url"] for e in entries]
    assert len(urls) == len(set(zip(urls, [e["label"] for e in entries])))      # no exact repeats


def test_the_phone_bar_follows_the_doors(client, db, tenant, client_admin, simple):
    _login(client)
    html = client.get("/invoices/").get_data(as_text=True)
    bar = html.split('class="bottom-tabs"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/sell"' in bar and 'href="/reports/"' in bar and 'href="/home"' in bar
    assert 'class="tab active"' in bar and bar.index('class="tab active"') > bar.index('href="/home"')
