"""Packages and powers, stage 1: the rules underneath. A client's package,
the Ultra Admin's exceptions and the client's own switches decide each
module together; a client without a package (or on "Current setup")
behaves exactly as before; one client's package never touches another's;
auditor powers default to what each role could already do."""

import importlib.util
from pathlib import Path

import pytest

from app.models.audit_log import AuditLog
from app.models.package import Package, TenantPackage
from app.models.tenant import BillingCycle, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.utils.entitlements import (
    AUDITOR_POWERS, CHOICE, CURRENT_SETUP, INCLUDED, LIMITS, OFF, ON, READ_ONLY, SETTINGS, STARTING_PACKAGES,
    assign_package, auditor_can, client_may_switch, current_setup_package, ensure_starting_packages, limit_of,
    module_mode, module_state, own_branding, set_auditor_powers, set_module_exception, set_setting_exception,
    setting_locked,
)
from app.utils.features import FEATURES

ROOT = Path(__file__).resolve().parent.parent


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _package(db, name="Test pack", modules=None, unlisted=OFF, settings=None, limits=None, branding=True):
    p = Package(name=name, modules=modules or {}, unlisted_modules=unlisted, settings=settings or {},
                limits=limits or {}, own_branding=branding)
    db.session.add(p)
    db.session.commit()
    return p


def _other_tenant(db, super_admin, name="Other Co"):
    t = Tenant(legal_name=name, registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
               billing_cycle=BillingCycle.MONTHLY)
    db.session.add(t)
    db.session.commit()
    return t


# --- no change for anyone ----------------------------------------------------------------------------

def test_a_client_without_a_package_is_exactly_as_before(db, tenant):
    tenant.set_feature("inventory", True)
    tenant.set_feature("purchases", False)
    db.session.commit()
    assert tenant.package_grant is None
    assert tenant.has_feature("inventory") and not tenant.has_feature("purchases")
    assert all(client_may_switch(tenant, key) for key in FEATURES)
    assert not any(setting_locked(tenant, key) for key in SETTINGS)
    assert all(limit_of(tenant, key) is None for key in LIMITS) and own_branding(tenant)


def test_current_setup_changes_nothing(db, tenant, super_admin):
    tenant.set_feature("loyalty", True)
    db.session.commit()
    before = {key: tenant.has_feature(key) for key in FEATURES}
    assign_package(tenant, current_setup_package(), super_admin)
    db.session.commit()
    assert {key: tenant.has_feature(key) for key in FEATURES} == before
    assert all(client_may_switch(tenant, key) for key in FEATURES)


