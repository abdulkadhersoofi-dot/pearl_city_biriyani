"""Counter billing (POS) is a module ("pos"). Every client has it unless it
is switched off - by the client (their own choice) or locked off by the
Ultra Admin. Off, the counter, its settings, the restaurant tables that
work through it and the ARFA Counter app are gone; invoices carry on."""

import uuid
from decimal import Decimal

import pytest

from app.models.business import DiningTable
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.utils.entitlements import set_module_exception

XHR = {"X-Requested-With": "XMLHttpRequest"}


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password})


@pytest.fixture()
def branch(db, tenant):
    user = User(tenant_id=tenant.id, name="Branch", email="branch@acme.example.com", role=UserRole.STAFF,
                must_change_password=False)
    user.set_password("BranchSecret123")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def tea(db, tenant):
    p = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="9963", gst_rate=Decimal("5"), unit="NOS",
                sale_price=Decimal("20"))
    db.session.add(p)
    db.session.commit()
    return p


def _checkout(client, tea):
    return client.post("/pos/checkout", data={
        "line_product_id[]": str(tea.id), "line_description[]": "Tea", "line_hsn[]": "9963", "line_qty[]": "1",
        "line_rate[]": "20", "line_gst_rate[]": "5", "line_unit[]": "NOS", "payment_mode": "cash",
        "checkout_uuid": str(uuid.uuid4())}, headers=XHR)


def test_every_client_has_the_counter_until_it_is_switched_off(client, db, tenant, client_admin, tea):
    assert tenant.features == {} and tenant.has_feature("pos")   # an existing client: nothing set, still on
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get("/pos/").status_code == 200 and _checkout(client, tea).status_code == 200
    modules = client.get("/settings/features").get_data(as_text=True)
    assert "Counter billing (POS)" in modules


def test_switched_off_the_counter_is_gone_and_invoices_carry_on(client, db, tenant, client_admin, branch, tea):
    tenant.set_feature("restaurant_mode", True)
    db.session.add(DiningTable(tenant_id=tenant.id, name="T1"))
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    form = {f"feature_{k}": "y" for k in ("restaurant_mode",)}   # every other switch off, POS included
    client.post("/settings/features", data=form)
    db.session.expire_all()
    assert not db.session.get(Tenant, tenant.id).has_feature("pos")
    for url in ("/pos/", "/pos/bills", "/settings/counter", "/restaurant/", "/restaurant/kitchen"):
        assert client.get(url).status_code == 404, url
    assert _checkout(client, tea).status_code == 404
    home = client.get("/invoices/").get_data(as_text=True)
    assert 'href="/pos/"' not in home and "/settings/counter" not in client.get("/settings/").get_data(as_text=True)
    assert "Pair a phone" not in client.get("/branches/").get_data(as_text=True)
    assert client.get("/invoices/new").status_code == 200
    client.get("/auth/logout")

    # A branch login lands on a page that says so; the phone app too.
    res = _login(client, branch.email, "BranchSecret123")
    assert res.headers["Location"].endswith("/counter-off")
    assert "Counter billing is switched off" in client.get("/counter-off").get_data(as_text=True)
    assert "Counter billing is switched off" in client.get("/app/").get_data(as_text=True)
    assert client.get("/app/bill").status_code == 404 and client.get("/pos/").status_code == 404


def test_the_ultra_admin_can_lock_the_counter_off(client, db, tenant, client_admin, super_admin):
    set_module_exception(tenant, "pos", "off", super_admin)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/settings/features", data={"feature_pos": "y"})
    db.session.expire_all()
    assert not db.session.get(Tenant, tenant.id).has_feature("pos")
    assert client.get("/pos/").status_code == 404


def test_taken_away_with_bills_the_counter_is_read_only(client, db, tenant, client_admin, super_admin, tea):
    _login(client, client_admin.email, "ClientSecret123")
    assert _checkout(client, tea).status_code == 200
    set_module_exception(tenant, "pos", "off", super_admin)   # the package takes it away
    db.session.commit()
    assert client.get("/pos/bills").status_code == 200        # old bills still open
    assert _checkout(client, tea).status_code == 403          # nothing new


def test_a_new_package_and_every_business_type_start_with_the_counter(db, super_admin):
    from app.models.tenant import BusinessType
    from app.utils.features import BUSINESS_TYPE_PRESETS

    assert all("pos" in preset for preset in BUSINESS_TYPE_PRESETS.values())
    from app.utils.entitlements import ensure_starting_packages, package_module_state
    from app.models.package import Package

    ensure_starting_packages()
    for package in Package.query.all():
        assert package_module_state(package, "pos") in ("inc", "choice"), package.key
