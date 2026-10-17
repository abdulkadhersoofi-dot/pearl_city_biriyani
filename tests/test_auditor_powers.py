"""Packages and powers, stage 4: auditor powers. The Ultra Admin sets each
auditor's powers, allowed packages and limits; an Auditor sets their own
Sub-Auditors' within their own; every console page, button and "Act as"
follows them."""

import pytest

from app.models.audit_log import AuditLog
from app.models.package import Package
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.utils.entitlements import (
    AUDITOR_POWERS, CHOICE, INCLUDED, OFF, assign_package, auditor_can, ensure_starting_packages, set_auditor_powers,
)


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _auditor(db, email, role=UserRole.AUDITOR, parent=None):
    u = User(name=email.split("@")[0], email=email, role=role, parent_auditor_id=parent, must_change_password=False,
             billing_cycle=BillingCycle.MONTHLY)
    u.set_password("AuditorPass123")
    db.session.add(u)
    db.session.commit()
    return u


def _client(db, name, auditor_id=None):
    t = Tenant(legal_name=name, registration_type=RegistrationType.REGULAR, auditor_id=auditor_id,
               billing_cycle=BillingCycle.MONTHLY)
    db.session.add(t)
    db.session.flush()
    db.session.add(Gstin(tenant_id=t.id, gstin=None, state_code="27", state_name="Maharashtra", is_primary=True))
    db.session.commit()
    return t


def _client_admin(db, tenant, email):
    u = User(tenant_id=tenant.id, name="Owner", email=email, role=UserRole.CLIENT_ADMIN, must_change_password=False)
    u.set_password("ClientSecret123")
    db.session.add(u)
    db.session.commit()
    return u


def _onboard(client, name, email, **extra):
    data = {"legal_name": name, "registration_type": "regular", "state_code": "27", "gstin": "",
            "business_type": "retail", "admin_name": "Admin", "admin_email": email, "admin_phone": "",
            "admin_password": "InitialPass123", "admin_confirm_password": "InitialPass123", **extra}
    return client.post("/admin/clients/new", data=data)


@pytest.fixture()
def office(db):
    auditor = _auditor(db, "office@example.com")
    shop = _client(db, "Office Shop", auditor.id)
    return auditor, shop


# --- the Ultra Admin sets powers -------------------------------------------------------------------

def test_ultra_admin_sets_an_auditors_powers(client, db, super_admin, office):
    auditor, _ = office
    ensure_starting_packages()
    db.session.commit()
    retail = Package.query.filter_by(key="retail").one()
    _login(client, super_admin.email, "SuperSecret123")
    page = client.get(f"/admin/auditors/{auditor.id}").get_data(as_text=True)
    assert 'id="sec-powers"' in page and "Choose a package for their clients" in page
    client.post(f"/admin/auditors/{auditor.id}/powers", data={
        "power": ["onboard_clients", "assign_packages", "month_end"], "package_id": [str(retail.id)],
        "max_clients": "3", "max_sub_auditors": "1"})
    db.session.expire_all()
    assert auditor_can(auditor, "assign_packages") and auditor_can(auditor, "month_end")
    assert not auditor_can(auditor, "act_as") and not auditor_can(auditor, "pause_clients")
    grant = auditor.power_grant
    assert grant.allowed_package_ids == [retail.id] and grant.max_clients == 3 and grant.max_sub_auditors == 1
    assert AuditLog.query.filter_by(action="auditor_powers_set", entity_id=auditor.id).count() == 1


def test_a_client_admin_or_other_auditor_cant_set_powers(client, db, office, tenant, client_admin):
    auditor, _ = office
    other = _auditor(db, "other@example.com")
    _login(client, other.email, "AuditorPass123")
    assert client.post(f"/admin/auditors/{auditor.id}/powers", data={"power": list(AUDITOR_POWERS)}).status_code in (403, 404)
    client.get("/auth/logout")
    _login(client, client_admin.email, "ClientSecret123")
    assert client.post(f"/admin/auditors/{auditor.id}/powers", data={}).status_code in (302, 403)
    assert auditor.power_grant is None