def test_the_migration_ships_the_same_packages_as_the_code():
    spec = importlib.util.spec_from_file_location(
        "pkg_migration", ROOT / "migrations" / "versions" / "a7c3f9d21b40_packages_and_powers.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    frozen = {row[0]: row for row in migration.PACKAGES}
    for p in STARTING_PACKAGES:
        key, name, order, unlisted, modules, settings, limits, branding, _ = frozen[p["key"]]
        assert (name, order, unlisted, modules, settings, limits, branding) == (
            p["name"], p["sort_order"], p.get("unlisted_modules", OFF), p["modules"], p["settings"], p["limits"],
            p["own_branding"]), key


def test_starting_packages_name_only_real_modules_and_settings(db):
    ensure_starting_packages()
    ensure_starting_packages()     # a second run adds nothing
    db.session.commit()
    assert Package.query.count() == len(STARTING_PACKAGES)
    for p in Package.query.all():
        assert set(p.modules) <= set(FEATURES), p.name
        assert set(p.settings) <= set(SETTINGS), p.name
        assert set(p.limits) <= set(LIMITS), p.name
    everything = Package.query.filter_by(key="everything").one()
    assert all(module_state_of(everything, k) == CHOICE for k in FEATURES)


def module_state_of(package, key):
    from app.utils.entitlements import package_module_state

    return package_module_state(package, key)


# --- the layers -----------------------------------------------------------------------------------------

def test_package_decides_and_the_client_switches_only_its_choices(db, tenant, super_admin):
    tenant.set_feature("purchases", True)    # switched on before, but not in the package
    tenant.set_feature("loyalty", False)
    db.session.commit()
    pack = _package(db, modules={"inventory": INCLUDED, "loyalty": CHOICE})
    assign_package(tenant, pack, super_admin)
    db.session.commit()
    assert module_state(tenant, "inventory") == INCLUDED and tenant.has_feature("inventory")
    assert module_state(tenant, "purchases") == OFF and not tenant.has_feature("purchases")
    assert not tenant.has_feature("loyalty")
    tenant.set_feature("loyalty", True)
    assert tenant.has_feature("loyalty")
    assert client_may_switch(tenant, "loyalty") and not client_may_switch(tenant, "inventory")


def test_an_exception_beats_the_package_until_cleared(db, tenant, super_admin):
    pack = _package(db, modules={"inventory": INCLUDED})
    assign_package(tenant, pack, super_admin)
    set_module_exception(tenant, "purchases", INCLUDED, super_admin)
    set_module_exception(tenant, "inventory", OFF, super_admin)
    db.session.commit()
    assert tenant.has_feature("purchases") and not tenant.has_feature("inventory")
    set_module_exception(tenant, "inventory", None, super_admin)
    db.session.commit()
    assert tenant.has_feature("inventory") and "inventory" not in tenant.package_grant.module_overrides
    # Setting it to what the package already says stores no exception.
    set_module_exception(tenant, "inventory", INCLUDED, super_admin)
    assert "inventory" not in tenant.package_grant.module_overrides


def test_a_module_taken_away_can_stay_readable(db, tenant, super_admin):
    assign_package(tenant, _package(db), super_admin)
    tenant.package_grant.read_only_modules = ["purchases"]
    db.session.commit()
    assert module_mode(tenant, "purchases") == READ_ONLY and not tenant.has_feature("purchases")
    assert module_mode(tenant, "inventory") == OFF


def test_a_switched_off_module_is_404_even_if_the_client_had_it_on(client, db, tenant, client_admin, super_admin):
    tenant.set_feature("inventory", True)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get("/inventory/").status_code == 200
    assign_package(tenant, _package(db, modules={"purchases": INCLUDED}), super_admin)
    db.session.commit()
    assert client.get("/inventory/").status_code == 404
    assert client.get("/purchases/").status_code == 200    # included, though the client never switched it on


def test_settings_modules_saves_only_the_clients_choices(client, db, tenant, client_admin, super_admin):
    pack = _package(db, modules={"inventory": INCLUDED, "loyalty": CHOICE})
    assign_package(tenant, pack, super_admin)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    # Try to switch everything on, including the Off ones and to switch the
    # Included one off: only Loyalty may change.
    data = {f"feature_{key}": "y" for key in FEATURES if key != "inventory"}
    client.post("/settings/features", data=data)
    db.session.refresh(tenant)
    assert tenant.has_feature("loyalty") and tenant.has_feature("inventory")
    assert not tenant.has_feature("purchases") and not tenant.has_feature("restaurant_mode")
    assert not (tenant.features or {}).get("purchases")


def test_settings_limits_and_branding_follow_package_and_exceptions(db, tenant, super_admin):
    pack = _package(db, settings={"aato": "arfa"}, limits={"branches": 2}, branding=False)
    assign_package(tenant, pack, super_admin)
    db.session.commit()
    assert setting_locked(tenant, "aato") and not setting_locked(tenant, "counter")
    assert limit_of(tenant, "branches") == 2 and limit_of(tenant, "logins") is None
    assert not own_branding(tenant)
    set_setting_exception(tenant, "aato", "client", super_admin)
    tenant.package_grant.limit_overrides = {"branches": None}
    tenant.package_grant.own_branding_override = True
    db.session.commit()
    assert not setting_locked(tenant, "aato") and limit_of(tenant, "branches") is None and own_branding(tenant)


def test_every_change_is_in_the_audit_log(db, tenant, super_admin):
    pack = _package(db)
    assign_package(tenant, pack, super_admin)
    set_module_exception(tenant, "inventory", INCLUDED, super_admin)
    set_setting_exception(tenant, "aato", "arfa", super_admin)
    db.session.commit()
    actions = [a.action for a in AuditLog.query.filter_by(tenant_id=tenant.id).order_by(AuditLog.id)]
    assert actions[-3:] == ["package_assigned", "module_exception_set", "setting_exception_set"]
    assert AuditLog.query.filter_by(action="package_assigned").first().actor_user_id == super_admin.id


def test_one_clients_package_never_touches_another(db, tenant, super_admin):
    other = _other_tenant(db, super_admin)
    other.set_feature("inventory", True)
    tenant.set_feature("inventory", True)
    db.session.commit()
    assign_package(tenant, _package(db), super_admin)          # inventory Off for tenant
    set_module_exception(other, "restaurant_mode", INCLUDED, super_admin)
    db.session.commit()
    assert not tenant.has_feature("inventory") and other.has_feature("inventory")
    assert other.has_feature("restaurant_mode") and not tenant.has_feature("restaurant_mode")
    grants = {g.tenant_id: g for g in TenantPackage.query.all()}
    assert grants[tenant.id].package.name == "Test pack"
    assert grants[other.id].package.key == CURRENT_SETUP      # an exception alone starts from Current setup
    assert grants[other.id].module_overrides == {"restaurant_mode": INCLUDED}


def test_unknown_modules_and_states_are_refused(db, tenant, super_admin):
    with pytest.raises(ValueError):
        set_module_exception(tenant, "nonsense", INCLUDED, super_admin)
    with pytest.raises(ValueError):
        set_module_exception(tenant, "inventory", "maybe", super_admin)
    with pytest.raises(ValueError):
        setting_locked(tenant, "nonsense")


# --- auditor powers ------------------------------------------------------------------------------------

def _auditor(db, email, role=UserRole.AUDITOR, parent=None):
    u = User(name=email, email=email, role=role, parent_auditor_id=parent, must_change_password=False,
             billing_cycle=BillingCycle.MONTHLY)
    u.set_password("AuditorPass123")
    db.session.add(u)
    db.session.commit()
    return u


def test_auditor_powers_default_to_what_each_role_could_do(db, super_admin, client_admin):
    auditor = _auditor(db, "a1@example.com")
    sub = _auditor(db, "s1@example.com", UserRole.SUB_AUDITOR, auditor.id)
    assert all(auditor_can(super_admin, p) for p in AUDITOR_POWERS)
    assert not any(auditor_can(client_admin, p) for p in AUDITOR_POWERS)
    assert auditor_can(auditor, "act_as") and auditor_can(auditor, "create_sub_auditors")
    assert auditor_can(sub, "act_as") and not auditor_can(sub, "create_sub_auditors")
    assert not auditor_can(auditor, "assign_packages")   # new, so given by the Ultra Admin only


def test_one_auditors_powers_are_their_own(db, super_admin):
    a1 = _auditor(db, "a1@example.com")
    a2 = _auditor(db, "a2@example.com")
    set_auditor_powers(a1, {"act_as": False, "assign_packages": True}, super_admin, allowed_package_ids=[1])
    db.session.commit()
    assert not auditor_can(a1, "act_as") and auditor_can(a1, "assign_packages")
    assert auditor_can(a2, "act_as") and not auditor_can(a2, "assign_packages")
    assert AuditLog.query.filter_by(action="auditor_powers_set", entity_id=a1.id).count() == 1
    with pytest.raises(ValueError):
        set_auditor_powers(a1, {"fly": True}, super_admin)
