from datetime import date, timedelta

import pytest

from app.models.tenant import BillingCycle, Tenant
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


def test_access_status_none_next_billing_due_never_blocks(tenant):
    tenant.next_billing_due = None
    status = tenant_access_status(tenant)
    assert status.blocked is False
    assert status.billing_notice is False


def test_paused_takes_priority_reason_over_billing_overdue(tenant):
    tenant.is_active = False
    tenant.next_billing_due = date.today() - timedelta(days=BILLING_GRACE_DAYS + 5)
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

def test_onboarding_leaves_the_client_pending_with_no_billing_cycle(client, db, super_admin):
    # Billing selection always stays with the Ultra Admin, even when the
    # Ultra Admin themselves is the one onboarding - it only happens at
    # verification, never at onboarding.
    _login(client, super_admin.email, "SuperSecret123")
    client.post(
        "/admin/clients/new",
        data={
            "legal_name": "Validity Test Co",
            "registration_type": "regular",
            "state_code": "27",
            "gstin": "",
            "admin_name": "Admin",
            "admin_email": "validity@example.com",
            "admin_phone": "",
            "admin_password": "InitialPass123",
            "admin_confirm_password": "InitialPass123",
        },
    )
    tenant = Tenant.query.filter_by(legal_name="Validity Test Co").first()
    assert tenant is not None
    assert tenant.billing_cycle is None
    assert tenant.next_billing_due is None


