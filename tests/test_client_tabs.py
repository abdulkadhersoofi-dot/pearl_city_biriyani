"""Console cleanup, stage 3: a client's page in tabs - Overview, Package &
modules, Users & branches, Billing (Ultra Admin), Documents, Changes - one
shown at a time, ?tab= opening one, actions sending you back to the tab
they belong to, and "Act as", pausing and the login resets in menus."""

import re

import pytest

from app.models.package import ModuleRequest, Package
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.utils.entitlements import assign_package, ensure_starting_packages, set_module_exception


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def ultra(client, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    return super_admin


def _auditor(db, email="tabs.aud@example.com", role=UserRole.AUDITOR, parent=None):
    u = User(name="Tab Auditor", email=email, role=role, parent_auditor_id=parent.id if parent else None,
             must_change_password=False, billing_cycle=BillingCycle.MONTHLY)
    u.set_password("AuditorPass123")
    db.session.add(u)
    db.session.commit()
    return u


def _tabs(html):
    nav = html.split('role="tablist"', 1)[1].split("</nav>", 1)[0]
    return re.findall(r'\?tab=(\w+)"', nav)


def _shown(html):
    """The panel the server opens the page on (the rest start hidden)."""
    panels = re.findall(r'<section id="tab-(\w+)"[^>]*?x-show="tab === \'\w+\'"( x-cloak)?\s*>', html)
    return [key for key, hidden in panels if not hidden]


def _panel(html, key):
    return html.split(f'<section id="tab-{key}"', 1)[1].split('<section id="tab-', 1)[0]


# --- tabs ---------------------------------------------------------------------------------------

def test_the_ultra_admin_sees_six_tabs_and_lands_on_overview(client, db, tenant, ultra):
    html = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    assert _tabs(html) == ["overview", "package", "users", "billing", "documents", "changes"]
    assert _shown(html) == ["overview"]
    assert "x-data=\"clientPage('overview')\"" in html


def test_a_tab_opens_from_the_address(client, db, tenant, ultra):
    html = client.get(f"/admin/clients/{tenant.id}?tab=users").get_data(as_text=True)
    assert _shown(html) == ["users"]
    assert 'id="sec-users"' in _panel(html, "users")
    assert _shown(client.get(f"/admin/clients/{tenant.id}?tab=nonsense").get_data(as_text=True)) == ["overview"]


def test_each_section_sits_in_its_tab(client, db, tenant, ultra):
    html = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    where = {"sec-allocation": "overview", "sec-locations": "overview", "sec-branding": "overview",
             "sec-package": "package", "sec-users": "users", "sec-access": "billing",
             "sec-month-end": "documents", "sec-activity": "documents", "sec-changes": "changes"}
    for section, tab in where.items():
        assert f'id="{section}"' in _panel(html, tab), section
    # ...and the script knows where an old #sec- link should go.
    for section, tab in where.items():
        assert f'"{section}": "{tab}"' in html


def test_an_auditor_sees_only_their_tabs(client, db, tenant):
    auditor = _auditor(db)
    tenant.auditor_id = auditor.id
    db.session.commit()
    _login(client, auditor.email, "AuditorPass123")
    html = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    assert "billing" not in _tabs(html) and 'id="tab-billing"' not in html
    assert _tabs(html)[0] == "overview" and "users" in _tabs(html)
    assert 'id="sec-allocation"' not in html
    assert _shown(client.get(f"/admin/clients/{tenant.id}?tab=billing").get_data(as_text=True)) == ["overview"]


def test_an_auditor_hands_a_client_to_a_sub_auditor_from_overview(client, db, tenant):
    auditor = _auditor(db)
    _auditor(db, "tabs.sub@example.com", role=UserRole.SUB_AUDITOR, parent=auditor)
    tenant.auditor_id = auditor.id
    db.session.commit()
    _login(client, auditor.email, "AuditorPass123")
    html = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    assert 'id="sec-sub-auditor"' in _panel(html, "overview")


# --- going back to the right tab -----------------------------------------------------------------

def test_actions_return_to_their_tab(client, db, tenant, client_admin, ultra):
    ensure_starting_packages()
    db.session.commit()
    retail = Package.query.filter_by(key="retail").first()
    resp = client.post(f"/admin/clients/{tenant.id}/billing/mark-paid")
    assert "tab=billing" in resp.headers["Location"]
    resp = client.post(f"/admin/clients/{tenant.id}/package", data={"package_id": retail.id})
    assert "tab=package" in resp.headers["Location"] and resp.headers["Location"].endswith("#sec-package")
    resp = client.post(f"/admin/clients/{tenant.id}/users/{client_admin.id}/deactivate")
    assert "tab=users" in resp.headers["Location"]
    resp = client.post(f"/admin/clients/{tenant.id}/users/{client_admin.id}/activate")
    assert "tab=users" in resp.headers["Location"]
    req = ModuleRequest(tenant_id=tenant.id, feature_key="loyalty", status=ModuleRequest.PENDING)
    db.session.add(req)
    db.session.commit()
    resp = client.post(f"/admin/clients/{tenant.id}/requests/{req.id}", data={"action": "decline"})
    assert "tab=package" in resp.headers["Location"]


# --- menus ----------------------------------------------------------------------------------------

def test_act_as_lists_only_active_owner_and_branch_logins(client, db, tenant, client_admin, staff, ultra):
    staff.is_active = False
    db.session.commit()
    html = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    head = html.split('class="page-header client-head"', 1)[1].split('role="tablist"', 1)[0]
    menu = head.split("Sign in as", 1)[1].split("</div>", 1)[0]
    assert client_admin.name in menu and f"/users/{client_admin.id}/act-as" in menu
    assert f"/users/{staff.id}/act-as" not in menu
    assert "Pause access" in head


def test_login_resets_sit_in_each_logins_menu(client, db, tenant, client_admin, ultra):
    users = _panel(client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True), "users")
    row = users.split(client_admin.email, 1)[1].split("</tr>", 1)[0]
    menu = row.split('role="menu"', 1)[1]
    assert "Reset password" in menu and "Deactivate" in menu
    assert ">Act as</button>" in row.split('role="menu"', 1)[0]


