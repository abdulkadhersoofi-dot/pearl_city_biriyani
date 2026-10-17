"""Console cleanup, stage 5: an auditor's page in tabs, onboarding in three
steps, and an audit log that can be searched (client, who, kind, dates) a
page at a time."""

import re
from datetime import datetime, timedelta, timezone

from app.models.audit_log import AuditLog
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.tenants.routes import AUDIT_PER_PAGE, audit_kind
from app.utils.audit import record_audit
from app.utils.entitlements import ensure_starting_packages


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _auditor(db, email="s5.aud@example.com", role=UserRole.AUDITOR, parent=None, billing_cycle=BillingCycle.MONTHLY,
             name="Asha Auditor"):
    u = User(name=name, email=email, role=role, parent_auditor_id=parent.id if parent else None,
             must_change_password=False, billing_cycle=billing_cycle)
    u.set_password("AuditorPass123")
    db.session.add(u)
    db.session.commit()
    return u


def _tabs(html):
    nav = html.split('role="tablist"', 1)[1].split("</nav>", 1)[0]
    return re.findall(r'\?tab=(\w+)"', nav)


def _panel(html, key):
    return html.split(f'<section id="tab-{key}"', 1)[1].split('<section id="tab-', 1)[0]


# --- an auditor's page in tabs ----------------------------------------------------------------------

def test_the_ultra_admin_sees_an_auditor_in_tabs(client, db, super_admin):
    auditor = _auditor(db)
    _auditor(db, "s5.sub@example.com", role=UserRole.SUB_AUDITOR, parent=auditor, name="Sunil Sub")
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get(f"/admin/auditors/{auditor.id}").get_data(as_text=True)
    assert _tabs(html) == ["overview", "powers", "clients", "billing", "changes"]
    assert "x-data=\"pageTabs('overview'" in html
    assert 'id="sec-powers"' in _panel(html, "powers")
    assert "Sunil Sub" in _panel(html, "clients") and 'id="sec-clients"' in _panel(html, "clients")
    assert "Mark this period paid" in _panel(html, "billing")
    head = html.split('class="page-header client-head"', 1)[1].split('role="tablist"', 1)[0]
    assert "Reset password" in head and "Deactivate" in head
    assert "x-data=\"pageTabs('billing'" in client.get(f"/admin/auditors/{auditor.id}?tab=billing").get_data(as_text=True)


def test_a_waiting_auditor_is_verified_from_overview(client, db, super_admin):
    waiting = _auditor(db, "s5.wait@example.com", billing_cycle=None, name="Waiting Person")
    _login(client, super_admin.email, "SuperSecret123")
    overview = _panel(client.get(f"/admin/auditors/{waiting.id}").get_data(as_text=True), "overview")
    assert "Waiting for verification" in overview and "Verify &amp; activate" in overview


def test_an_auditor_sees_their_sub_auditor_without_billing(client, db):
    auditor = _auditor(db)
    sub = _auditor(db, "s5.sub2@example.com", role=UserRole.SUB_AUDITOR, parent=auditor, name="Sunil Sub")
    _login(client, auditor.email, "AuditorPass123")
    html = client.get(f"/admin/auditors/{sub.id}").get_data(as_text=True)
    assert "billing" not in _tabs(html) and 'id="tab-billing"' not in html
    assert "powers" in _tabs(html)
    assert "Reset password" not in html.split('role="tablist"', 1)[0]


def test_auditor_actions_return_to_their_tab(client, db, super_admin):
    auditor = _auditor(db)
    _login(client, super_admin.email, "SuperSecret123")
    assert "tab=billing" in client.post(f"/admin/auditors/{auditor.id}/billing/mark-paid").headers["Location"]
    assert "tab=billing" in client.post(f"/admin/auditors/{auditor.id}/billing/trigger-alarm").headers["Location"]
    resp = client.post(f"/admin/auditors/{auditor.id}/powers", data={"power": ["act_as"]})
    assert "tab=powers" in resp.headers["Location"]


# --- onboarding in three steps ------------------------------------------------------------------------

def test_onboarding_has_three_steps_with_the_package_described(client, db, super_admin):
    ensure_starting_packages()
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get("/admin/clients/new").get_data(as_text=True)
    for n in (1, 2, 3):
        assert f'data-step="{n}"' in html
    assert "onboardSteps(1," in html
    step2 = html.split('data-step="2"', 1)[1].split('data-step="3"', 1)[0]
    assert 'name="package_id"' in step2 and "package-peek" in step2
    assert "included" in html and "&#34;for&#34;: &#34;Pharmacy / medical store&#34;" in html
    step1 = html.split('data-step="1"', 1)[1].split('data-step="2"', 1)[0]
    assert 'name="legal_name"' in step1 and 'name="business_type"' in step1
    assert 'name="admin_email"' in html.split('data-step="3"', 1)[1]


