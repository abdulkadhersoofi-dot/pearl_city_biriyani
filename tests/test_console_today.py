"""Console cleanup, stage 2: the Today page every console login lands on,
the shorter side menu, the Clients list's filter chips (with "no auditor"
moved there) and the plain Auditors list (verifying moved to Today)."""

import html as html_lib
import re
from datetime import date, timedelta

from app.models.package import ModuleRequest
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.utils.audit import record_audit


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _auditor(db, email, role=UserRole.AUDITOR, parent=None, billing_cycle=BillingCycle.MONTHLY, name="Asha Auditor"):
    u = User(name=name, email=email, role=role, parent_auditor_id=parent.id if parent else None,
             must_change_password=False, billing_cycle=billing_cycle)
    u.set_password("AuditorPass123")
    db.session.add(u)
    db.session.commit()
    return u


def _client(db, name, auditor=None, billing_cycle=BillingCycle.MONTHLY, **kw):
    t = Tenant(legal_name=name, registration_type=RegistrationType.REGULAR, billing_cycle=billing_cycle,
               auditor_id=auditor.id if auditor else None, **kw)
    db.session.add(t)
    db.session.flush()
    db.session.add(Gstin(tenant_id=t.id, gstin=None, state_code="27", state_name="Maharashtra", is_primary=True))
    db.session.commit()
    return t


def _menu(html):
    return re.findall(r'class="nav-label">([^<]+)<', html)


def _needs(html):
    block = html.split('id="needs-title"', 1)[1].split('id="changes-title"', 1)[0]
    return [html_lib.unescape(re.sub(r"\s+", " ", t).strip()) for t in re.findall(r"<strong>([^<]+)</strong>", block)]


# --- landing --------------------------------------------------------------------------------------

def test_every_console_login_lands_on_today(client, db, super_admin):
    resp = _login(client, super_admin.email, "SuperSecret123")
    assert resp.request.path == "/admin/today"
    assert client.get("/").headers["Location"].endswith("/admin/today")
    client.get("/auth/logout")
    auditor = _auditor(db, "today.aud@example.com")
    assert _login(client, auditor.email, "AuditorPass123").request.path == "/admin/today"
    client.get("/auth/logout")
    sub = _auditor(db, "today.sub@example.com", role=UserRole.SUB_AUDITOR, parent=auditor, name="Sunil Sub")
    assert _login(client, sub.email, "AuditorPass123").request.path == "/admin/today"