# --- overview and the package tab -----------------------------------------------------------------

def test_overview_points_at_a_waiting_request(client, db, tenant, ultra):
    ensure_starting_packages()
    db.session.commit()
    assign_package(tenant, Package.query.filter_by(key="retail").first(), ultra)
    db.session.add(ModuleRequest(tenant_id=tenant.id, feature_key="loyalty", status=ModuleRequest.PENDING))
    db.session.commit()
    html = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    assert "asked for Loyalty points" in _panel(html, "overview")
    nav = html.split('role="tablist"', 1)[1].split("</nav>", 1)[0]
    package_tab = nav.split("?tab=package", 1)[1].split("</a>", 1)[0]
    assert '<span class="tab-count is-alert">1</span>' in package_tab
    facts = _panel(html, "overview").split('class="client-facts"', 1)[1].split("</dl>", 1)[0]
    assert "Retail" in facts and "Not allotted" in facts


def test_waiting_for_verification_shows_on_overview(client, db, super_admin):
    pending = Tenant(legal_name="New Shop", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id)
    db.session.add(pending)
    db.session.flush()
    db.session.add(Gstin(tenant_id=pending.id, gstin=None, state_code="27", state_name="Maharashtra", is_primary=True))
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get(f"/admin/clients/{pending.id}").get_data(as_text=True)
    overview = _panel(html, "overview")
    assert "Waiting for verification" in overview and "Verify &amp; activate" in overview


def test_the_exceptions_table_starts_on_what_differs(client, db, tenant, ultra):
    ensure_starting_packages()
    db.session.commit()
    assign_package(tenant, Package.query.filter_by(key="retail").first(), ultra)
    db.session.commit()
    plain = _panel(client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True), "package")
    assert "x-data=\"{ only: false }\"" in plain and "Only what differs" not in plain
    set_module_exception(tenant, "loyalty", "inc", ultra)
    db.session.commit()
    changed = _panel(client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True), "package")
    assert "x-data=\"{ only: true }\"" in changed and "Only what differs" in changed
    assert ":class=\"{ 'only-changed': only }\"" in changed
