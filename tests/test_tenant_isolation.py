from app.models.customer import Customer
from app.models.tenant import Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _make_second_tenant_with_admin(db):
    tenant_b = Tenant(legal_name="Beta Corp", registration_type=RegistrationType.REGULAR)
    db.session.add(tenant_b)
    db.session.flush()
    db.session.add(
        Gstin(tenant_id=tenant_b.id, gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka", is_primary=True)
    )
    admin_b = User(
        tenant_id=tenant_b.id,
        name="Beta Admin",
        email="admin@beta.example.com",
        role=UserRole.CLIENT_ADMIN,
        must_change_password=False,
    )
    admin_b.set_password("BetaSecret123")
    db.session.add(admin_b)
    db.session.commit()
    return tenant_b, admin_b


def test_client_admin_only_sees_own_customers(client, db, tenant, client_admin):
    tenant_b, admin_b = _make_second_tenant_with_admin(db)

    cust_a = Customer(tenant_id=tenant.id, name="Alpha Customer", state_code="27", state_name="Maharashtra")
    cust_b = Customer(tenant_id=tenant_b.id, name="Beta Customer", state_code="29", state_name="Karnataka")
    db.session.add_all([cust_a, cust_b])
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/customers/")

    assert b"Alpha Customer" in resp.data
    assert b"Beta Customer" not in resp.data


def test_client_admin_cannot_open_another_tenants_customer_by_id(client, db, tenant, client_admin):
    tenant_b, admin_b = _make_second_tenant_with_admin(db)
    cust_b = Customer(tenant_id=tenant_b.id, name="Beta Customer", state_code="29", state_name="Karnataka")
    db.session.add(cust_b)
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get(f"/customers/{cust_b.id}/edit")

    assert resp.status_code == 404  # tenant_query() filters it out before it's ever found


def test_staff_role_is_blocked_from_customer_management(client, db, tenant):
    staff = User(
        tenant_id=tenant.id, name="Cashier", email="cashier@acme.example.com",
        role=UserRole.STAFF, must_change_password=False,
    )
    staff.set_password("CashierSecret123")
    db.session.add(staff)
    db.session.commit()

    _login(client, staff.email, "CashierSecret123")
    resp = client.get("/customers/")

    assert resp.status_code == 403


def test_super_admin_is_blocked_from_tenant_scoped_screens(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.get("/invoices/")

    assert resp.status_code == 403
