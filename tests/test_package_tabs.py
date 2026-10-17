"""Console cleanup, stage 4: the package editor in tabs - Business type,
Trade packs, Modules, Counter options, Settings & locks, Logins & limits,
Name & price - with who a package is for (pre-setting its trade pack),
one save that keeps the open tab, and packages compared side by side."""

import re

import pytest

from app.models.audit_log import AuditLog
from app.models.package import Package
from app.models.user import BillingCycle, User, UserRole
from app.utils.entitlements import STARTING_PACKAGES, ensure_starting_packages, package_module_state
from app.utils.features import AVAILABLE_FEATURES


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def packs(db):
    ensure_starting_packages()
    db.session.commit()
    return {p.key: p for p in Package.query.all()}


@pytest.fixture()
def ultra(client, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    return super_admin


def _tabs(html):
    nav = html.split('aria-label="Parts of this package"', 1)[1].split("</nav>", 1)[0]
    return re.findall(r'id="ptabbtn-(\w+)"', nav)


def _open(html):
    return re.findall(r"x-data=\"packageEditor\('(\w+)'", html)[0]


def _panel(html, key):
    return html.split(f'id="ptab-{key}"', 1)[1].split('<section id="ptab-', 1)[0]


def _form(package, **extra):
    data = {"name": package.name, "description": package.description or "", "price_monthly": "", "price_yearly": "",
            "unlisted_modules": package.unlisted_modules, "own_branding": "y" if package.own_branding else "",
            "business_type": package.business_type or ""}
    for f in AVAILABLE_FEATURES:
        data[f"mod_{f.key}"] = package_module_state(package, f.key)
    for key, value in (package.limits or {}).items():
        data[f"limit_{key}"] = str(value)
    for key, value in (package.settings or {}).items():
        data[f"set_{key}"] = value
    data.update(extra)
    return data


# --- the tabs ---------------------------------------------------------------------------------------

def test_seven_tabs_in_order(client, db, ultra, packs):
    html = client.get(f"/admin/packages/{packs['retail'].id}").get_data(as_text=True)
    assert _tabs(html) == ["type", "trade", "modules", "counter", "settings", "logins", "name"]
    assert _open(html) == "modules"                       # an existing package opens on its modules
    assert _open(client.get("/admin/packages/new").get_data(as_text=True)) == "type"   # a new one on who it's for
    assert _open(client.get(f"/admin/packages/{packs['retail'].id}?tab=settings").get_data(as_text=True)) == "settings"
    assert _open(client.get(f"/admin/packages/{packs['retail'].id}?tab=bogus").get_data(as_text=True)) == "modules"


def test_each_kind_has_its_own_tab(client, db, ultra, packs):
    html = client.get(f"/admin/packages/{packs['retail'].id}").get_data(as_text=True)
    trade, modules, counter = _panel(html, "trade"), _panel(html, "modules"), _panel(html, "counter")
    assert 'id="lbl_restaurant_mode"' in trade and 'id="lbl_margin_scheme"' in trade and 'id="lbl_purchases"' not in trade
    assert 'id="lbl_purchases"' in modules and 'id="lbl_favourites"' not in modules and 'name="unlisted_modules"' in modules
    assert 'id="lbl_favourites"' in counter and 'id="lbl_cashier_pin"' in counter
    settings = _panel(html, "settings")
    a = [settings.index(t) for t in (">Business</h4>", ">Look &amp; printing</h4>", ">Counter &amp; sign-in</h4>")]
    assert a == sorted(a) and 'name="set_aato"' in settings
    logins = _panel(html, "logins")
    assert 'name="limit_branches"' in logins and 'name="counter_permission"' in logins and 'name="own_branding"' in logins
    assert 'name="name"' in _panel(html, "name")
    # One save for every tab, outside the panels.
    assert html.count('type="submit" class="btn btn-primary">Save package') == 1


def test_business_types_carry_their_packs_for_the_pre_set(client, db, ultra, packs):
    html = client.get(f"/admin/packages/{packs['pharmacy'].id}").get_data(as_text=True)
    panel = _panel(html, "type")
    assert 'name="business_type" value="pharmacy"' in panel
    assert re.search(r'value="pharmacy" x-model="bt" @change="picked\(\)" checked', panel)
    assert "Pharmacy pack" in panel and "Pre-set" in panel
    # The script gets each type's packs and usual modules.
    assert "&#34;pharmacy&#34;: {&#34;label&#34;" in html and "&#34;pack_names&#34;: [&#34;Pharmacy&#34;]" in html


# --- saving -------------------------------------------------------------------------------------------

def test_saving_keeps_the_tab_and_records_the_business_type(client, db, ultra, packs):
    retail = packs["retail"]
    resp = client.post(f"/admin/packages/{retail.id}", data=_form(retail, business_type="bakery", tab="settings"))
    assert resp.status_code == 302 and resp.headers["Location"].endswith("tab=settings")
    db.session.refresh(retail)
    assert retail.business_type == "bakery"
    log = AuditLog.query.filter_by(action="package_edited").order_by(AuditLog.id.desc()).first()
    assert log.details["before"]["business_type"] == "retail" and log.details["after"]["business_type"] == "bakery"
    client.post(f"/admin/packages/{retail.id}", data=_form(retail, business_type=""))
    db.session.refresh(retail)
    assert retail.business_type is None


def test_an_unknown_business_type_is_refused(client, db, ultra, packs):
    retail = packs["retail"]
    page = client.post(f"/admin/packages/{retail.id}", data=_form(retail, business_type="spaceport"),
                       follow_redirects=True).get_data(as_text=True)
    assert "Pick a business type from the list." in page
    db.session.refresh(retail)
    assert retail.business_type == "retail"


def test_a_copy_keeps_who_it_is_for(client, db, ultra, packs):
    html = client.get(f"/admin/packages/new?copy={packs['restaurant'].id}").get_data(as_text=True)
    assert re.search(r'value="restaurant" x-model="bt" @change="picked\(\)" checked', html)


def test_starting_packages_say_who_they_are_for(db, packs):
    assert packs["pharmacy"].business_type == "pharmacy"
    assert packs["electronics"].business_type == "mobile_electronics"
    assert packs["everything"].business_type is None
    assert {s["key"] for s in STARTING_PACKAGES if s.get("business_type")} == {
        "retail", "restaurant", "pharmacy", "electronics", "wholesale"}


def test_onboarding_offers_a_package_marked_for_the_type(client, db, ultra, packs):
    bakery = Package(name="Bakery Plus", business_type="bakery", modules={}, settings={}, limits={}, sort_order=99)
    db.session.add(bakery)
    db.session.commit()
    html = client.get("/admin/clients/new").get_data(as_text=True)
    suggest = re.search(r"suggest: (\{[^}]*\})", html).group(1)
    assert f'"bakery": {bakery.id}' in suggest
    assert f'"pharmacy": {packs["pharmacy"].id}' in suggest


# --- compare ------------------------------------------------------------------------------------------

def test_compare_shows_packages_side_by_side(client, db, ultra, packs):
    packs["billing"].is_active = False
    db.session.commit()
    html = client.get("/admin/packages/compare").get_data(as_text=True)
    head = html.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert "Retail" in head and "Pharmacy" in head and ">Billing<" not in head
    assert ">Billing<" in client.get("/admin/packages/compare?retired=1").get_data(as_text=True).split("</thead>", 1)[0]
    pharmacy_row = html.split(">Pharmacy</td>", 1)[1].split("</tr>", 1)[0]
    assert 'title="Included"' in pharmacy_row and 'title="Off"' in pharmacy_row
    assert html.index("Trade packs") < html.index(">Selling<") < html.index("Counter options")
    row = html.split(">Pharmacy</td>", 1)[0].rsplit("<tr", 1)[1]
    assert 'class="differs"' in row
    assert "Only differences" in html and "Settings only ARFA changes" in html


def test_only_the_ultra_admin_compares(client, db, packs):
    auditor = User(name="Aud", email="cmp.aud@example.com", role=UserRole.AUDITOR, must_change_password=False,
                   billing_cycle=BillingCycle.MONTHLY)
    auditor.set_password("AuditorPass123")
    db.session.add(auditor)
    db.session.commit()
    _login(client, auditor.email, "AuditorPass123")
    assert client.get("/admin/packages/compare").status_code == 403
