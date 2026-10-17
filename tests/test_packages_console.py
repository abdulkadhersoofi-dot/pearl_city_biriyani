"""Packages and powers, stage 2: packages in the console. Only the Ultra
Admin sees and edits packages, gives a client a package (with a preview
first), changes single modules / settings / limits / branding for one
client, picks a package when onboarding, and gets the package price as
the client's fee."""

import re
from decimal import Decimal

import pytest

from app.models.audit_log import AuditLog
from app.models.package import Package, TenantPackage
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.utils.entitlements import (
    CHOICE, INCLUDED, OFF, assign_package, ensure_starting_packages, limit_of, own_branding, setting_locked,
)
from app.utils.features import AVAILABLE_FEATURES


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def packs(db):
    ensure_starting_packages()
    db.session.commit()
    return {p.key: p for p in Package.query.all()}


@pytest.fixture()
def ultra(client, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    return super_admin


def _auditor(db, email="aud@example.com"):
    u = User(name="Aud", email=email, role=UserRole.AUDITOR, must_change_password=False,
             billing_cycle=BillingCycle.MONTHLY)
    u.set_password("AuditorPass123")
    db.session.add(u)
    db.session.commit()
    return u


def _editor_form(name, states=None, **extra):
    states = states or {}
    data = {"name": name, "description": "", "price_monthly": "", "price_yearly": "", "limit_branches": "",
            "limit_logins": "", "limit_gstins": "", "unlisted_modules": OFF, "own_branding": "y"}
    for f in AVAILABLE_FEATURES:
        data[f"mod_{f.key}"] = states.get(f.key, OFF)
    data.update(extra)
    return data


# --- who may see it --------------------------------------------------------------------------------

def test_only_the_ultra_admin_reaches_packages(client, db, tenant, client_admin, packs):
    auditor = _auditor(db)
    _login(client, auditor.email, "AuditorPass123")
    assert client.get("/admin/packages").status_code == 403
    assert client.post(f"/admin/clients/{tenant.id}/package", data={"package_id": packs["retail"].id}).status_code == 403
    page = client.get(f"/admin/clients/{tenant.id}")
    if page.status_code == 200:
        assert 'id="sec-package"' not in page.get_data(as_text=True)
    client.get("/auth/logout")
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get("/admin/packages").status_code == 403


def test_the_packages_screen_lists_every_package_and_its_clients(client, db, tenant, ultra, packs):
    assign_package(tenant, packs["retail"], ultra)
    db.session.commit()
    html = client.get("/admin/packages").get_data(as_text=True)
    for p in packs.values():
        assert p.name.replace("&", "&amp;") in html
    retail = html.split(">Retail</a>", 1)[1].split("</article>", 1)[0]
    assert re.search(r"<dt>Clients</dt><dd>1</dd>", retail)
    nav = client.get("/admin/clients").get_data(as_text=True)
    assert 'href="/admin/packages"' in nav


# --- making and editing packages ------------------------------------------------------------------

def test_create_a_package(client, db, ultra, packs):
    res = client.post("/admin/packages/new", data=_editor_form(
        "Kirana Plus", {"inventory": INCLUDED, "loyalty": CHOICE}, price_monthly="499", limit_branches="2",
        set_aato="arfa"))
    assert res.status_code == 302
    p = Package.query.filter_by(name="Kirana Plus").one()
    assert p.modules["inventory"] == INCLUDED and p.modules["loyalty"] == CHOICE and p.modules["purchases"] == OFF
    assert p.price_monthly == Decimal("499.00") and p.limits == {"branches": 2} and p.settings["aato"] == "arfa"
    assert AuditLog.query.filter_by(action="package_created", entity_id=p.id).count() == 1


def test_bad_package_input_is_refused(client, db, ultra, packs):
    for data in (_editor_form(""), _editor_form("Retail"), _editor_form("X", price_monthly="abc"),
                 _editor_form("Y", limit_logins="two")):
        page = client.post("/admin/packages/new", data=data, follow_redirects=True).get_data(as_text=True)
        assert 'role="alert"' in page or "toast-error" in page
    assert Package.query.filter(Package.name.in_(["", "X", "Y"])).count() == 0


def test_a_copy_starts_from_the_original(client, db, ultra, packs):
    html = client.get(f"/admin/packages/new?copy={packs['pharmacy'].id}").get_data(as_text=True)
    assert 'value="Pharmacy (copy)"' in html
    assert re.search(r'name="mod_pharmacy" value="inc" id="mod_pharmacy_inc" checked', html)


def test_editing_a_package_changes_it_for_its_clients(client, db, tenant, ultra, packs):
    assign_package(tenant, packs["billing"], ultra)
    db.session.commit()
    assert not tenant.has_feature("inventory")
    page = client.get(f"/admin/packages/{packs['billing'].id}").get_data(as_text=True)
    assert "Used by 1 client" in page
    states = {k: v for k, v in packs["billing"].modules.items()}
    states["inventory"] = INCLUDED
    client.post(f"/admin/packages/{packs['billing'].id}", data=_editor_form("Billing", states, own_branding=""))
    db.session.expire_all()
    assert tenant.has_feature("inventory")
    assert AuditLog.query.filter_by(action="package_edited").count() == 1


def test_a_retired_package_cant_be_given(client, db, tenant, ultra, packs):
    client.post(f"/admin/packages/{packs['retail'].id}/active", data={"active": "0"})
    assert not db.session.get(Package, packs["retail"].id).is_active
    client.post(f"/admin/clients/{tenant.id}/package", data={"package_id": packs["retail"].id})
    assert tenant.package_grant is None or tenant.package_grant.package.key != "retail"


# --- a client's package -----------------------------------------------------------------------------

def test_preview_says_what_changes_for_this_client(client, db, tenant, ultra, packs):
    tenant.set_feature("restaurant_mode", True)
    db.session.commit()
    html = client.get(f"/admin/clients/{tenant.id}/package/preview?package_id={packs['billing'].id}").get_data(as_text=True)
    assert "Switch off <strong>Restaurant tables" in html
    assert "Switch on <strong>Credit sales" in html


def test_giving_a_package_with_its_price(client, db, tenant, ultra, packs):
    packs["retail"].price_monthly = Decimal("799")
    packs["retail"].price_yearly = Decimal("7999")
    tenant.billing_cycle = BillingCycle.YEARLY
    db.session.commit()
    client.post(f"/admin/clients/{tenant.id}/package", data={"package_id": packs["retail"].id, "use_price": "y"})
    db.session.expire_all()
    assert tenant.package_grant.package.key == "retail" and tenant.has_feature("inventory")
    assert tenant.subscription_fee == Decimal("7999.00")
    page = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    assert 'id="sec-package"' in page and "On <strong>Retail</strong>" in page


def test_exceptions_for_one_client(client, db, tenant, ultra, packs):
    assign_package(tenant, packs["retail"], ultra)
    db.session.commit()
    client.post(f"/admin/clients/{tenant.id}/exceptions", data={
        "mod_inventory": "off", "mod_job_cards": "inc", "set_counter": "arfa",
        "limitmode_branches": "number", "limit_branches": "7", "limitmode_logins": "none",
        "limitmode_gstins": "package", "own_branding": "no"})
    db.session.expire_all()
    assert not tenant.has_feature("inventory") and tenant.has_feature("job_cards")
    assert setting_locked(tenant, "counter")
    assert limit_of(tenant, "branches") == 7 and limit_of(tenant, "logins") is None and limit_of(tenant, "gstins") == 1
    assert not own_branding(tenant)
    # Back to the package.
    client.post(f"/admin/clients/{tenant.id}/exceptions", data={
        "mod_inventory": "package", "mod_job_cards": "package", "set_counter": "package",
        "limitmode_branches": "package", "limitmode_logins": "package", "own_branding": "package"})
    db.session.expire_all()
    grant = tenant.package_grant
    assert grant.module_overrides == {} and grant.setting_overrides == {} and grant.limit_overrides == {}
    assert grant.own_branding_override is None and tenant.has_feature("inventory")


def test_one_clients_exceptions_stay_with_that_client(client, db, tenant, ultra, packs, super_admin):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.commit()
    assign_package(tenant, packs["retail"], ultra)
    assign_package(other, packs["retail"], ultra)
    db.session.commit()
    client.post(f"/admin/clients/{tenant.id}/exceptions", data={"mod_inventory": "off"})
    db.session.expire_all()
    assert not tenant.has_feature("inventory") and other.has_feature("inventory")
    assert TenantPackage.query.filter_by(tenant_id=other.id).one().module_overrides == {}


# --- onboarding and verification ----------------------------------------------------------------------

def _onboard(client, name, email, **extra):
    data = {"legal_name": name, "registration_type": "regular", "state_code": "27", "gstin": "",
            "business_type": "pharmacy", "admin_name": "Admin", "admin_email": email, "admin_phone": "",
            "admin_password": "InitialPass123", "admin_confirm_password": "InitialPass123", **extra}
    return client.post("/admin/clients/new", data=data)


def test_the_ultra_admin_onboards_onto_a_package(client, db, ultra, packs):
    form = client.get("/admin/clients/new").get_data(as_text=True)
    assert 'name="package_id"' in form
    _onboard(client, "Medi Plus", "medi@example.com", package_id=packs["pharmacy"].id)
    t = Tenant.query.filter_by(legal_name="Medi Plus").one()
    assert t.package_grant.package.key == "pharmacy" and t.has_feature("pharmacy")


def test_an_auditor_onboards_as_before(client, db, packs):
    auditor = _auditor(db)
    _login(client, auditor.email, "AuditorPass123")
    assert 'name="package_id"' not in client.get("/admin/clients/new").get_data(as_text=True)
    _onboard(client, "Aud Shop", "audshop@example.com", package_id=packs["everything"].id)
    t = Tenant.query.filter_by(legal_name="Aud Shop").one()
    assert t.package_grant is None


def test_verifying_a_client_on_a_priced_package_sets_the_fee(client, db, ultra, packs, super_admin):
    packs["retail"].price_monthly = Decimal("650")
    t = Tenant(legal_name="New Shop", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id)
    db.session.add(t)
    db.session.flush()
    db.session.add(Gstin(tenant_id=t.id, gstin=None, state_code="27", state_name="Maharashtra", is_primary=True))
    assign_package(t, packs["retail"], super_admin)
    db.session.commit()
    client.post(f"/admin/clients/{t.id}/billing/set-cycle", data={"billing_cycle": "monthly"})
    db.session.expire_all()
    assert t.subscription_fee == Decimal("650.00")
