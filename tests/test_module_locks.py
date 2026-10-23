"""The Ultra Admin decides each module for a client: Off or On - locked,
the client and their branches can't change it, and a module that's off
can't be opened at all - or Client decides, with the client's own switch.
(Before, the console's switch only flipped the client's own switch, which
the client could flip straight back.)"""

import pytest

from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.utils.entitlements import module_state


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


def _set(client, tenant, **modules):
    data = {}
    for key, value in modules.items():
        state, _, on = value.partition(":")
        data[f"mod_{key}"] = state
        if on == "on":
            data[f"on_{key}"] = "1"
    return client.post(f"/admin/clients/{tenant.id}/modules", data=data)


def test_a_module_locked_off_is_gone_for_the_client_and_branches(client, db, tenant, client_admin, super_admin, branch):
    tenant.set_feature("restaurant_mode", True)   # the client had switched it on themselves
    db.session.commit()
    _login(client, "firm@example.com", "SuperSecret123")
    page = client.get(f"/admin/clients/{tenant.id}?tab=package").get_data(as_text=True)
    assert 'id="sec-module-locks"' in page and 'name="mod_restaurant_mode" value="off"' in page
    assert _set(client, tenant, restaurant_mode="off").status_code == 302
    client.get("/auth/logout")
    db.session.expire_all()
    assert module_state(tenant, "restaurant_mode") == "off" and not tenant.has_feature("restaurant_mode")

    _login(client, client_admin.email, "ClientSecret123")
    modules = client.get("/settings/features").get_data(as_text=True)
    assert "Ask ARFA to add" in modules
    # Ticking it on Settings > Modules changes nothing.
    client.post("/settings/features", data={"feature_restaurant_mode": "y"})
    db.session.expire_all()
    assert not db.session.get(Tenant, tenant.id).has_feature("restaurant_mode")
    assert client.get("/restaurant/").status_code == 404
    client.get("/auth/logout")
    _login(client, branch.email, "BranchSecret123")
    assert client.get("/restaurant/").status_code == 404


def test_a_module_locked_on_stays_on(client, db, tenant, client_admin, super_admin):
    _login(client, "firm@example.com", "SuperSecret123")
    _set(client, tenant, inventory="inc")
    client.get("/auth/logout")
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/settings/features", data={})    # every switch left off
    db.session.expire_all()
    assert db.session.get(Tenant, tenant.id).has_feature("inventory")
    assert "Included" in client.get("/settings/features").get_data(as_text=True)


def test_client_decides_sets_their_switch_and_they_can_change_it(client, db, tenant, client_admin, super_admin):
    _login(client, "firm@example.com", "SuperSecret123")
    _set(client, tenant, loyalty="off")
    _set(client, tenant, loyalty="choice:on")
    db.session.expire_all()
    assert module_state(tenant, "loyalty") == "choice" and tenant.has_feature("loyalty")
    assert (tenant.package_grant.module_overrides or {}).get("loyalty") is None   # back to the package's own state
    client.get("/auth/logout")
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/settings/features", data={})
    db.session.expire_all()
    assert not db.session.get(Tenant, tenant.id).has_feature("loyalty")


def test_only_the_ultra_admin_locks_modules(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    assert _set(client, tenant, inventory="off").status_code in (302, 403)
    db.session.expire_all()
    assert module_state(tenant, "inventory") == "choice"
