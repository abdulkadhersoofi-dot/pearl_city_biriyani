"""Packages and powers, stage 5: branches, logins and style. Branch logins
get roles capped by the package; a branch can be left without some
modules; the Ultra Admin has switches for any one login, marked "Set by
ARFA"; branch / login / GSTIN limits with a "3 of 5 used" meter; own
branding by package; sign-in minimums the client may tighten, never loosen.
Everything stays as it was for a client without a package."""

from decimal import Decimal

import pytest

from app.auth import security
from app.models.audit_log import AuditLog
from app.models.package import LoginGrant, Package
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.utils.entitlements import (
    INCLUDED, OFF, assign_package, brand_logo_id, counter_permissions_allowed, limit_meter, login_hides,
    security_minimum, set_login_switches, strictest_minutes,
)
from app.utils.permissions import max_discount_percent, user_can
from app.utils.theme import tenant_theme_css


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _pack(db, **kw):
    p = Package(name=kw.pop("name", "Small shop"), modules=kw.pop("modules", {}), unlisted_modules=kw.pop("unlisted", "choice"),
                settings={}, limits=kw.pop("limits", {}), security=kw.pop("security", {}), **kw)
    db.session.add(p)
    db.session.commit()
    return p


def _branch_form(name, email, role="cashier"):
    return {"name": name, "email": email, "phone": "", "branch_type": "owned", "role": role,
            "password": "BranchPass123", "confirm_password": "BranchPass123"}


# --- nothing changes without a package ---------------------------------------------------------------

def test_a_client_without_a_package_keeps_everything(db, tenant, client_admin, staff):
    assert counter_permissions_allowed(tenant) == {"discount", "refund", "void", "view_reports", "edit_price",
                                                   "manage_stock"}
    assert user_can(staff, "refund") and not user_can(staff, "void")          # today's staff defaults
    assert security_minimum(tenant, "idle_minutes") is None and not security_minimum(tenant, "owner_two_step")
    assert limit_meter(tenant, "branches")["limit"] is None
    assert not security.two_factor_required(client_admin)


# --- roles -----------------------------------------------------------------------------------------------

