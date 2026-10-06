from datetime import date, timedelta

import pytest

from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.utils.billing import BILLING_GRACE_DAYS, add_one_month, tenant_access_status


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


# --- add_one_month -----------------------------------------------------

def test_add_one_month_normal_case():
    assert add_one_month(date(2026, 3, 15)) == date(2026, 4, 15)


def test_add_one_month_clamps_to_shorter_month():
    assert add_one_month(date(2026, 1, 31)) == date(2026, 2, 28)


def test_add_one_month_leap_year_february():
    assert add_one_month(date(2028, 1, 31)) == date(2028, 2, 29)


def test_add_one_month_rolls_over_year():
    assert add_one_month(date(2026, 12, 10)) == date(2027, 1, 10)


# --- tenant_access_status -----------------------------------------------

def test_access_status_active_by_default(tenant):
    status = tenant_access_status(tenant)
    assert status.blocked is False
    assert status.billing_notice is False


def test_access_status_paused_blocks(tenant):
    tenant.is_active = False
    status = tenant_access_status(tenant)
    assert status.blocked is True
    assert status.reason == "paused"


def test_access_status_expired_blocks(tenant):
    tenant.valid_until = date.today() - timedelta(days=1)
    status = tenant_access_status(tenant)
    assert status.blocked is True
    assert status.reason == "expired"


def test_access_status_valid_until_in_future_does_not_block(tenant):
    tenant.valid_until = date.today() + timedelta(days=1)
    status = tenant_access_status(tenant)
    assert status.blocked is False


def test_access_status_billing_due_within_grace_is_a_notice_not_a_block(tenant):
    tenant.next_billing_due = date.today() - timedelta(days=1)
    status = tenant_access_status(tenant)
    assert status.blocked is False
    assert status.billing_notice is True
    assert str(BILLING_GRACE_DAYS - 1) in status.billing_notice_message or "day" in status.billing_notice_message


def test_access_status_billing_overdue_past_grace_blocks(tenant):
    tenant.next_billing_due = date.today() - timedelta(days=BILLING_GRACE_DAYS + 1)
    status = tenant_access_status(tenant)
    assert status.blocked is True
    assert status.reason == "billing_overdue"


def test_access_status_billing_due_exactly_on_grace_boundary_not_blocked(tenant):
    tenant.next_billing_due = date.today() - timedelta(days=BILLING_GRACE_DAYS)
    status = tenant_access_status(tenant)
    assert status.blocked is False
    assert status.billing_notice is True


def test_access_status_none_fields_never_block(tenant):
    tenant.valid_until = None
    tenant.next_billing_due = None
    status = tenant_access_status(tenant)
    assert status.blocked is False
    assert status.billing_notice is False


def test_paused_takes_priority_reason_over_expired(tenant):
    tenant.is_active = False
    tenant.valid_until = date.today() - timedelta(days=5)
    status = tenant_access_status(tenant)
    assert status.reason == "paused"


# --- Login gating --------------------------------------------------------

def test_paused_tenant_client_admin_cannot_log_in(client, db, tenant, client_admin):
    tenant.is_active = False
    db.session.commit()
    resp = _login(client, client_admin.email, "ClientSecret123")
    assert resp.status_code == 200
    assert b"paused" in resp.data.lower()
    assert resp.request.path == "/auth/login"


def test_expired_tenant_branch_cannot_log_in(client, db, tenant, staff):
    tenant.valid_until = date.today() - timedelta(days=1)
    db.session.commit()
    resp = _login(client, staff.email, "CashierSecret123")
    assert resp.status_code == 200
    assert b"expired" in resp.data.lower()


def test_billing_overdue_past_grace_blocks_login(client, db, tenant, client_admin):
    tenant.next_billing_due = date.today() - timedelta(days=BILLING_GRACE_DAYS + 1)
    db.session.commit()
    resp = _login(client, client_admin.email, "ClientSecret123")
    assert b"grace period has ended" in resp.data.lower()


def test_billing_notice_within_grace_still_allows_login(client, db, tenant, client_admin):
    tenant.next_billing_due = date.today() - timedelta(days=1)
    db.session.commit()
    resp = _login(client, client_admin.email, "ClientSecret123")
    assert resp.status_code == 200
    assert resp.request.path != "/auth/login"
    assert b"Monthly renewal was due" in resp.data


def test_super_admin_login_unaffected_by_tenant_access_rules(client, db, super_admin):
    resp = _login(client, super_admin.email, "SuperSecret123")
    assert resp.status_code == 200
    assert resp.request.path != "/auth/login"


# --- Mid-session cutoff (before_request enforcement) ---------------------

