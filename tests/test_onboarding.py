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

    # Logs straight in with the password the Super Admin set - no OTP, no email.
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
        "/staff/new",
        data={
            "name": "New Cashier",
            "email": "newcashier@example.com",
            "phone": "",
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
        f"/staff/{staff.id}/reset-password",
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
        f"/staff/{other_staff.id}/reset-password",
        data={"new_password": "ShouldNotWork123", "confirm_password": "ShouldNotWork123"},
    )
    assert resp.status_code == 404
