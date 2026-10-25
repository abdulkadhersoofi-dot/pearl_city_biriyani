from app.models.tenant import Tenant
from app.models.user import User, UserRole


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def test_onboarding_without_gstin_still_creates_a_business_location(client, db, super_admin):
    """An unregistered client has no GSTIN number, but still needs a
    business-location row on file - without one, POS checkout and
    invoicing have nothing to bill from and fail outright."""
    _login(client, super_admin.email, "SuperSecret123")

    resp = client.post(
        "/admin/clients/new",
        data={
            "legal_name": "Street Biriyani Stall",
            "registration_type": "unregistered",
            "state_code": "27",
            # gstin deliberately omitted - this client has none.
            "admin_name": "Stall Admin",
            "admin_email": "stall@example.com",
            "admin_phone": "",
            "admin_password": "InitialPass123",
            "admin_confirm_password": "InitialPass123",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    tenant = Tenant.query.filter_by(legal_name="Street Biriyani Stall").first()
    assert tenant is not None
    gstin = tenant.gstins.first()
    assert gstin is not None, "Onboarding without a GSTIN must still create a location row"
    assert gstin.gstin is None
    assert gstin.is_primary is True
    assert gstin.state_code == "27"


def test_super_admin_onboards_client_with_password_no_otp(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")

    resp = client.post(
        "/admin/clients/new",
        data={
            "legal_name": "New Traders",
            "registration_type": "regular",
            "state_code": "27",
            "admin_name": "New Admin",
            "admin_email": "newadmin@example.com",
            "admin_phone": "",
            "admin_password": "InitialPass123",
            "admin_confirm_password": "InitialPass123",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    admin = User.query.filter_by(email="newadmin@example.com").first()
    assert admin is not None
    assert admin.role == UserRole.CLIENT_ADMIN
    assert admin.must_change_password is True
    assert admin.check_password("InitialPass123")

    new_tenant = Tenant.query.filter_by(legal_name="New Traders").first()
    assert new_tenant.billing_cycle is None, "A freshly onboarded client starts pending verification"

    # Can't sign in yet - the Ultra Admin hasn't verified/picked a billing
    # cycle for this client.
    client.get("/auth/logout")
    resp = _login(client, "newadmin@example.com", "InitialPass123")
    assert b"awaiting verification" in resp.data.lower()

    # Ultra Admin verifies and picks a billing cycle.
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(
        f"/admin/clients/{new_tenant.id}/billing/set-cycle",
        data={"billing_cycle": "monthly"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(new_tenant)
    assert new_tenant.billing_cycle is not None

    # Now logs straight in with the password the Ultra Admin set - no OTP, no email.
    client.get("/auth/logout")
    resp = _login(client, "newadmin@example.com", "InitialPass123")
    assert resp.status_code == 200
    assert b"change" in resp.data.lower() or resp.request.path == "/auth/change-password"


def test_super_admin_can_backfill_a_missing_business_location(client, db, super_admin, tenant):
    """Covers a tenant onboarded before this fix, or any other tenant that
    somehow ended up with zero Gstin rows - the firm must be able to add
    one without re-onboarding the client from scratch."""
    from app.models.tenant import Gstin

    Gstin.query.filter_by(tenant_id=tenant.id).delete()
    db.session.commit()
    assert tenant.gstins.count() == 0

    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(
        f"/admin/clients/{tenant.id}/gstins/new",
        data={"gstin": "", "state_code": "27", "registered_address": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    db.session.refresh(tenant)
    gstin = tenant.gstins.first()
    assert gstin is not None
    assert gstin.gstin is None
    assert gstin.is_primary is True


def test_super_admin_resets_client_admin_password_directly(client, db, super_admin, tenant, client_admin):
    _login(client, super_admin.email, "SuperSecret123")

    resp = client.post(
        f"/admin/clients/{tenant.id}/users/{client_admin.id}/reset-password",
        data={"new_password": "BrandNewPass123", "confirm_password": "BrandNewPass123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    db.session.refresh(client_admin)
    assert client_admin.check_password("BrandNewPass123")
    assert client_admin.must_change_password is True


def test_client_admin_creates_staff_with_password_no_otp(client, db, client_admin, tenant):
    _login(client, client_admin.email, "ClientSecret123")

    resp = client.post(
        "/branches/new",
        data={
            "name": "New Cashier",
            "email": "newcashier@example.com",
            "phone": "",
            "branch_type": "third_party",
            "password": "CashierInit123",
            "confirm_password": "CashierInit123",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    staff = User.query.filter_by(email="newcashier@example.com").first()
    assert staff is not None
    assert staff.role == UserRole.STAFF
    assert staff.tenant_id == tenant.id
    assert staff.must_change_password is True
    assert staff.check_password("CashierInit123")

    client.get("/auth/logout")
    resp = _login(client, "newcashier@example.com", "CashierInit123")
    assert resp.status_code == 200


def test_client_admin_resets_staff_password_directly(client, db, client_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")

    resp = client.post(
        f"/branches/{staff.id}/reset-password",
        data={"new_password": "StaffResetPass123", "confirm_password": "StaffResetPass123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    db.session.refresh(staff)
    assert staff.check_password("StaffResetPass123")
    assert staff.must_change_password is True


def test_client_admin_cannot_reset_another_tenants_staff(client, db, client_admin, tenant):
    from app.models.tenant import Gstin, RegistrationType, Tenant

    other_tenant = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR)
    db.session.add(other_tenant)
    db.session.flush()
    db.session.add(
        Gstin(tenant_id=other_tenant.id, gstin="29CCCCC0000C1Z5", state_code="29", state_name="Karnataka", is_primary=True)
    )
    other_staff = User(
        tenant_id=other_tenant.id, name="Other Cashier", email="othercashier@example.com",
        role=UserRole.STAFF, must_change_password=False,
    )
    other_staff.set_password("OtherSecret123")
    db.session.add(other_staff)
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        f"/branches/{other_staff.id}/reset-password",
        data={"new_password": "ShouldNotWork123", "confirm_password": "ShouldNotWork123"},
    )
    assert resp.status_code == 404


def test_the_reset_password_button_is_on_every_login_of_the_client(client, db, super_admin, tenant, client_admin,
                                                                    staff):
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get(f"/admin/clients/{tenant.id}?tab=users").get_data(as_text=True)
    for user in (client_admin, staff):
        assert f"/admin/clients/{tenant.id}/users/{user.id}/reset-password" in html
    page = client.get(f"/admin/clients/{tenant.id}/users/{staff.id}/reset-password")
    assert page.status_code == 200 and staff.email.encode() in page.data


def test_resetting_a_forgotten_password_unlocks_the_login(app, client, db, super_admin, tenant, client_admin):
    from app.auth import security
    from app.models.audit_log import AuditLog

    # The owner forgot it and guessed until the account locked.
    for _ in range(app.config["LOGIN_LOCKOUT_ATTEMPTS"]):
        client.post("/auth/login", data={"email": client_admin.email, "password": "wrong-guess-1"})
    db.session.refresh(client_admin)
    assert security.locked_until(client_admin) is not None

    _login(client, super_admin.email, "SuperSecret123")
    assert b"locked until" in client.get(f"/admin/clients/{tenant.id}?tab=users").data
    resp = client.post(f"/admin/clients/{tenant.id}/users/{client_admin.id}/reset-password",
                       data={"new_password": "BrandNewPass123", "confirm_password": "BrandNewPass123"},
                       follow_redirects=True)
    assert b"unlocked" in resp.data
    db.session.refresh(client_admin)
    assert security.locked_until(client_admin) is None and client_admin.must_change_password is True
    row = AuditLog.query.filter_by(action="client_password_reset_by_firm", entity_id=client_admin.id).one()
    assert row.details == {"unlocked": True}

    client.get("/auth/logout")
    res = client.post("/auth/login", data={"email": client_admin.email, "password": "BrandNewPass123"})
    assert res.status_code == 302 and res.headers["Location"].endswith("/auth/change-password")


def test_a_branch_login_s_password_is_reset_the_same_way(client, db, super_admin, tenant, staff):
    _login(client, super_admin.email, "SuperSecret123")
    client.post(f"/admin/clients/{tenant.id}/users/{staff.id}/reset-password",
                data={"new_password": "BranchNewPass123", "confirm_password": "BranchNewPass123"})
    db.session.refresh(staff)
    assert staff.check_password("BranchNewPass123")


def test_a_login_of_another_client_cant_be_reset_from_this_client(client, db, super_admin, tenant, client_admin):
    from app.models.tenant import BillingCycle, RegistrationType, Tenant

    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    res = client.post(f"/admin/clients/{other.id}/users/{client_admin.id}/reset-password",
                      data={"new_password": "BrandNewPass123", "confirm_password": "BrandNewPass123"})
    assert res.status_code == 404
    db.session.refresh(client_admin)
    assert client_admin.check_password("ClientSecret123")
