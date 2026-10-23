"""A client's business details can be changed after onboarding - names,
GST registration, business type, turnover, GSTINs and addresses - from the
console (Ultra Admin, or an auditor with the "edit client details" power).
Documents already issued never change: a location that has issued them is
switched off and a new one takes a new GSTIN."""

import html
import uuid
from decimal import Decimal

import pytest

from app.models.audit_log import AuditLog
from app.models.pos_bill import POSBill
from app.models.product import Product
from app.models.tenant import BusinessType, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole

D = Decimal


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password})


@pytest.fixture()
def unregistered(db, tenant):
    tenant.registration_type = RegistrationType.UNREGISTERED
    loc = tenant.gstins.first()
    loc.gstin = None
    db.session.commit()
    return tenant


def _form(tenant, **over):
    loc = tenant.gstins.filter_by(is_active=True).first()
    data = {"legal_name": tenant.legal_name, "trade_name": tenant.trade_name or "",
            "registration_type": tenant.registration_type.value, "business_type": tenant.business_type.value,
            "aato": "", "primary_id": str(loc.id),
            f"loc-{loc.id}-gstin": loc.gstin or "", f"loc-{loc.id}-state": loc.state_code,
            f"loc-{loc.id}-address": loc.registered_address or "", f"loc-{loc.id}-city": loc.city or "",
            f"loc-{loc.id}-pincode": loc.pincode or ""}
    data.update({k.replace("LOC", str(loc.id)): v for k, v in over.items()})
    return data


def _sell(client, db, tenant):
    tea = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="9963", gst_rate=D("5"), unit="NOS",
                  sale_price=D("20"))
    db.session.add(tea)
    db.session.commit()
    res = client.post("/pos/checkout", data={
        "line_product_id[]": str(tea.id), "line_description[]": "Tea", "line_hsn[]": "9963", "line_qty[]": "1",
        "line_rate[]": "20", "line_gst_rate[]": "5", "line_unit[]": "NOS", "payment_mode": "cash",
        "checkout_uuid": str(uuid.uuid4())}, headers={"X-Requested-With": "XMLHttpRequest"})
    assert res.status_code == 200, res.get_json()
    return db.session.get(POSBill, res.get_json()["bill_id"])


def test_names_type_and_turnover_change(client, db, tenant, super_admin):
    _login(client, "firm@example.com", "SuperSecret123")
    page = client.get(f"/admin/clients/{tenant.id}/details")
    assert page.status_code == 200 and "Edit details" in page.get_data(as_text=True)
    assert f"/admin/clients/{tenant.id}/details" in client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    res = client.post(f"/admin/clients/{tenant.id}/details",
                      data=_form(tenant, legal_name="Acme Traders LLP", trade_name="Acme", business_type="restaurant",
                                 aato="1500000"))
    assert res.status_code == 302
    t = db.session.get(Tenant, tenant.id)
    assert (t.legal_name, t.trade_name, t.business_type, t.aato) == ("Acme Traders LLP", "Acme", BusinessType.RESTAURANT,
                                                                     D("1500000"))
    log = AuditLog.query.filter_by(action="client_details_changed").one()
    assert log.details["before"]["legal_name"] == "Acme Traders" and len(log.details["changes"]) == 4


def test_an_unregistered_client_becomes_registered(client, db, unregistered, super_admin):
    _login(client, "firm@example.com", "SuperSecret123")
    form = _form(unregistered, registration_type="regular")
    res = client.post(f"/admin/clients/{unregistered.id}/details", data=form)
    assert "needs a GSTIN on every business location" in res.get_data(as_text=True)
    assert db.session.get(Tenant, unregistered.id).registration_type == RegistrationType.UNREGISTERED  # nothing saved
    res = client.post(f"/admin/clients/{unregistered.id}/details",
                      data=_form(unregistered, registration_type="regular", **{"loc-LOC-gstin": "27aaaaa0000a1z5"}))
    assert res.status_code == 302
    t = db.session.get(Tenant, unregistered.id)
    loc = t.gstins.filter_by(is_active=True).one()
    assert t.registration_type == RegistrationType.REGULAR and loc.gstin == "27AAAAA0000A1Z5" and loc.state_code == "27"
    assert t.gstins.count() == 1  # nothing was issued from it: corrected in place