def test_set_billing_cycle_verifies_a_pending_client(client, db, super_admin, tenant):
    tenant.billing_cycle = None
    db.session.commit()

    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(
        f"/admin/clients/{tenant.id}/billing/set-cycle",
        data={"billing_cycle": "monthly"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    db.session.refresh(tenant)
    assert tenant.billing_cycle.value == "monthly"
    # The recurring due date is still not set - that only happens on the
    # Client Admin's first post-password-change login.
    assert tenant.next_billing_due is None


def test_set_billing_cycle_rejects_a_blank_cycle(client, db, super_admin, tenant):
    tenant.billing_cycle = None
    db.session.commit()

    _login(client, super_admin.email, "SuperSecret123")
    client.post(f"/admin/clients/{tenant.id}/billing/set-cycle", data={"billing_cycle": ""})
    db.session.refresh(tenant)
    assert tenant.billing_cycle is None


def test_set_billing_cycle_changes_an_already_active_client_from_monthly_to_yearly(client, db, super_admin, tenant):
    tenant.billing_cycle = BillingCycle.MONTHLY
    db.session.commit()

    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(
        f"/admin/clients/{tenant.id}/billing/set-cycle",
        data={"billing_cycle": "yearly"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(tenant)
    assert tenant.billing_cycle.value == "yearly"


def test_client_admin_cannot_change_their_own_billing_cycle(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(f"/admin/clients/{tenant.id}/billing/set-cycle", data={"billing_cycle": "yearly"})
    assert resp.status_code == 403


def test_pending_verification_blocks_login(client, db, super_admin, tenant, client_admin):
    tenant.billing_cycle = None
    client_admin.must_change_password = False
    db.session.commit()

    resp = _login(client, client_admin.email, "ClientSecret123")
    assert b"awaiting verification" in resp.data.lower()


# --- Super Admin billing actions -------------------------------------------

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


def test_mark_billing_paid_records_last_paid_on(client, db, super_admin, tenant):
    tenant.next_billing_due = date.today() - timedelta(days=1)
    tenant.last_paid_on = None
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    client.post(f"/admin/clients/{tenant.id}/billing/mark-paid")
    db.session.refresh(tenant)
    assert tenant.last_paid_on == date.today()


def test_mark_billing_paid_button_is_never_hidden_even_when_already_paid_ahead(client, db, super_admin, tenant):
    # The button stays clickable at all times - no plain-text swap - the
    # route's own idempotency guards are what make repeat clicks safe.
    tenant.next_billing_due = date.today() + timedelta(days=20)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.get(f"/admin/clients/{tenant.id}")
    assert b"Mark this period paid" in resp.data
    assert b"Resync to live calendar" in resp.data


def test_mark_billing_paid_is_idempotent_against_repeated_clicks(client, db, super_admin, tenant):
    # The bug this guards against: clicking "mark paid" 4 times in a row
    # (e.g. a double-click, or an impatient admin) must advance the
    # cycle once, not 4 times.
    tenant.next_billing_due = date.today() - timedelta(days=1)
    db.session.commit()
    expected = add_one_month(tenant.next_billing_due)

    _login(client, super_admin.email, "SuperSecret123")
    for _ in range(4):
        client.post(f"/admin/clients/{tenant.id}/billing/mark-paid", follow_redirects=True)
        db.session.refresh(tenant)

    assert tenant.next_billing_due == expected


def test_mark_billing_paid_second_click_shows_already_paid_message(client, db, super_admin, tenant):
    tenant.next_billing_due = date.today() - timedelta(days=1)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")

    client.post(f"/admin/clients/{tenant.id}/billing/mark-paid")
    resp = client.post(f"/admin/clients/{tenant.id}/billing/mark-paid", follow_redirects=True)
    # last_paid_on == today fires first, ahead of the next_billing_due
    # comparison - both are "nothing to do" outcomes, but this is the
    # more specific one.
    assert b"already marked paid today" in resp.data.lower()


def test_mark_billing_paid_already_paid_ahead_shows_the_future_due_date(client, db, super_admin, tenant):
    # A later day's click, once the tenant is genuinely paid ahead into
    # the future and last_paid_on is no longer today - exercises the
    # next_billing_due > today guard specifically.
    tenant.next_billing_due = date.today() + timedelta(days=20)
    tenant.last_paid_on = date.today() - timedelta(days=10)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")

    resp = client.post(f"/admin/clients/{tenant.id}/billing/mark-paid", follow_redirects=True)
    assert b"already paid up" in resp.data.lower()
    db.session.refresh(tenant)
    assert tenant.next_billing_due == date.today() + timedelta(days=20)


def test_mark_billing_paid_blocks_a_same_day_repeat_even_when_the_advance_lands_on_today(client, db, super_admin, tenant):
    # The exact edge case a plain "next_billing_due > today" check
    # misses: a tenant overdue by precisely one cycle length advances,
    # on the first click, to a new due date that is itself exactly
    # today - not in the future - so a naive date-only guard would let
    # a second same-day click slip through and advance a second time.
    import calendar as _calendar

    today = date.today()
    if today.month == 1:
        last_day = _calendar.monthrange(today.year - 1, 12)[1]
        one_month_ago = date(today.year - 1, 12, min(today.day, last_day))
    else:
        last_day = _calendar.monthrange(today.year, today.month - 1)[1]
        one_month_ago = date(today.year, today.month - 1, min(today.day, last_day))
    assert add_one_month(one_month_ago) == today  # sanity-check the fixture date

    tenant.next_billing_due = one_month_ago
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")

    client.post(f"/admin/clients/{tenant.id}/billing/mark-paid")
    db.session.refresh(tenant)
    assert tenant.next_billing_due == today

    resp = client.post(f"/admin/clients/{tenant.id}/billing/mark-paid", follow_redirects=True)
    db.session.refresh(tenant)
    assert tenant.next_billing_due == today, "a same-day repeat click must never advance a second time"
    assert b"already marked paid today" in resp.data.lower()


# --- Resync to live calendar ------------------------------------------------

def test_resync_billing_cycle_recomputes_from_the_anchor(client, db, super_admin, tenant):
    # Simulate a "mashed up" cycle - many cycles ahead of where it should
    # legitimately be - and confirm resync snaps it back to the correct
    # live value, counting forward from the anchor rather than trusting
    # whatever next_billing_due currently (wrongly) holds.
    tenant.cycle_anchor_date = date(2026, 1, 8)
    tenant.next_billing_due = date(2030, 1, 8)  # badly drifted
    db.session.commit()

    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(f"/admin/clients/{tenant.id}/billing/resync", follow_redirects=True)
    assert resp.status_code == 200

    db.session.refresh(tenant)
    # The correct live value: the smallest monthly boundary from the
    # anchor that's still in the future relative to today.
    expected = tenant.cycle_anchor_date
    while expected <= date.today():
        expected = add_one_month(expected)
    assert tenant.next_billing_due == expected
    assert tenant.last_paid_on == date.today()
    assert b"resynced to the live calendar" in resp.data.lower()


def test_resync_billing_cycle_requires_a_billing_cycle(client, db, super_admin, tenant):
    tenant.billing_cycle = None
    tenant.cycle_anchor_date = date(2026, 1, 8)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(f"/admin/clients/{tenant.id}/billing/resync", follow_redirects=True)
    assert b"set a billing cycle" in resp.data.lower()


def test_resync_billing_cycle_requires_a_started_cycle(client, db, super_admin, tenant):
    tenant.cycle_anchor_date = None
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(f"/admin/clients/{tenant.id}/billing/resync", follow_redirects=True)
    assert b"nothing to resync" in resp.data.lower()


def test_client_admin_cannot_resync_their_own_billing_cycle(client, db, tenant, client_admin):
    tenant.cycle_anchor_date = date(2026, 1, 8)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(f"/admin/clients/{tenant.id}/billing/resync")
    assert resp.status_code == 403


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
    assert resp.request.path == "/admin/auditors"

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