# --- every console page follows the powers -----------------------------------------------------------

def test_without_a_power_its_pages_and_buttons_are_gone(client, db, super_admin, office):
    auditor, shop = office
    admin = _client_admin(db, shop, "owner@officeshop.example.com")
    set_auditor_powers(auditor, {p: False for p in AUDITOR_POWERS}, super_admin)
    db.session.commit()
    _login(client, auditor.email, "AuditorPass123")
    assert client.get("/admin/clients/new").status_code == 403
    assert client.post(f"/admin/clients/{shop.id}/deactivate").status_code == 403
    assert client.post(f"/admin/clients/{shop.id}/users/{admin.id}/act-as").status_code == 403
    assert client.get(f"/admin/clients/{shop.id}/users/{admin.id}/reset-password").status_code == 403
    assert client.post(f"/admin/clients/{shop.id}/gstins/new", data={}).status_code == 403
    assert client.get("/admin/filing").status_code == 403
    assert client.get(f"/admin/clients/{shop.id}/monthly-pack.zip?period=2026-09").status_code == 403
    assert client.get("/admin/auditors/new").status_code == 403
    page = client.get(f"/admin/clients/{shop.id}").get_data(as_text=True)
    assert '/act-as"' not in page and "Pause access" not in page and "Reset password" not in page
    assert 'id="sec-month-end"' not in page and "Upload logo" not in page
    directory = client.get("/admin/clients").get_data(as_text=True)
    assert "Onboard client" not in directory and 'href="/admin/filing"' not in directory


def test_with_todays_powers_nothing_changes(client, db, office):
    auditor, shop = office
    _client_admin(db, shop, "owner2@officeshop.example.com")
    _login(client, auditor.email, "AuditorPass123")
    page = client.get(f"/admin/clients/{shop.id}").get_data(as_text=True)
    assert "Act as" in page and "Pause access" in page and 'id="sec-month-end"' in page
    assert client.get("/admin/clients/new").status_code == 200


def test_client_limit(client, db, super_admin, office):
    auditor, _ = office          # already has one client
    set_auditor_powers(auditor, {}, super_admin, max_clients=1)
    db.session.commit()
    _login(client, auditor.email, "AuditorPass123")
    res = _onboard(client, "Second Shop", "second@example.com")
    assert res.status_code == 302 and Tenant.query.filter_by(legal_name="Second Shop").count() == 0


# --- packages for auditors ------------------------------------------------------------------------------

def test_an_auditor_gives_only_allowed_packages_to_own_clients(client, db, super_admin, office):
    auditor, shop = office
    elsewhere = _client(db, "Not Mine")
    ensure_starting_packages()
    db.session.commit()
    retail = Package.query.filter_by(key="retail").one()
    everything = Package.query.filter_by(key="everything").one()
    set_auditor_powers(auditor, {"assign_packages": True}, super_admin, allowed_package_ids=[retail.id])
    db.session.commit()
    _login(client, auditor.email, "AuditorPass123")
    page = client.get(f"/admin/clients/{shop.id}").get_data(as_text=True)
    assert "Put Office Shop on this package" in page and ">Everything" not in page
    assert "Change single modules or settings" not in page          # exceptions stay with the Ultra Admin
    client.post(f"/admin/clients/{shop.id}/package", data={"package_id": everything.id})
    assert shop.package_grant is None
    client.post(f"/admin/clients/{shop.id}/package", data={"package_id": retail.id})
    db.session.expire_all()
    assert shop.package_grant.package.key == "retail"
    assert client.post(f"/admin/clients/{elsewhere.id}/package", data={"package_id": retail.id}).status_code in (403, 404)
    # Onboarding offers the allowed package.
    form = client.get("/admin/clients/new").get_data(as_text=True)
    assert 'name="package_id"' in form and ">Retail" in form and ">Everything" not in form
    _onboard(client, "Third Shop", "third@example.com", package_id=retail.id)
    assert Tenant.query.filter_by(legal_name="Third Shop").one().package_grant.package.key == "retail"