def test_already_logged_in_session_is_cut_off_once_tenant_is_paused(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/invoices/")
    assert resp.status_code == 200

    tenant.is_active = False
    db.session.commit()

    resp = client.get("/invoices/", follow_redirects=True)
    assert resp.request.path == "/auth/login"
    assert b"paused" in resp.data.lower()

    # And the session really was logged out, not just redirected once.
    resp = client.get("/invoices/", follow_redirects=True)
    assert resp.request.path == "/auth/login"


def test_super_admin_browsing_is_never_cut_off(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.get("/admin/clients")
    assert resp.status_code == 200


# --- Onboarding sets validity fields --------------------------------------

def test_onboarding_sets_valid_until_from_form(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    future = (date.today() + timedelta(days=200)).isoformat()
    client.post(
        "/admin/clients/new",
        data={
            "legal_name": "Validity Test Co",
            "registration_type": "regular",
            "state_code": "27",
            "gstin": "",
            "valid_until": future,
            "admin_name": "Admin",
            "admin_email": "validity@example.com",
            "admin_phone": "",
            "admin_password": "InitialPass123",
            "admin_confirm_password": "InitialPass123",
        },
    )
    tenant = Tenant.query.filter_by(legal_name="Validity Test Co").first()
    assert tenant is not None
    assert tenant.valid_until == date.today() + timedelta(days=200)
    assert tenant.next_billing_due == add_one_month(date.today())


def test_onboarding_rejects_a_blank_valid_until(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    client.post(
        "/admin/clients/new",
        data={
            "legal_name": "No Validity Co",
            "registration_type": "regular",
            "state_code": "27",
            "gstin": "",
            "valid_until": "",
            "admin_name": "Admin",
            "admin_email": "novalidity@example.com",
            "admin_phone": "",
            "admin_password": "InitialPass123",
            "admin_confirm_password": "InitialPass123",
        },
    )
    assert Tenant.query.filter_by(legal_name="No Validity Co").first() is None


# --- Super Admin renewal / billing actions --------------------------------

def test_super_admin_can_renew_access(client, db, super_admin, tenant):
    _login(client, super_admin.email, "SuperSecret123")
    new_date = (date.today() + timedelta(days=400)).isoformat()
    resp = client.post(
        f"/admin/clients/{tenant.id}/renew", data={"valid_until": new_date}, follow_redirects=True
    )
    assert resp.status_code == 200
    db.session.refresh(tenant)
    assert tenant.valid_until == date.today() + timedelta(days=400)


def test_client_admin_cannot_renew_their_own_access(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        f"/admin/clients/{tenant.id}/renew", data={"valid_until": "2099-01-01"}
    )
    assert resp.status_code == 403


def test_mark_billing_paid_advances_from_current_due_date_not_today(client, db, super_admin, tenant):
    tenant.next_billing_due = date(2026, 1, 15)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    client.post(f"/admin/clients/{tenant.id}/billing/mark-paid", follow_redirects=True)
    db.session.refresh(tenant)
    assert tenant.next_billing_due == date(2026, 2, 15)


def test_mark_billing_paid_starts_a_cycle_when_none_tracked(client, db, super_admin, tenant):
    tenant.next_billing_due = None
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    client.post(f"/admin/clients/{tenant.id}/billing/mark-paid", follow_redirects=True)
    db.session.refresh(tenant)
    assert tenant.next_billing_due == add_one_month(date.today())


# --- Act as (impersonation) -----------------------------------------------

def test_super_admin_can_act_as_client_admin_and_reach_invoicing(client, db, super_admin, tenant, client_admin):
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(f"/admin/clients/{tenant.id}/users/{client_admin.id}/act-as", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.request.path == "/invoices/"

    resp = client.get("/invoices/new")
    assert resp.status_code == 200


def test_super_admin_can_act_as_branch_and_reach_pos(client, db, super_admin, tenant, staff):
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(f"/admin/clients/{tenant.id}/users/{staff.id}/act-as", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.request.path == "/pos/"


def test_non_super_admin_cannot_act_as_anyone(client, db, tenant, client_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(f"/admin/clients/{tenant.id}/users/{staff.id}/act-as")
    assert resp.status_code == 403


def test_stop_impersonating_returns_to_admin_console(client, db, super_admin, tenant, client_admin):
    _login(client, super_admin.email, "SuperSecret123")
    client.post(f"/admin/clients/{tenant.id}/users/{client_admin.id}/act-as")

    resp = client.get("/auth/stop-impersonating", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.request.path == "/admin/clients"

    # Back to being the super admin - can reach the admin console again.
    resp = client.get("/admin/clients")
    assert resp.status_code == 200


def test_impersonation_bypasses_a_paused_tenants_cutoff(client, db, super_admin, tenant, client_admin):
    tenant.is_active = False
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(f"/admin/clients/{tenant.id}/users/{client_admin.id}/act-as", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.request.path == "/invoices/"

    resp = client.get("/invoices/new")
    assert resp.status_code == 200


def test_act_as_rejects_a_user_from_a_different_tenant(client, db, super_admin, tenant, staff):
    from app.models.tenant import Gstin, RegistrationType

    other_tenant = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR)
    db.session.add(other_tenant)
    db.session.commit()

    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(f"/admin/clients/{other_tenant.id}/users/{staff.id}/act-as")
    assert resp.status_code == 404


# --- Admin-side branch/user activate/deactivate ---------------------------

def test_super_admin_can_deactivate_and_reactivate_a_branch(client, db, super_admin, tenant, staff):
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(f"/admin/clients/{tenant.id}/users/{staff.id}/deactivate", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(staff)
    assert staff.is_active is False

    resp = client.post(f"/admin/clients/{tenant.id}/users/{staff.id}/activate", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(staff)
    assert staff.is_active is True


def test_client_admin_cannot_use_admin_side_user_toggle(client, db, tenant, client_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(f"/admin/clients/{tenant.id}/users/{staff.id}/deactivate")
    assert resp.status_code == 403


def test_deactivated_branch_cannot_log_in(client, db, super_admin, tenant, staff):
    _login(client, super_admin.email, "SuperSecret123")
    client.post(f"/admin/clients/{tenant.id}/users/{staff.id}/deactivate")
    client.get("/auth/logout")

    resp = _login(client, staff.email, "CashierSecret123")
    assert resp.request.path == "/auth/login"
    assert b"incorrect" in resp.data.lower()
