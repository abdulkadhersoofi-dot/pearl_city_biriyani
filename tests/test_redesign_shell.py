"""The new app frame (redesign stage 1): the grouped side menu, the New
menu, the search palette, the bell and toasts. A switched-off module must
never show up in any of them, and the bell must only ever count the
signed-in client's own rows."""

import html as html_lib
import json
import re
from decimal import Decimal as D

import pytest

from app.models.product import Product
from app.models.tenant import Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _page(client, path="/invoices/"):
    return client.get(path).get_data(as_text=True)


def _menu_labels(html):
    return [html_lib.unescape(t) for t in re.findall(r'class="nav-label">([^<]+)<', html)]


def _palette(html):
    raw = re.search(r'<script type="application/json" id="palette-data">(.*?)</script>', html, re.S).group(1)
    return json.loads(raw)


def _set(db, tenant, **flags):
    for key, value in flags.items():
        tenant.set_feature(key, value)
    db.session.commit()


def test_client_admin_gets_grouped_menu_with_only_core_screens(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    html = _page(client)
    labels = _menu_labels(html)
    for core in ("POS", "Invoices", "Items", "Customers", "Reports", "GST returns",
                 "Branches", "Settings", "Live"):
        assert core in labels
    # Credit / debit notes are a tab inside Invoices now, not their own entry.
    assert "Credit & debit notes" not in labels and "/notes/" in html
    for module in ("Stock", "Purchases", "Expenses", "Accounts", "Job cards", "Tables", "Old gold", "Wholesale",
                   "Loyalty", "e-Invoice & e-Way bills", "Quotes & orders", "Dashboard"):
        assert module not in labels
    groups = [html_lib.unescape(g) for g in re.findall(r'class="nav-group-label">([^<]+)<', html)]
    assert groups == ["Home", "Sell", "Stock & buying", "People & money", "Reports & GST", "Business"]
    # The old flat top row is gone.
    assert "main-nav" not in html


def test_turning_a_module_on_adds_it_to_menu_new_and_search(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    before = _page(client)
    assert "/inventory/adjustments/new" not in before
    assert not any(e["url"] == "/inventory/reorder" for e in _palette(before))

    _set(db, tenant, inventory=True, purchases=True, pharmacy=True)
    after = _page(client)
    assert {"Stock", "Purchases"} <= set(_menu_labels(after))
    assert "/inventory/adjustments/new" in after  # New menu
    urls = {e["url"] for e in _palette(after)}
    assert {"/inventory/reorder", "/purchases/suppliers", "/pharmacy/register"} <= urls


def test_switched_off_modules_never_reach_the_search_palette(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    urls = {e["url"] for e in _palette(_page(client))}
    for hidden in ("/inventory/", "/purchases/", "/expenses/", "/jobs/", "/restaurant/", "/jewellery/rates",
                   "/pharmacy/register", "/accounts/receivables", "/reports/dashboard", "/pos/offline",
                   "/branches/cashiers", "/settings/modules"):
        assert hidden not in urls
    # ...and each one really is closed, not just unlisted.
    for hidden in ("/inventory/", "/purchases/", "/pharmacy/register"):
        assert client.get(hidden).status_code == 404


def test_active_entry_follows_the_page(client, db, tenant, client_admin):
    _set(db, tenant, business_reports=True)
    _login(client, "admin@acme.example.com", "ClientSecret123")
    html = _page(client, "/reports/gstr3b")
    active = re.findall(r'class="nav-link active"[^>]*>\s*<svg[^>]*>.*?</svg><span class="nav-label">([^<]+)<', html, re.S)
    assert active == ["GST returns"]
    html = _page(client, "/reports/dashboard")
    active = re.findall(r'class="nav-link active"[^>]*>\s*<svg[^>]*>.*?</svg><span class="nav-label">([^<]+)<', html, re.S)
    assert active == ["Dashboard"]


def test_branch_login_keeps_the_simple_counter_layout(client, db, tenant, staff):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    html = _page(client, "/pos/")
    assert 'class="sidenav"' not in html
    assert "palette-data" not in html
    assert "btn-new" not in html
    assert "Sign out" in html
    assert client.get("/needs-you").status_code == 404


def test_firm_console_gets_its_own_groups(client, db, super_admin):
    _login(client, "firm@example.com", "SuperSecret123")
    html = _page(client, "/admin/clients")
    assert _menu_labels(html) == ["Today", "Clients", "Auditors", "Filing & GST", "Packages", "Billing", "Audit log"]
    assert "btn-new" not in html  # nothing to create from the console's New
    assert "/admin/audit-log" in {e["url"] for e in _palette(html)}


def test_flashed_messages_become_toasts(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    resp = client.post("/customers/new", data={"name": "Ravi Stores", "state_code": "27"}, follow_redirects=True)
    html = resp.get_data(as_text=True)
    stack = html.split('class="toast-stack"', 1)[1]
    assert "toast toast-" in stack


def test_icons_come_from_the_vendored_sprite(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    html = _page(client)
    assert re.search(r"/static/img/icons\.svg\?v=[0-9a-f]{10}#i-receipt-text", html)
    assert client.get("/static/img/icons.svg").status_code == 200
    for font in ("figtree", "bricolage-grotesque", "jetbrains-mono"):
        assert client.get(f"/static/fonts/{font}.woff2").status_code == 200
    assert "fonts.googleapis" not in html and "cdn." not in html


def _low_stock_item(db, tenant, name):
    p = Product(tenant_id=tenant.id, name=name, hsn_or_sac_code="3401", gst_rate=D("18"), unit="PCS",
                sale_price=D("10"), min_stock_level=D("5"))
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture()
def other_tenant(db, super_admin):
    t = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id)
    db.session.add(t)
    db.session.flush()
    db.session.add(Gstin(tenant_id=t.id, gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka",
                         is_primary=True))
    admin = User(tenant_id=t.id, name="Other Admin", email="admin@other.example.com", role=UserRole.CLIENT_ADMIN,
                 must_change_password=False)
    admin.set_password("OtherSecret123")
    db.session.add(admin)
    db.session.commit()
    return t


def test_bell_lists_only_this_clients_low_stock(client, db, tenant, client_admin, other_tenant):
    _set(db, tenant, inventory=True)
    _set(db, other_tenant, inventory=True)
    _low_stock_item(db, other_tenant, "Other Soap")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    html = client.get("/needs-you").get_data(as_text=True)
    assert "Nothing needs you" in html and "data-alerts" not in html

    _low_stock_item(db, tenant, "Our Soap")
    _low_stock_item(db, tenant, "Our Shampoo")
    html = client.get("/needs-you").get_data(as_text=True)
    assert 'data-alerts="1"' in html
    assert "2 items at or below minimum stock" in html  # not 3: the other client's soap never counts


def test_bell_says_nothing_about_a_module_that_is_off(client, db, tenant, client_admin):
    _low_stock_item(db, tenant, "Our Soap")  # inventory flag is off
    _login(client, "admin@acme.example.com", "ClientSecret123")
    html = client.get("/needs-you").get_data(as_text=True)
    assert "minimum stock" not in html


def test_default_theme_font_is_the_bundled_one_and_a_chosen_font_still_wins():
    from app.utils.theme import tenant_theme_css

    default = tenant_theme_css(Tenant(primary_color="#1f7a4d", background_color="#f6f7f5", text_color="#1f2a24",
                                      font_family="system"))
    assert '"Figtree"' in default and "--font-display" not in default
    serif = tenant_theme_css(Tenant(primary_color="#1f7a4d", background_color="#f6f7f5", text_color="#1f2a24",
                                    font_family="serif"))
    assert "--font-display: Georgia" in serif