def test_switching_a_clients_own_choice_modules(client, db, super_admin, office):
    auditor, shop = office
    pack = Package(name="Small", modules={"inventory": INCLUDED, "loyalty": CHOICE}, unlisted_modules=OFF)
    db.session.add(pack)
    db.session.commit()
    assign_package(shop, pack, super_admin)
    db.session.commit()
    _login(client, auditor.email, "AuditorPass123")
    client.post(f"/admin/clients/{shop.id}/choices", data={"module": ["loyalty", "purchases"]})
    db.session.expire_all()
    assert shop.has_feature("loyalty") and not shop.has_feature("purchases")   # Off in the package stays off
    set_auditor_powers(auditor, {"switch_client_modules": False}, super_admin)
    db.session.commit()
    assert client.post(f"/admin/clients/{shop.id}/choices", data={}).status_code == 403


def test_act_as_carries_the_auditors_powers(client, db, super_admin, office):
    auditor, shop = office
    admin = _client_admin(db, shop, "owner3@officeshop.example.com")
    set_auditor_powers(auditor, {"switch_client_modules": False}, super_admin)
    db.session.commit()
    _login(client, auditor.email, "AuditorPass123")
    client.post(f"/admin/clients/{shop.id}/users/{admin.id}/act-as")
    assert client.post("/settings/features", data={"feature_loyalty": "y"}).status_code == 403
    assert not shop.has_feature("loyalty")


# --- Sub-Auditors -------------------------------------------------------------------------------------

def test_an_auditor_sets_sub_auditor_powers_within_their_own(client, db, super_admin, office):
    auditor, _ = office
    sub = _auditor(db, "sub@example.com", UserRole.SUB_AUDITOR, auditor.id)
    set_auditor_powers(auditor, {"act_as": False}, super_admin)
    db.session.commit()
    _login(client, auditor.email, "AuditorPass123")
    page = client.get(f"/admin/auditors/{sub.id}").get_data(as_text=True)
    assert 'id="sec-powers"' in page and "have this power yourself" in page
    client.post(f"/admin/auditors/{sub.id}/powers", data={"power": ["act_as", "month_end", "create_sub_auditors"]})
    db.session.expire_all()
    assert auditor_can(sub, "month_end")
    assert not auditor_can(sub, "act_as") and not auditor_can(sub, "create_sub_auditors")
    assert not auditor_can(sub, "pause_clients")          # not ticked


def test_a_sub_auditor_loses_a_power_their_auditor_loses(db, super_admin, office):
    auditor, _ = office
    sub = _auditor(db, "sub2@example.com", UserRole.SUB_AUDITOR, auditor.id)
    assert auditor_can(sub, "act_as")
    set_auditor_powers(auditor, {"act_as": False}, super_admin)
    db.session.commit()
    assert not auditor_can(sub, "act_as")


def test_sub_auditor_limit(client, db, super_admin, office):
    auditor, _ = office
    _auditor(db, "sub3@example.com", UserRole.SUB_AUDITOR, auditor.id)
    set_auditor_powers(auditor, {}, super_admin, max_sub_auditors=1)
    db.session.commit()
    _login(client, auditor.email, "AuditorPass123")
    res = client.get("/admin/auditors/new")
    assert res.status_code == 302


def test_a_sub_auditor_loses_a_package_their_auditor_loses(db, super_admin, office):
    from app.utils.entitlements import allowed_packages
    auditor, _ = office
    sub = _auditor(db, "sub4@example.com", UserRole.SUB_AUDITOR, auditor.id)
    ensure_starting_packages()
    db.session.commit()
    retail = Package.query.filter_by(key="retail").one()
    set_auditor_powers(auditor, {"assign_packages": True}, super_admin, allowed_package_ids=[retail.id])
    set_auditor_powers(sub, {"assign_packages": True}, auditor, allowed_package_ids=[retail.id])
    db.session.commit()
    assert allowed_packages(sub) == [retail]
    set_auditor_powers(auditor, {"assign_packages": True}, super_admin, allowed_package_ids=[])
    db.session.commit()
    assert allowed_packages(sub) == []