def test_a_new_gstin_on_a_location_with_documents_is_a_new_location(client, db, tenant, client_admin, super_admin):
    _login(client, client_admin.email, "ClientSecret123")
    old_bill = _sell(client, db, tenant)
    old_loc = db.session.get(Gstin, old_bill.gstin_id)
    client.get("/auth/logout")
    _login(client, "firm@example.com", "SuperSecret123")
    res = client.post(f"/admin/clients/{tenant.id}/details",
                      data=_form(tenant, **{"loc-LOC-gstin": "29AAAAA0000A1Z5", "loc-LOC-city": "Bengaluru"}))
    assert res.status_code == 302
    db.session.refresh(old_loc)
    new_loc = tenant.gstins.filter_by(is_active=True).one()
    assert not old_loc.is_active and old_loc.gstin == "27AAAAA0000A1Z5"
    assert new_loc.gstin == "29AAAAA0000A1Z5" and new_loc.state_code == "29" and new_loc.is_primary
    assert new_loc.city == "Bengaluru"
    assert db.session.get(POSBill, old_bill.id).gstin.gstin == "27AAAAA0000A1Z5"  # the old bill is untouched
    client.get("/auth/logout")
    _login(client, client_admin.email, "ClientSecret123")
    db.session.expire_all()
    new_bill = _sell(client, db, tenant)
    # On from the new location, numbering carried on - never a repeat of 0001.
    assert new_bill.gstin_id == new_loc.id and new_bill.bill_number.endswith("0002")


def test_a_registered_client_becomes_unregistered(client, db, tenant, client_admin, super_admin):
    _login(client, client_admin.email, "ClientSecret123")
    _sell(client, db, tenant)
    client.get("/auth/logout")
    _login(client, "firm@example.com", "SuperSecret123")
    loc = tenant.gstins.first()
    res = client.post(f"/admin/clients/{tenant.id}/details",
                      data=_form(tenant, registration_type="unregistered", **{"loc-LOC-gstin": loc.gstin}))
    assert res.status_code == 302
    t = db.session.get(Tenant, tenant.id)
    now = t.gstins.filter_by(is_active=True).one()
    assert t.registration_type == RegistrationType.UNREGISTERED and now.gstin is None and now.state_code == "27"
    assert Gstin.query.filter_by(gstin="27AAAAA0000A1Z5", is_active=False).count() == 1


@pytest.mark.parametrize("over, message", [
    ({"loc-LOC-gstin": "27AAAAA0000"}, "isn't a valid 15-character GSTIN"),
    ({"legal_name": "  "}, "legal name can't be empty"),
    ({"loc-LOC-pincode": "12"}, "isn't a 6-digit PIN code"),
    ({"aato": "lots"}, "Turnover must be a number"),
])
def test_bad_details_are_refused_and_nothing_changes(client, db, tenant, super_admin, over, message):
    _login(client, "firm@example.com", "SuperSecret123")
    res = client.post(f"/admin/clients/{tenant.id}/details", data=_form(tenant, **over))
    assert res.status_code == 200 and message in html.unescape(res.get_data(as_text=True))
    assert db.session.get(Tenant, tenant.id).legal_name == "Acme Traders"


def test_a_gstin_of_another_client_is_refused(client, db, tenant, super_admin):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR)
    db.session.add(other)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other.id, gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka",
                         is_primary=True))
    db.session.commit()
    _login(client, "firm@example.com", "SuperSecret123")
    res = client.post(f"/admin/clients/{tenant.id}/details", data=_form(tenant, **{"loc-LOC-gstin": "29BBBBB0000B1Z5"}))
    assert "already on file for another client" in res.get_data(as_text=True)


def test_only_the_console_with_the_power_edits_details(client, db, tenant, client_admin, super_admin):
    from app.models.tenant import BillingCycle

    auditor = User(name="Auditor", email="aud@example.com", role=UserRole.AUDITOR, must_change_password=False,
                   billing_cycle=BillingCycle.MONTHLY)
    auditor.set_password("AuditorSecret123")
    db.session.add(auditor)
    db.session.commit()
    tenant.auditor_id = auditor.id
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get(f"/admin/clients/{tenant.id}/details").status_code in (302, 403)
    settings = client.get("/settings/").get_data(as_text=True)
    assert "Registration &amp; locations" in settings and "27AAAAA0000A1Z5" in settings and "ask ARFA" in settings
    client.get("/auth/logout")
    _login(client, "aud@example.com", "AuditorSecret123")
    assert client.get(f"/admin/clients/{tenant.id}/details").status_code == 200
    from app.utils.entitlements import set_auditor_powers

    set_auditor_powers(auditor, {"edit_client_details": False}, super_admin)
    db.session.commit()
    assert client.get(f"/admin/clients/{tenant.id}/details").status_code == 403
