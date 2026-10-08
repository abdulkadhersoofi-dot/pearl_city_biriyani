from datetime import date, timedelta

from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.utils.auditor_scope import assert_auditor_owns_tenant, auditor_tenant_ids
from app.utils.billing import add_one_year, advance_billing_cycle, tenant_access_status


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _make_auditor(
    db, email, role=UserRole.AUDITOR, parent_auditor_id=None, name="Auditor One", password="AuditorPass123",
    billing_cycle=BillingCycle.MONTHLY,
):
    # Already-verified by default, like every pre-existing auditor after
    # the migration backfill - tests that care about the pending gate
    # itself pass billing_cycle=None explicitly.
    user = User(
        name=name, email=email, role=role, parent_auditor_id=parent_auditor_id, must_change_password=False,
        billing_cycle=billing_cycle,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _make_tenant(db, legal_name, auditor_id=None, billing_cycle=None):
    t = Tenant(legal_name=legal_name, registration_type=RegistrationType.REGULAR, auditor_id=auditor_id, billing_cycle=billing_cycle)
    db.session.add(t)
    db.session.flush()
    db.session.add(Gstin(tenant_id=t.id, gstin=None, state_code="27", state_name="Maharashtra", is_primary=True))
    db.session.commit()
    return t


# --- add_one_year / advance_billing_cycle --------------------------------

def test_add_one_year_normal_case():
    assert add_one_year(date(2026, 3, 15)) == date(2027, 3, 15)


def test_add_one_year_clamps_leap_day():
    assert add_one_year(date(2028, 2, 29)) == date(2029, 2, 28)


def test_advance_billing_cycle_monthly(tenant, db):
    tenant.billing_cycle = BillingCycle.MONTHLY
    db.session.commit()
    assert advance_billing_cycle(tenant, date(2026, 1, 15)) == date(2026, 2, 15)


def test_advance_billing_cycle_yearly(tenant, db):
    tenant.billing_cycle = BillingCycle.YEARLY
    db.session.commit()
    assert advance_billing_cycle(tenant, date(2026, 1, 15)) == date(2027, 1, 15)


# --- pending verification gate -------------------------------------------

def test_pending_tenant_is_blocked(db, tenant):
    tenant.billing_cycle = None
    db.session.commit()
    status = tenant_access_status(tenant)
    assert status.blocked is True
    assert status.reason == "pending_verification"


def test_verified_tenant_with_no_due_date_is_not_blocked(db, tenant):
    tenant.billing_cycle = BillingCycle.MONTHLY
    db.session.commit()
    status = tenant_access_status(tenant)
    assert status.blocked is False


def test_manual_alarm_forces_a_notice_with_no_other_trigger(db, tenant):
    tenant.billing_cycle = BillingCycle.MONTHLY
    tenant.manual_alarm_active = True
    db.session.commit()
    status = tenant_access_status(tenant)
    assert status.blocked is False
    assert status.billing_notice is True


# --- auditor/sub-auditor creation ----------------------------------------

def test_ultra_admin_creates_an_auditor(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(
        "/admin/auditors/new",
        data={
            "role": "auditor",
            "name": "Firm Auditor",
            "email": "auditor1@example.com",
            "phone": "",
            "password": "AuditorPass123",
            "confirm_password": "AuditorPass123",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    created = User.query.filter_by(email="auditor1@example.com").first()
    assert created is not None
    assert created.role == UserRole.AUDITOR
    assert created.parent_auditor_id is None
    assert created.must_change_password is True


def test_ultra_admin_creates_a_sub_auditor_with_a_parent(client, db, super_admin):
    auditor = _make_auditor(db, "auditor2@example.com")
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(
        "/admin/auditors/new",
        data={
            "role": "sub_auditor",
            "parent_auditor_id": str(auditor.id),
            "name": "Office Junior",
            "email": "subauditor1@example.com",
            "phone": "",
            "password": "SubAuditorPass123",
            "confirm_password": "SubAuditorPass123",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    created = User.query.filter_by(email="subauditor1@example.com").first()
    assert created is not None
    assert created.role == UserRole.SUB_AUDITOR
    assert created.parent_auditor_id == auditor.id


def test_auditor_creates_sub_auditor_parent_is_forced_to_self(client, db, super_admin):
    auditor = _make_auditor(db, "auditor3@example.com")
    other_auditor = _make_auditor(db, "auditor4@example.com")
    _login(client, auditor.email, "AuditorPass123")
    resp = client.post(
        "/admin/auditors/new",
        data={
            # Even if the form tried to claim a different parent, the
            # route must force it to the signed-in Auditor.
            "role": "auditor",
            "parent_auditor_id": str(other_auditor.id),
            "name": "My Sub",
            "email": "mysub@example.com",
            "phone": "",
            "password": "MySubPass123",
            "confirm_password": "MySubPass123",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    created = User.query.filter_by(email="mysub@example.com").first()
    assert created is not None
    assert created.role == UserRole.SUB_AUDITOR
    assert created.parent_auditor_id == auditor.id


def test_sub_auditor_cannot_create_anyone(client, db):
    sub = _make_auditor(db, "subonly@example.com", role=UserRole.SUB_AUDITOR)
    _login(client, sub.email, "AuditorPass123")
    resp = client.get("/admin/auditors/new")
    assert resp.status_code == 403


# --- scoping: auditor_tenant_ids / assert_auditor_owns_tenant ------------

def test_ultra_admin_scope_is_unrestricted(super_admin):
    assert auditor_tenant_ids(super_admin) is None


def test_auditor_scope_includes_own_and_sub_auditors_tenants(db):
    auditor = _make_auditor(db, "scopeauditor@example.com")
    sub = _make_auditor(db, "scopesub@example.com", role=UserRole.SUB_AUDITOR, parent_auditor_id=auditor.id)
    own_tenant = _make_tenant(db, "Scope Own Co", auditor_id=auditor.id)
    sub_tenant = _make_tenant(db, "Scope Sub Co", auditor_id=sub.id)
    unrelated = _make_tenant(db, "Scope Unrelated Co")

    ids = auditor_tenant_ids(auditor)
    assert ids == {own_tenant.id, sub_tenant.id}
    assert unrelated.id not in ids


def test_sub_auditor_scope_is_only_their_own_allocation(db):
    auditor = _make_auditor(db, "scopeauditor2@example.com")
    sub = _make_auditor(db, "scopesub2@example.com", role=UserRole.SUB_AUDITOR, parent_auditor_id=auditor.id)
    sub_tenant = _make_tenant(db, "Scope Sub2 Co", auditor_id=sub.id)
    auditor_only_tenant = _make_tenant(db, "Scope Auditor Only Co", auditor_id=auditor.id)

    ids = auditor_tenant_ids(sub)
    assert ids == {sub_tenant.id}
    assert auditor_only_tenant.id not in ids


def test_assert_auditor_owns_tenant_403s_outside_scope(db):
    auditor = _make_auditor(db, "ownscope@example.com")
    other = _make_tenant(db, "Owns Scope Other Co")
    try:
        assert_auditor_owns_tenant(auditor, other)
        assert False, "expected a 403 abort"
    except Exception as exc:
        assert getattr(exc, "code", None) == 403


# --- onboarding: who creates it, where it lands --------------------------

def test_auditor_onboarded_client_is_allocated_to_them_and_pending(client, db):
    auditor = _make_auditor(db, "onboardauditor@example.com")
    _login(client, auditor.email, "AuditorPass123")
    resp = client.post(
        "/admin/clients/new",
        data={
            "legal_name": "Auditor Onboarded Co",
            "registration_type": "regular",
            "state_code": "27",
            "gstin": "",
            "admin_name": "Admin",
            "admin_email": "auditoronboarded@example.com",
            "admin_phone": "",
            "admin_password": "InitialPass123",
            "admin_confirm_password": "InitialPass123",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    created = Tenant.query.filter_by(legal_name="Auditor Onboarded Co").first()
    assert created is not None
    assert created.auditor_id == auditor.id
    assert created.billing_cycle is None  # billing selection stays with the Ultra Admin


def test_ultra_admin_onboarded_client_is_unallocated(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    client.post(
        "/admin/clients/new",
        data={
            "legal_name": "Ultra Admin Onboarded Co",
            "registration_type": "regular",
            "state_code": "27",
            "gstin": "",
            "admin_name": "Admin",
            "admin_email": "ultraonboarded@example.com",
            "admin_phone": "",
            "admin_password": "InitialPass123",
            "admin_confirm_password": "InitialPass123",
        },
    )
    created = Tenant.query.filter_by(legal_name="Ultra Admin Onboarded Co").first()
    assert created is not None
    assert created.auditor_id is None
    assert created.billing_cycle is None


# --- directory/detail scoping over HTTP ----------------------------------

def test_auditor_cannot_view_another_auditors_client(client, db):
    auditor_a = _make_auditor(db, "viewa@example.com")
    auditor_b = _make_auditor(db, "viewb@example.com")
    tenant_b = _make_tenant(db, "View B Co", auditor_id=auditor_b.id, billing_cycle=BillingCycle.MONTHLY)

    _login(client, auditor_a.email, "AuditorPass123")
    resp = client.get(f"/admin/clients/{tenant_b.id}")
    assert resp.status_code == 403


def test_auditor_directory_only_lists_their_own_clients(client, db):
    auditor_a = _make_auditor(db, "lista@example.com")
    auditor_b = _make_auditor(db, "listb@example.com")
    _make_tenant(db, "List A Co", auditor_id=auditor_a.id, billing_cycle=BillingCycle.MONTHLY)
    _make_tenant(db, "List B Co", auditor_id=auditor_b.id, billing_cycle=BillingCycle.MONTHLY)

    _login(client, auditor_a.email, "AuditorPass123")
    resp = client.get("/admin/clients")
    assert b"List A Co" in resp.data
    assert b"List B Co" not in resp.data


# --- reassignment / sub-auditor allocation -------------------------------

def test_ultra_admin_reassigns_a_client_to_an_auditor(client, db, super_admin, tenant):
    auditor = _make_auditor(db, "reassign@example.com")
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(
        f"/admin/clients/{tenant.id}/reassign-auditor",
        data={"auditor_id": str(auditor.id)},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(tenant)
    assert tenant.auditor_id == auditor.id


def test_ultra_admin_can_unallocate_a_client(client, db, super_admin, tenant):
    auditor = _make_auditor(db, "unallocate@example.com")
    tenant.auditor_id = auditor.id
    db.session.commit()

    _login(client, super_admin.email, "SuperSecret123")
    client.post(f"/admin/clients/{tenant.id}/reassign-auditor", data={"auditor_id": ""})
    db.session.refresh(tenant)
    assert tenant.auditor_id is None


def test_auditor_allocates_own_client_to_own_sub_auditor(client, db):
    auditor = _make_auditor(db, "allocateauditor@example.com")
    sub = _make_auditor(db, "allocatesub@example.com", role=UserRole.SUB_AUDITOR, parent_auditor_id=auditor.id)
    own_tenant = _make_tenant(db, "Allocate Own Co", auditor_id=auditor.id, billing_cycle=BillingCycle.MONTHLY)

    _login(client, auditor.email, "AuditorPass123")
    resp = client.post(
        f"/admin/clients/{own_tenant.id}/allocate-sub-auditor",
        data={"sub_auditor_id": str(sub.id)},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(own_tenant)
    assert own_tenant.auditor_id == sub.id


def test_auditor_cannot_allocate_to_someone_elses_sub_auditor(client, db):
    auditor = _make_auditor(db, "allocateauditor2@example.com")
    other_auditor = _make_auditor(db, "otherauditor2@example.com")
    foreign_sub = _make_auditor(db, "foreignsub@example.com", role=UserRole.SUB_AUDITOR, parent_auditor_id=other_auditor.id)
    own_tenant = _make_tenant(db, "Allocate Own Co 2", auditor_id=auditor.id, billing_cycle=BillingCycle.MONTHLY)

    _login(client, auditor.email, "AuditorPass123")
    resp = client.post(
        f"/admin/clients/{own_tenant.id}/allocate-sub-auditor",
        data={"sub_auditor_id": str(foreign_sub.id)},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(own_tenant)
    assert own_tenant.auditor_id == auditor.id  # unchanged


def test_auditor_cannot_allocate_a_client_that_isnt_theirs(client, db):
    auditor = _make_auditor(db, "notmineauditor@example.com")
    other_auditor = _make_auditor(db, "notmineother@example.com")
    sub = _make_auditor(db, "notminesub@example.com", role=UserRole.SUB_AUDITOR, parent_auditor_id=auditor.id)
    not_mine = _make_tenant(db, "Not Mine Co", auditor_id=other_auditor.id, billing_cycle=BillingCycle.MONTHLY)

    _login(client, auditor.email, "AuditorPass123")
    resp = client.post(f"/admin/clients/{not_mine.id}/allocate-sub-auditor", data={"sub_auditor_id": str(sub.id)})
    assert resp.status_code == 404


# --- cycle anchor: only on first post-password-change login -------------

def test_cycle_anchor_set_on_first_client_admin_login_after_password_change(client, db, tenant, client_admin):
    tenant.billing_cycle = BillingCycle.MONTHLY
    tenant.cycle_anchor_date = None
    tenant.next_billing_due = None
    client_admin.must_change_password = True
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        "/auth/change-password",
        data={
            "current_password": "ClientSecret123",
            "new_password": "BrandNewClientPass123",
            "confirm_password": "BrandNewClientPass123",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(tenant)
    assert tenant.cycle_anchor_date == date.today()
    from app.utils.billing import add_one_month
    assert tenant.next_billing_due == add_one_month(date.today())


def test_cycle_anchor_not_re_stamped_on_a_later_password_change(client, db, tenant, client_admin):
    tenant.billing_cycle = BillingCycle.MONTHLY
    tenant.cycle_anchor_date = date(2020, 1, 1)
    tenant.next_billing_due = date(2020, 2, 1)
    client_admin.must_change_password = False
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    client.post(
        "/auth/change-password",
        data={
            "current_password": "ClientSecret123",
            "new_password": "AnotherNewPass123",
            "confirm_password": "AnotherNewPass123",
        },
    )
    db.session.refresh(tenant)
    assert tenant.cycle_anchor_date == date(2020, 1, 1)
    assert tenant.next_billing_due == date(2020, 2, 1)


def test_cycle_anchor_not_set_for_staff_password_change(client, db, tenant, staff):
    tenant.billing_cycle = BillingCycle.MONTHLY
    tenant.cycle_anchor_date = None
    db.session.commit()

    _login(client, staff.email, "CashierSecret123")
    client.post(
        "/auth/change-password",
        data={
            "current_password": "CashierSecret123",
            "new_password": "NewStaffPass1234",
            "confirm_password": "NewStaffPass1234",
        },
    )
    db.session.refresh(tenant)
    assert tenant.cycle_anchor_date is None


# --- manual alarm trigger/mute --------------------------------------------

def test_ultra_admin_triggers_and_mutes_the_alarm(client, db, super_admin, tenant):
    tenant.billing_cycle = BillingCycle.MONTHLY
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")

    client.post(f"/admin/clients/{tenant.id}/billing/trigger-alarm")
    db.session.refresh(tenant)
    assert tenant.manual_alarm_active is True
    assert tenant_access_status(tenant).billing_notice is True

    client.post(f"/admin/clients/{tenant.id}/billing/mute-alarm")
    db.session.refresh(tenant)
    assert tenant.manual_alarm_active is False


def test_auditor_cannot_trigger_the_alarm(client, db):
    auditor = _make_auditor(db, "alarmauditor@example.com")
    tenant = _make_tenant(db, "Alarm Co", auditor_id=auditor.id, billing_cycle=BillingCycle.MONTHLY)
    _login(client, auditor.email, "AuditorPass123")
    resp = client.post(f"/admin/clients/{tenant.id}/billing/trigger-alarm")
    assert resp.status_code == 403


# --- act-as through the new hierarchy -------------------------------------

def test_auditor_can_act_as_their_own_clients_admin(client, db):
    auditor = _make_auditor(db, "actasauditor@example.com")
    own_tenant = _make_tenant(db, "Act As Own Co", auditor_id=auditor.id, billing_cycle=BillingCycle.MONTHLY)
    admin_user = User(tenant_id=own_tenant.id, name="Client Admin", email="actasclient@example.com", role=UserRole.CLIENT_ADMIN, must_change_password=False)
    admin_user.set_password("ClientSecret123")
    db.session.add(admin_user)
    db.session.commit()

    _login(client, auditor.email, "AuditorPass123")
    resp = client.post(f"/admin/clients/{own_tenant.id}/users/{admin_user.id}/act-as", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.request.path == "/invoices/"

    # Can reach branch creation - this is how an Auditor creates branches
    # for the companies they manage, with no separate UI of its own.
    resp = client.get("/branches/new")
    assert resp.status_code == 200

    resp = client.get("/auth/stop-impersonating", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.request.path == "/admin/clients"


def test_auditor_cannot_act_as_a_client_outside_their_scope(client, db):
    auditor = _make_auditor(db, "outsideauditor@example.com")
    other_auditor = _make_auditor(db, "outsideother@example.com")
    foreign_tenant = _make_tenant(db, "Outside Co", auditor_id=other_auditor.id, billing_cycle=BillingCycle.MONTHLY)
    admin_user = User(tenant_id=foreign_tenant.id, name="Foreign Admin", email="foreignadmin@example.com", role=UserRole.CLIENT_ADMIN, must_change_password=False)
    admin_user.set_password("ClientSecret123")
    db.session.add(admin_user)
    db.session.commit()

    _login(client, auditor.email, "AuditorPass123")
    resp = client.post(f"/admin/clients/{foreign_tenant.id}/users/{admin_user.id}/act-as")
    assert resp.status_code == 403


def test_ultra_admin_can_act_as_an_auditor(client, db, super_admin):
    auditor = _make_auditor(db, "actasbyultra@example.com")
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(f"/admin/auditors/{auditor.id}/act-as", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.request.path == "/admin/clients"

    resp = client.get("/auth/stop-impersonating", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.request.path == "/admin/auditors"


def test_auditor_can_act_as_their_own_sub_auditor(client, db):
    auditor = _make_auditor(db, "subparent@example.com")
    sub = _make_auditor(db, "subchild@example.com", role=UserRole.SUB_AUDITOR, parent_auditor_id=auditor.id)
    _login(client, auditor.email, "AuditorPass123")
    resp = client.post(f"/admin/auditors/{sub.id}/act-as", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.request.path == "/admin/clients"


def test_auditor_cannot_act_as_an_unrelated_auditor(client, db):
    auditor_a = _make_auditor(db, "unrelateda@example.com")
    auditor_b = _make_auditor(db, "unrelatedb@example.com")
    _login(client, auditor_a.email, "AuditorPass123")
    resp = client.post(f"/admin/auditors/{auditor_b.id}/act-as")
    assert resp.status_code == 403


# --- nav labels -------------------------------------------------------------

def test_ultra_admin_sees_auditors_tab_not_clients(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.get("/admin/auditors")
    assert b">Auditors<" in resp.data


def test_auditor_sees_clients_tab(client, db):
    auditor = _make_auditor(db, "navauditor@example.com")
    _login(client, auditor.email, "AuditorPass123")
    resp = client.get("/admin/clients")
    assert b">Clients<" in resp.data


# --- auditors have the exact same billing-cycle system as clients --------

def test_pending_auditor_blocks_login(client, db):
    auditor = _make_auditor(db, "pendingauditor@example.com", billing_cycle=None)
    resp = _login(client, auditor.email, "AuditorPass123")
    assert b"awaiting a billing cycle" in resp.data.lower()


def test_ultra_admin_sets_a_pending_auditors_billing_cycle(client, db, super_admin):
    auditor = _make_auditor(db, "setcycleauditor@example.com", billing_cycle=None)
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(
        f"/admin/auditors/{auditor.id}/billing/set-cycle",
        data={"billing_cycle": "monthly"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(auditor)
    assert auditor.billing_cycle.value == "monthly"

    # Now unblocked.
    client.get("/auth/logout")
    resp = _login(client, auditor.email, "AuditorPass123")
    assert resp.status_code == 200
    assert b"awaiting a billing cycle" not in resp.data.lower()


def test_ultra_admin_changes_an_auditors_cycle_from_monthly_to_yearly(client, db, super_admin):
    auditor = _make_auditor(db, "changecycleauditor@example.com", billing_cycle=BillingCycle.MONTHLY)
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(
        f"/admin/auditors/{auditor.id}/billing/set-cycle",
        data={"billing_cycle": "yearly"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(auditor)
    assert auditor.billing_cycle.value == "yearly"


def test_auditor_cannot_change_their_own_billing_cycle(client, db):
    auditor = _make_auditor(db, "selfchangeauditor@example.com")
    _login(client, auditor.email, "AuditorPass123")
    resp = client.post(f"/admin/auditors/{auditor.id}/billing/set-cycle", data={"billing_cycle": "yearly"})
    assert resp.status_code == 403


def test_auditor_cycle_anchor_set_on_first_login_after_password_change(client, db):
    auditor = _make_auditor(db, "anchorauditor@example.com", billing_cycle=BillingCycle.MONTHLY)
    auditor.must_change_password = True
    auditor.cycle_anchor_date = None
    db.session.commit()

    _login(client, auditor.email, "AuditorPass123")
    resp = client.post(
        "/auth/change-password",
        data={
            "current_password": "AuditorPass123",
            "new_password": "AuditorNewPass123",
            "confirm_password": "AuditorNewPass123",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(auditor)
    assert auditor.cycle_anchor_date == date.today()
    assert auditor.next_billing_due is not None


def test_mark_auditor_billing_paid_advances_the_cycle(client, db, super_admin):
    auditor = _make_auditor(db, "markpaidauditor@example.com", billing_cycle=BillingCycle.MONTHLY)
    auditor.next_billing_due = date(2026, 1, 15)
    db.session.commit()

    _login(client, super_admin.email, "SuperSecret123")
    client.post(f"/admin/auditors/{auditor.id}/billing/mark-paid")
    db.session.refresh(auditor)
    assert auditor.next_billing_due == date(2026, 2, 15)


def test_ultra_admin_triggers_and_mutes_an_auditors_alarm(client, db, super_admin):
    auditor = _make_auditor(db, "alarmauditor2@example.com", billing_cycle=BillingCycle.MONTHLY)
    _login(client, super_admin.email, "SuperSecret123")

    client.post(f"/admin/auditors/{auditor.id}/billing/trigger-alarm")
    db.session.refresh(auditor)
    assert auditor.manual_alarm_active is True

    client.post(f"/admin/auditors/{auditor.id}/billing/mute-alarm")
    db.session.refresh(auditor)
    assert auditor.manual_alarm_active is False


def test_pending_auditors_listed_on_the_auditors_directory(client, db, super_admin):
    _make_auditor(db, "pendinglist@example.com", billing_cycle=None, name="Pending Listed Auditor")
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.get("/admin/auditors")
    assert b"Pending Listed Auditor" in resp.data
    assert b"awaiting verification" in resp.data.lower()


def test_tenant_no_longer_has_a_hard_expiry_field(tenant):
    assert not hasattr(tenant, "valid_until")