def test_a_new_branch_gets_its_role(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/branches/new", data=_branch_form("Mall counter", "mall@acme.example.com", "senior_cashier"))
    branch = User.query.filter_by(email="mall@acme.example.com").one()
    assert branch.branch_role == "senior_cashier" and branch.can_void and branch.can_give_discount
    assert max_discount_percent(branch) == Decimal("10") and not branch.can_manage_stock
    page = client.get("/branches/").get_data(as_text=True)
    assert "Senior cashier" in page


def test_an_existing_login_keeps_its_six_permissions(client, db, tenant, client_admin, staff):
    staff.can_void = True
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    assert "Own set" in client.get("/branches/").get_data(as_text=True)
    assert user_can(staff, "void") and user_can(staff, "refund")


def test_changing_one_permission_makes_it_an_own_set(client, db, tenant, client_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")
    client.post(f"/branches/{staff.id}/permissions", data={"role": "stock_keeper"})
    db.session.expire_all()
    assert staff.branch_role == "stock_keeper" and staff.can_manage_stock and not staff.can_refund
    client.post(f"/branches/{staff.id}/permissions",
                data={"role": "own", "can_manage_stock": "y", "can_refund": "y", "can_view_reports": "y"})
    db.session.expire_all()
    assert staff.branch_role is None and staff.can_refund


def test_the_package_caps_what_a_role_hands_out(client, db, tenant, client_admin, staff, super_admin):
    assign_package(tenant, _pack(db, counter_permissions=["refund", "view_reports", "discount"]), super_admin)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    client.post(f"/branches/{staff.id}/permissions", data={"role": "manager"})
    db.session.expire_all()
    assert staff.branch_role == "manager"
    assert user_can(staff, "discount") and not user_can(staff, "void") and not user_can(staff, "manage_stock")
    page = client.get(f"/branches/{staff.id}/permissions").get_data(as_text=True)
    assert "Not in your package" in page


# --- modules at one branch ----------------------------------------------------------------------------------

def test_a_branch_can_be_left_without_a_module(client, db, tenant, client_admin, staff):
    tenant.set_feature("job_cards", True)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    client.post(f"/branches/{staff.id}/permissions",                 # on the form, not ticked: hidden here
                data={"role": "cashier", "module_shown": ["job_cards"], "module": []})
    db.session.expire_all()
    assert staff.hidden_modules == ["job_cards"] and login_hides(staff, "job_cards")
    assert tenant.has_feature("job_cards")                            # the business keeps it
    assert client.get("/jobs/").status_code == 200                    # the owner still opens it
    client.get("/auth/logout")
    _login(client, staff.email, "CashierSecret123")
    assert client.get("/jobs/").status_code == 404
    assert "Jobs</span>" not in client.get("/pos/").get_data(as_text=True)


# --- the Ultra Admin's switches for one login ----------------------------------------------------------------

def test_ultra_admin_switches_for_one_login(client, db, tenant, client_admin, staff, super_admin):
    tenant.set_feature("job_cards", True)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    assert f'id="login-{staff.id}"' in client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    client.post(f"/admin/clients/{tenant.id}/logins/{staff.id}/switches",
                data={"perm_refund": "off", "perm_void": "on", "max_discount": "", "hide": ["job_cards"]})
    db.session.expire_all()
    assert not user_can(staff, "refund") and user_can(staff, "void") and login_hides(staff, "job_cards")
    assert AuditLog.query.filter_by(action="login_switches_set", entity_id=staff.id).count() == 1
    # The Client Admin sees them as "Set by ARFA" and can't change them.
    client.get("/auth/logout")
    _login(client, client_admin.email, "ClientSecret123")
    page = client.get(f"/branches/{staff.id}/permissions").get_data(as_text=True)
    assert "Set by ARFA" in page and "hidden by ARFA" in page
    client.post(f"/branches/{staff.id}/permissions",
                data={"role": "manager", "module_shown": ["job_cards"], "module": ["job_cards"]})
    db.session.expire_all()
    assert not user_can(staff, "refund") and login_hides(staff, "job_cards")
    assert client.post(f"/admin/clients/{tenant.id}/logins/{staff.id}/switches", data={}).status_code in (302, 403)


def test_handing_a_login_back_to_the_client(db, client, tenant, staff, super_admin):
    set_login_switches(staff, super_admin, permissions={"refund": False}, max_discount_percent=Decimal("5"))
    db.session.commit()
    assert not user_can(staff, "refund")
    _login(client, super_admin.email, "SuperSecret123")
    client.post(f"/admin/clients/{tenant.id}/logins/{staff.id}/switches", data={"action": "clear"})
    db.session.expire_all()
    assert user_can(staff, "refund") and staff.login_grant.permissions == {}


def test_arfa_discount_limit_caps_the_clients(db, tenant, staff, super_admin):
    staff.can_give_discount, staff.max_discount_percent = True, Decimal("20")
    set_login_switches(staff, super_admin, max_discount_percent=Decimal("5"))
    db.session.commit()
    assert max_discount_percent(staff) == Decimal("5")


def test_an_auditor_cant_set_login_switches(client, db, tenant, staff):
    auditor = User(name="Aud", email="aud@example.com", role=UserRole.AUDITOR, must_change_password=False,
                   billing_cycle=BillingCycle.MONTHLY)
    auditor.set_password("AuditorPass123")
    db.session.add(auditor)
    db.session.flush()
    tenant.auditor_id = auditor.id
    db.session.commit()
    _login(client, auditor.email, "AuditorPass123")
    assert client.post(f"/admin/clients/{tenant.id}/logins/{staff.id}/switches",
                       data={"perm_refund": "off"}).status_code == 403
    assert f'id="login-{staff.id}"' not in client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    assert user_can(staff, "refund")


def test_login_switches_stay_with_their_client(client, db, tenant, staff, super_admin):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    # Another client's address can't reach this login.
    assert client.post(f"/admin/clients/{other.id}/logins/{staff.id}/switches",
                       data={"perm_refund": "off"}).status_code == 404
    set_login_switches(staff, super_admin, permissions={"refund": False})
    db.session.commit()
    grant = LoginGrant.query.one()
    assert grant.tenant_id == tenant.id
    assert LoginGrant.query.filter_by(tenant_id=other.id).count() == 0


# --- limits -------------------------------------------------------------------------------------------------

def test_branch_and_login_limits(client, db, tenant, client_admin, super_admin):
    assign_package(tenant, _pack(db, limits={"branches": 1, "logins": 2}), super_admin)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    assert "0 of 1</strong> branches used" in client.get("/branches/").get_data(as_text=True)
    client.post("/branches/new", data=_branch_form("One", "one@acme.example.com"))
    assert User.query.filter_by(email="one@acme.example.com").count() == 1
    page = client.get("/branches/").get_data(as_text=True)
    assert "1 of 1</strong> branches used" in page and "2 of 2</strong> logins used" in page
    res = client.post("/branches/new", data=_branch_form("Two", "two@acme.example.com"))
    assert res.status_code == 302 and User.query.filter_by(email="two@acme.example.com").count() == 0
    # A switched-off branch can't come back while the limit is full either.
    one = User.query.filter_by(email="one@acme.example.com").one()
    client.post(f"/branches/{one.id}/deactivate")
    client.post("/branches/new", data=_branch_form("Three", "three@acme.example.com"))
    client.post(f"/branches/{one.id}/activate")
    db.session.expire_all()
    assert not one.is_active


def test_gstin_limit_in_the_console(client, db, tenant, super_admin):
    assign_package(tenant, _pack(db, limits={"gstins": 1}), super_admin)
    auditor = User(name="Aud", email="aud2@example.com", role=UserRole.AUDITOR, must_change_password=False,
                   billing_cycle=BillingCycle.MONTHLY)
    auditor.set_password("AuditorPass123")
    db.session.add(auditor)
    db.session.flush()
    tenant.auditor_id = auditor.id
    db.session.commit()
    data = {"gstin": "", "state_code": "29", "registered_address": "Bengaluru"}
    _login(client, auditor.email, "AuditorPass123")
    assert "1 of 1</strong> GSTINs used" in client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    client.post(f"/admin/clients/{tenant.id}/gstins/new", data=data)
    assert Gstin.query.filter_by(tenant_id=tenant.id).count() == 1
    client.get("/auth/logout")
    _login(client, super_admin.email, "SuperSecret123")              # the Ultra Admin may go past it
    client.post(f"/admin/clients/{tenant.id}/gstins/new", data=data)
    assert Gstin.query.filter_by(tenant_id=tenant.id).count() == 2


# --- sign-in minimums -------------------------------------------------------------------------------------

def test_idle_minimum_the_client_may_tighten_never_loosen(client, db, tenant, client_admin, super_admin):
    assign_package(tenant, _pack(db, security={"idle_minutes": 30}), super_admin)
    db.session.commit()
    assert security.idle_limit_minutes(client_admin) == 30                    # "never" became 30
    _login(client, client_admin.email, "ClientSecret123")
    page = client.get("/settings/security").get_data(as_text=True)
    assert "30 minutes at most" in page
    client.post("/settings/security", data={"idle_minutes": "480"})
    from app.utils.module_settings import module_settings
    assert module_settings(tenant, "security")["idle_minutes"] == 30
    client.post("/settings/security", data={"idle_minutes": "15"})
    assert module_settings(tenant, "security")["idle_minutes"] == 15
    assert strictest_minutes(0, None) == 0 and strictest_minutes(60, 30) == 30


def test_two_step_for_the_owner(client, db, tenant, client_admin, super_admin):
    assign_package(tenant, _pack(db, security={"owner_two_step": True}), super_admin)
    db.session.commit()
    assert security.two_factor_required(client_admin)
    res = client.post("/auth/login", data={"email": client_admin.email, "password": "ClientSecret123"})
    assert res.status_code == 302 and res.headers["Location"].endswith("/auth/two-factor/setup")


def test_client_exceptions_for_counter_permissions_and_minimums(client, db, tenant, super_admin, staff):
    assign_package(tenant, _pack(db, security={"idle_minutes": 30}), super_admin)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    client.post(f"/admin/clients/{tenant.id}/exceptions",
                data={"counter_mode": "own", "counter_permission": ["refund"], "secmode_idle_minutes": "none",
                      "secmode_owner_two_step": "yes"})
    db.session.expire_all()
    assert counter_permissions_allowed(tenant) == {"refund"}
    assert security_minimum(tenant, "idle_minutes") is None and security_minimum(tenant, "owner_two_step")
    assert not user_can(staff, "view_reports") and user_can(staff, "refund")


def test_package_editor_saves_counter_permissions_and_minimums(client, db, super_admin):
    pack = _pack(db, name="Editor test", unlisted="off")
    _login(client, super_admin.email, "SuperSecret123")
    from app.utils.features import AVAILABLE_FEATURES
    data = {"name": "Editor test", "unlisted_modules": "off", "counter_form": "1",
            "counter_permission": ["refund", "view_reports"], "sec_idle_minutes": "60", "sec_lock_minutes": "5",
            "sec_owner_two_step": "y", **{f"mod_{f.key}": OFF for f in AVAILABLE_FEATURES}}
    client.post(f"/admin/packages/{pack.id}", data=data)
    db.session.expire_all()
    assert pack.counter_permissions == ["refund", "view_reports"]
    assert pack.security == {"idle_minutes": 60, "lock_minutes": 5, "owner_two_step": True}
    data["counter_permission"] = ["discount", "refund", "void", "view_reports", "edit_price", "manage_stock"]
    client.post(f"/admin/packages/{pack.id}", data=data)
    db.session.expire_all()
    assert pack.counter_permissions is None                            # all of them = no narrowing


# --- branding by package --------------------------------------------------------------------------------------

def test_without_own_branding_the_client_gets_arfas_look(client, db, tenant, client_admin, super_admin):
    tenant.primary_color = "#aa0000"
    tenant.login_slug = "acme"
    tenant.logo_image_id = None
    db.session.commit()
    assert "#aa0000" in tenant_theme_css(tenant)
    assert client.get("/auth/login/acme").status_code == 200
    assign_package(tenant, _pack(db, own_branding=False), super_admin)
    db.session.commit()
    assert tenant_theme_css(tenant) == "" and brand_logo_id(tenant) is None
    assert client.get("/auth/login/acme").status_code == 302
    _login(client, client_admin.email, "ClientSecret123")
    page = client.get("/settings/appearance").get_data(as_text=True)
    assert "uses ARFA's look" in page
    assert client.post("/settings/appearance", data={"primary_color": "#000000"}).status_code == 403


def test_cashier_pins_count_as_logins(client, db, tenant, client_admin, staff, super_admin):
    from app.models.platform import Cashier
    assign_package(tenant, _pack(db, limits={"logins": 2}), super_admin)      # the owner + one branch
    tenant.set_feature("cashier_pin", True)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    assert "2 of 2</strong> logins used" in client.get("/branches/cashiers").get_data(as_text=True)
    client.post("/branches/cashiers", data={"action": "add", "counter_id": staff.id, "name": "Ravi", "pin": "4826"})
    assert Cashier.query.filter_by(tenant_id=tenant.id).count() == 0