def test_clients_never_reach_today(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get("/admin/today").status_code in (403, 404)


def test_the_ultra_admins_menu_is_short(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get("/admin/today").get_data(as_text=True)
    assert _menu(html) == ["Today", "Clients", "Auditors", "Filing &amp; GST", "Packages", "Sign-in domains",
                             "Billing", "Audit log"]
    assert 'aria-current="page"' in html.split('class="nav-label">Today', 1)[0].rsplit("<a", 1)[1]


def test_filing_and_gst_are_one_entry_with_two_tabs(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    for url in ("/admin/filing", "/admin/gst-exports"):
        html = client.get(url).get_data(as_text=True)
        assert 'aria-label="Filing and GST"' in html
        entry = html.split('class="nav-label">Filing &amp; GST', 1)[0].rsplit("<a", 1)[1]
        assert 'aria-current="page"' in entry


def test_an_auditors_menu_follows_their_powers(client, db):
    auditor = _auditor(db, "menu.aud@example.com")
    _login(client, auditor.email, "AuditorPass123")
    assert _menu(client.get("/admin/today").get_data(as_text=True)) == [
        "Today", "Clients", "Sub-Auditors", "Filing", "Subscription"]
    assert 'aria-label="Filing and GST"' not in client.get("/admin/filing").get_data(as_text=True)


# --- what Today shows ------------------------------------------------------------------------------

def test_today_lists_what_needs_the_ultra_admin(client, db, super_admin):
    auditor = _auditor(db, "needs.aud@example.com")
    _client(db, "Waiting Shop", billing_cycle=None)
    _auditor(db, "waiting.aud@example.com", billing_cycle=None, name="Waiting Auditor")
    nobody = _client(db, "Orphan Traders")
    _client(db, "Paused Mart", auditor=auditor, is_active=False)
    _client(db, "Due Stores", auditor=auditor, next_billing_due=date.today() - timedelta(days=1))
    asker = _client(db, "Asking Co", auditor=auditor)
    db.session.add(ModuleRequest(tenant_id=asker.id, feature_key="loyalty", status=ModuleRequest.PENDING))
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get("/admin/today").get_data(as_text=True)
    needs = _needs(html)
    assert needs[:4] == ["1 client waiting to be verified", "1 auditor waiting to be verified",
                         "1 module request to decide", "1 client with no auditor"]
    assert "1 client can't sign in" in needs and "1 renewal due" in needs
    assert "Waiting Shop" in html and "Waiting Auditor" in html and "asks for Loyalty points" in html
    assert f"/admin/clients/{nobody.id}" in html
    assert "/admin/clients?show=no_auditor" in html and "/admin/clients?show=pending" in html


def test_an_auditor_sees_only_their_own_clients(client, db):
    mine = _auditor(db, "mine.aud@example.com")
    other = _auditor(db, "other.aud@example.com", name="Other Auditor")
    _client(db, "My Paused Shop", auditor=mine, is_active=False)
    _client(db, "My New Shop", auditor=mine, billing_cycle=None)
    _client(db, "Not Mine Paused", auditor=other, is_active=False)
    _client(db, "Nobody's Shop")
    _login(client, mine.email, "AuditorPass123")
    html = client.get("/admin/today").get_data(as_text=True)
    assert "My Paused Shop" in html and "My New Shop" in html
    assert "Not Mine Paused" not in html and "Nobody's Shop" not in html
    needs = _needs(html)
    assert "1 client waiting for ARFA to verify" in needs      # informational - verifying is ARFA's
    assert not any("module request" in n or "no auditor" in n for n in needs)


def test_nothing_waiting_says_so(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    assert "Nothing needs you today." in client.get("/admin/today").get_data(as_text=True)


def test_changed_today_lists_changes_but_not_views_or_sign_ins(client, db, super_admin, tenant):
    record_audit(super_admin, "client_billing_marked_paid", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id)
    record_audit(super_admin, "view_client_data", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get("/admin/today").get_data(as_text=True)
    feed = html.split('id="changes-title"', 1)[1]
    assert "Marked paid for the period" in feed and "Acme Traders" in feed
    assert "View client data" not in feed and "Login success" not in feed
    tile = html.split("<small>Changes today</small>", 1)[0].rsplit("data-count-up=", 1)[1]
    assert tile.startswith('"1"')


def test_an_auditor_does_not_see_other_clients_changes(client, db, super_admin):
    mine = _auditor(db, "feed.aud@example.com")
    theirs = _client(db, "Someone Else's", auditor=_auditor(db, "feed.other@example.com"))
    record_audit(super_admin, "client_deactivated", tenant_id=theirs.id, entity_type="tenant", entity_id=theirs.id)
    db.session.commit()
    _login(client, mine.email, "AuditorPass123")
    feed = client.get("/admin/today").get_data(as_text=True).split('id="changes-title"', 1)[1]
    assert "Someone Else" not in feed


# --- the lists --------------------------------------------------------------------------------------

def test_clients_list_opens_on_the_chip_asked_for(client, db, super_admin):
    _client(db, "Orphan Traders")
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get("/admin/clients?show=no_auditor").get_data(as_text=True)
    assert "show: 'no_auditor'" in html
    assert "show: 'all'" in client.get("/admin/clients?show=nonsense").get_data(as_text=True)


def test_auditors_cannot_filter_by_no_auditor(client, db):
    auditor = _auditor(db, "chip.aud@example.com")
    _login(client, auditor.email, "AuditorPass123")
    html = client.get("/admin/clients?show=no_auditor").get_data(as_text=True)
    assert "show: 'all'" in html and "No auditor <b>" not in html


def test_the_auditors_page_is_a_plain_list(client, db, super_admin):
    _client(db, "Waiting Shop", billing_cycle=None)
    _client(db, "Orphan Traders")
    _auditor(db, "plain.aud@example.com", billing_cycle=None, name="Pending Person")
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get("/admin/auditors").get_data(as_text=True)
    assert "Pending Person" in html and "Awaiting verification" in html
    assert "Waiting Shop" not in html and "Orphan Traders" not in html
    assert "Unallocated clients" not in html
    assert "show: 'pending'" in client.get("/admin/auditors?show=pending").get_data(as_text=True)