def test_an_error_opens_the_step_that_holds_it(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    base = {"legal_name": "New Co", "registration_type": "regular", "state_code": "27", "business_type": "retail",
            "admin_name": "Owner", "admin_email": "not-an-email", "admin_password": "Password123",
            "admin_confirm_password": "Password123"}
    html = client.post("/admin/clients/new", data=base).get_data(as_text=True)
    assert "onboardSteps(3," in html
    html = client.post("/admin/clients/new", data={**base, "legal_name": "", "admin_email": "a@b.example.com"}).get_data(as_text=True)
    assert "onboardSteps(1," in html


def test_three_steps_still_onboard_one_client(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post("/admin/clients/new", data={
        "legal_name": "Stepwise Stores", "registration_type": "regular", "state_code": "27", "business_type": "bakery",
        "admin_name": "Owner", "admin_email": "owner@stepwise.example.com", "admin_password": "Password123",
        "admin_confirm_password": "Password123"})
    assert resp.status_code == 302
    tenant = Tenant.query.filter_by(legal_name="Stepwise Stores").one()
    assert tenant.billing_cycle is None and tenant.has_feature("weighing")


# --- the audit log ---------------------------------------------------------------------------------------

def _client(db, name):
    t = Tenant(legal_name=name, registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(t)
    db.session.flush()
    db.session.add(Gstin(tenant_id=t.id, gstin=None, state_code="27", state_name="Maharashtra", is_primary=True))
    db.session.commit()
    return t


def test_actions_fall_into_kinds():
    assert audit_kind("login_success") == "signin" and audit_kind("two_factor_failed") == "signin"
    assert audit_kind("package_assigned") == "access" and audit_kind("auditor_powers_set") == "access"
    assert audit_kind("client_billing_marked_paid") == "billing" and audit_kind("platform_invoice_issued") == "billing"
    assert audit_kind("view_client_data") == "views" and audit_kind("tally_export") == "views"
    assert audit_kind("client_onboarded") == "console" and audit_kind("auditor_created") == "console"
    assert audit_kind("invoice_created") == "records" and audit_kind("something_new") == "records"


def test_the_audit_log_filters_by_client_who_kind_and_date(client, db, super_admin):
    acme, other = _client(db, "Acme Log Co"), _client(db, "Other Log Co")
    auditor = _auditor(db)
    record_audit(super_admin, "client_billing_marked_paid", tenant_id=acme.id, entity_type="tenant", entity_id=acme.id)
    record_audit(auditor, "client_deactivated", tenant_id=other.id, entity_type="tenant", entity_id=other.id)
    old = AuditLog(actor_user_id=super_admin.id, action="client_activated", tenant_id=other.id,
                   created_at=datetime.now(timezone.utc) - timedelta(days=40))
    db.session.add(old)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")

    def rows(**query):
        html = client.get("/admin/audit-log", query_string=query).get_data(as_text=True)
        return html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    by_client = rows(client=acme.id)
    assert "Acme Log Co" in by_client and "Other Log Co" not in by_client
    by_who = rows(who=auditor.id)
    assert "Client deactivated" in by_who and "Client billing marked paid" not in by_who
    billing = rows(kind="billing")
    assert "Client billing marked paid" in billing and "Client deactivated" not in billing
    recent = rows(**{"from": (datetime.now().date() - timedelta(days=7)).isoformat()})
    assert "Client activated" not in recent and "Client deactivated" in recent
    assert "Nothing matches these filters." in rows(client=acme.id, kind="signin")


def test_the_audit_log_pages_fifty_at_a_time(client, db, super_admin):
    for _ in range(AUDIT_PER_PAGE + 5):
        db.session.add(AuditLog(actor_user_id=super_admin.id, action="invoice_created"))
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    first = client.get("/admin/audit-log?kind=records").get_data(as_text=True)
    assert first.split("<tbody>", 1)[1].count("<tr>") == AUDIT_PER_PAGE
    assert "Page 1 of 2" in first and "page=2" in first and "kind=records" in first.split('rel="next"', 1)[0].rsplit("<a", 1)[1]
    second = client.get("/admin/audit-log?kind=records&page=2").get_data(as_text=True)
    assert second.split("<tbody>", 1)[1].count("<tr>") == 5 and "Page 2 of 2" in second
    assert "Page 2 of 2" in client.get("/admin/audit-log?kind=records&page=99").get_data(as_text=True)


def test_only_the_ultra_admin_reads_the_audit_log(client, db):
    auditor = _auditor(db)
    _login(client, auditor.email, "AuditorPass123")
    assert client.get("/admin/audit-log").status_code == 403
