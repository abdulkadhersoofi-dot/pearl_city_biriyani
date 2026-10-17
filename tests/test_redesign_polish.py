"""Redesign stage 6: dark mode (following the device, or picked per
device), the split-screen sign-in pages, the firm console's Clients page
and the accessibility helpers."""

import html
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.invoicing.services import InvoiceInput, LineInput, create_invoice
from app.models.customer import Customer
from app.models.invoice_series import DocumentType
from app.models.tenant import BillingCycle, RegistrationType, Tenant
from app.utils.theme import tenant_theme_css

CSS = (Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "app.css").read_text()


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


# --- Dark mode ----------------------------------------------------------------

def test_dark_colours_only_ever_apply_on_screen():
    """Every dark block sits inside a screen media query, so printing from
    the browser always comes out light."""
    blocks = [m.start() for m in re.finditer(r':root(:not\(\[data-theme="light"\]\)|\[data-theme="dark"\])\s*\{', CSS)]
    assert len(blocks) >= 4
    for at in blocks:
        opened = CSS.rfind("@media", 0, at)
        assert "screen" in CSS[opened:CSS.index("{", opened)], CSS[opened:at][:80]
    assert "@media screen and (prefers-color-scheme: dark)" in CSS


def test_a_clients_own_colours_step_aside_in_the_dark_but_not_their_button(db, tenant):
    tenant.primary_color = "#aa3355"
    tenant.background_color = "#fff8e7"
    db.session.commit()
    css = tenant_theme_css(tenant)
    # Unchanged for light screens: the client's colours on plain :root ...
    assert ":root { --color-primary: #aa3355;" in css and "--color-bg: #fff8e7" in css
    # ... and the dark blocks are more specific than :root, so they win for
    # the background and text, while never touching --color-primary.
    dark = CSS[CSS.index("@media screen and (prefers-color-scheme: dark)"):]
    dark = dark[:dark.index("\n}\n")]
    assert "--color-bg:" in dark and "--color-page-text:" in dark and "--color-primary:" not in dark


def test_pages_set_the_saved_theme_before_paint(client, db, client_admin):
    page = client.get("/auth/login").get_data(as_text=True)
    assert 'name="color-scheme" content="light dark"' in page and 'localStorage.getItem("arfa.theme")' in page
    _login(client, client_admin.email, "ClientSecret123")
    page = client.get("/invoices/").get_data(as_text=True)
    assert page.index('localStorage.getItem("arfa.theme")') < page.index("css/app.css")
    menu = page.split('aria-label="Your account"', 1)[1]
    assert 'x-data="themePick"' in menu and "Auto" in menu and "Light" in menu and "Dark" in menu


def test_a_branch_login_can_pick_light_or_dark_too(client, db, staff):
    _login(client, staff.email, "CashierSecret123")
    page = client.get("/pos/").get_data(as_text=True)
    assert 'aria-label="Appearance: light or dark"' in page and 'x-data="themePick"' in page


# --- Sign-in pages --------------------------------------------------------------

def test_sign_in_is_split_with_the_brand_on_one_side(client, db):
    page = client.get("/auth/login").get_data(as_text=True)
    assert 'class="auth-split"' in page and 'class="auth-side"' in page and 'class="auth-main"' in page
    assert '<h1 class="auth-title">Sign in</h1>' in page and 'class="auth-points"' in page
    assert 'autocomplete="username"' in page and 'autocomplete="current-password"' in page
    assert 'data-show-password' in page and 'aria-label="Show password"' in page


def test_a_clients_own_sign_in_link_wears_their_name_and_colour(client, db, tenant):
    tenant.login_slug = "acme-polish"
    tenant.primary_color = "#224488"
    db.session.commit()
    page = client.get("/auth/login/acme-polish").get_data(as_text=True)
    side = page.split('class="auth-side"', 1)[1].split("</aside>", 1)[0]
    assert tenant.display_name in side and 'class="auth-points"' not in side
    assert "--color-primary: #224488" in page


def test_a_wrong_password_is_announced(client, db, client_admin):
    page = _login(client, client_admin.email, "nope").get_data(as_text=True)
    assert 'role="alert">Incorrect email or password.' in page


def test_two_step_pages_have_their_own_heading(client, db, client_admin):
    from datetime import datetime, timezone

    client_admin.totp_secret = "JBSWY3DPEHPK3PXP"
    client_admin.totp_enabled_at = datetime.now(timezone.utc)
    db.session.commit()
    res = client.post("/auth/login", data={"email": client_admin.email, "password": "ClientSecret123"})
    assert res.status_code == 302
    page = client.get(res.headers["Location"]).get_data(as_text=True)
    assert '<h1 class="auth-title">Two-step sign-in</h1>' in page


# --- Firm console ----------------------------------------------------------------

def test_clients_page_counts_clients_by_state(client, db, super_admin, tenant):
    paused = Tenant(legal_name="Paused Stores", registration_type=RegistrationType.REGULAR,
                    onboarded_by_id=super_admin.id, billing_cycle=BillingCycle.MONTHLY, is_active=False)
    pending = Tenant(legal_name="New Shop", registration_type=RegistrationType.COMPOSITION,
                     onboarded_by_id=super_admin.id, billing_cycle=None)
    db.session.add_all([paused, pending])
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    page = client.get("/admin/clients").get_data(as_text=True)
    # One row of filter chips with counts (console cleanup, stage 2).
    counts = {html.unescape(label.strip()): n
              for label, n in re.findall(r'class="filter-chip"[^>]*>\s*([^<]+?) <b>(\d+)</b>', page)}
    assert counts == {"All": "3", "Active": "1", "Renewal due": "0", "Can't sign in": "1",
                      "Waiting for verification": "1", "No auditor": "2"}
    assert 'data-groups=" all blocked no_auditor "' in page and "Paused" in page and "Waiting for verification" in page
    acme = page.split("Acme Traders", 1)[0].rsplit("<tr", 1)[1]
    assert 'data-groups=" all active no_auditor "' in acme and "27aaaaa0000a1z5" in acme   # searchable by GSTIN


def test_client_page_jumps_to_its_sections_and_reads_money_the_indian_way(client, db, super_admin, tenant, client_admin):
    buyer = Customer(tenant_id=tenant.id, name="Ravi Stores", gstin="27AAAPR1234A1Z5", state_code="27",
                     state_name="Maharashtra")
    db.session.add(buyer)
    db.session.commit()
    create_invoice(tenant, client_admin, InvoiceInput(
        gstin_id=tenant.gstins.first().id, document_type=DocumentType.TAX_INVOICE, customer_id=buyer.id,
        place_of_supply_state_code="27", invoice_date=date.today(), notes=None,
        lines=[LineInput(description="Service", hsn_or_sac_code="998719", qty=Decimal("1"), rate=Decimal("100000"),
                         discount_percent=Decimal("0"), gst_rate=Decimal("18"), product_id=None, unit="NOS")]))
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    page = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    nav = page.split('aria-label="Sections of this page"', 1)[1].split("</nav>", 1)[0]
    for target in ("#sec-access", "#sec-users", "#sec-activity"):
        assert target in nav and f'id="{target[1:]}"' in page
    assert "Rs. 1,18,000.00" in page


# --- Accessibility helpers ---------------------------------------------------------

def test_form_errors_are_tied_to_their_fields(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    page = client.post("/customers/new", data={"name": ""}, follow_redirects=True).get_data(as_text=True)
    field = re.search(r'<input[^>]*name="name"[^>]*>', page).group(0)
    assert 'aria-invalid="true"' in field and 'aria-describedby="name-error"' in field
    assert 'id="name-error"' in page
