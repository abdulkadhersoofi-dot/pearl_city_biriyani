"""Packages and powers, stage 3: what the client sees. Settings > Modules
shows their package (Included locked, their choices as switches, the rest
"Not in your package" with "Ask ARFA to add"); requests reach the Ultra
Admin's bell and client page; locked settings show their value with a
lock and refuse changes (unless the Ultra Admin is acting as the client);
a module taken away while it holds records stays readable, and GST
returns keep counting those records."""

from datetime import date
from decimal import Decimal

import pytest

from app.inventory import ledger
from app.models.audit_log import AuditLog
from app.models.package import ModuleRequest, Package
from app.models.purchase import ItcEligibility, Supplier
from app.models.tenant import BillingCycle, RegistrationType, Tenant
from app.purchases import services as ps
from app.reports.gstr import gstr3b_data
from app.utils.entitlements import CHOICE, INCLUDED, OFF, READ_ONLY, assign_package, module_mode, set_module_exception

D = Decimal


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _pack(db, name="Shop pack", modules=None, settings=None):
    p = Package(name=name, modules=modules or {}, unlisted_modules=OFF, settings=settings or {}, limits={})
    db.session.add(p)
    db.session.commit()
    return p


# --- Settings > Modules ----------------------------------------------------------------------------

def test_modules_page_shows_the_package(client, db, tenant, client_admin, super_admin):
    assign_package(tenant, _pack(db, modules={"inventory": INCLUDED, "loyalty": CHOICE}), super_admin)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    html = client.get("/settings/features").get_data(as_text=True)
    mine, outside = html.split("Not in your package", 1)
    assert 'name="feature_loyalty"' in mine and 'name="feature_inventory"' not in mine
    assert "Inventory" in mine and "Included" in mine
    assert "Purchases" in outside and "Ask ARFA to add" in outside and 'name="feature_purchases"' not in html


def test_without_a_package_the_modules_page_is_as_before(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    html = client.get("/settings/features").get_data(as_text=True)
    assert "Not in your package" not in html and 'name="feature_purchases"' in html


def test_ask_arfa_to_add_reaches_the_ultra_admin(client, db, tenant, client_admin, super_admin):
    assign_package(tenant, _pack(db, modules={"inventory": INCLUDED}), super_admin)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/settings/features/ask/purchases")
    client.post("/settings/features/ask/purchases")            # asking twice keeps one request
    assert client.post("/settings/features/ask/inventory").status_code == 404   # already included
    assert ModuleRequest.query.filter_by(tenant_id=tenant.id).count() == 1
    assert "Asked " in client.get("/settings/features").get_data(as_text=True)
    client.get("/auth/logout")
    _login(client, super_admin.email, "SuperSecret123")
    bell = client.get("/needs-you").get_data(as_text=True)
    assert f"{tenant.display_name} asks for Purchases" in bell
    page = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    assert "asked for:" in page and "Add as their choice" in page
    req = ModuleRequest.query.one()
    client.post(f"/admin/clients/{tenant.id}/requests/{req.id}", data={"action": "choice"})
    db.session.expire_all()
    assert req.status == "added" and tenant.has_feature("purchases")
    assert "Nothing needs you" in client.get("/needs-you").get_data(as_text=True)
    assert AuditLog.query.filter_by(action="module_request_resolved").count() == 1


def test_a_request_can_be_declined_and_stays_with_its_client(client, db, tenant, client_admin, super_admin):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.commit()
    assign_package(tenant, _pack(db), super_admin)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/settings/features/ask/loyalty")
    client.get("/auth/logout")
    _login(client, super_admin.email, "SuperSecret123")
    req = ModuleRequest.query.one()
    assert req.tenant_id == tenant.id
    # The request can't be reached through another client's page.
    assert client.post(f"/admin/clients/{other.id}/requests/{req.id}", data={"action": "inc"}).status_code == 404
    client.post(f"/admin/clients/{tenant.id}/requests/{req.id}", data={"action": "decline"})
    db.session.expire_all()
    assert req.status == "declined" and not tenant.has_feature("loyalty") and not other.has_feature("loyalty")


# --- locked settings ---------------------------------------------------------------------------------

def test_locked_settings_show_a_lock_and_refuse_changes(client, db, tenant, client_admin, super_admin):
    tenant.aato = D("1000000")
    db.session.commit()
    assign_package(tenant, _pack(db, settings={"aato": "arfa", "business_type": "arfa", "counter": "arfa",
                                               "security": "arfa", "branding": "arfa"}), super_admin)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    html = client.get("/settings/").get_data(as_text=True)
    assert "set by ARFA as part of your package" in html and "Recommended modules" not in html
    client.post("/settings/", data={"section": "business", "aato": "99999999", "business_type": "pharmacy"})
    db.session.expire_all()
    assert tenant.aato == D("1000000") and tenant.business_type.value != "pharmacy"
    assert client.post("/settings/counter", data={"pos_payment_modes": ["cash"]}).status_code == 403
    assert client.post("/settings/security", data={"idle_minutes": "15"}).status_code == 403
    assert client.post("/settings/appearance", data={"primary_color": "#000000"}).status_code == 403
    counter = client.get("/settings/counter").get_data(as_text=True)
    assert "lock-fieldset" in counter and "disabled" in counter


def test_the_ultra_admin_acting_as_the_client_can_change_a_locked_setting(client, db, tenant, client_admin, super_admin):
    assign_package(tenant, _pack(db, settings={"aato": "arfa"}), super_admin)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    client.post(f"/admin/clients/{tenant.id}/users/{client_admin.id}/act-as")
    client.post("/settings/", data={"section": "business", "aato": "2500000", "business_type": tenant.business_type.value})
    db.session.expire_all()
    assert tenant.aato == D("2500000")


# --- read-only modules and GST ---------------------------------------------------------------------

@pytest.fixture()
def bought(db, tenant, client_admin):
    for key in ("inventory", "purchases"):
        tenant.set_feature(key, True)
    supplier = Supplier(tenant_id=tenant.id, name="Sri Balaji Distributors", gstin="27BBBBB1111B1Z5",
                        state_code="27", state_name="Maharashtra")
    db.session.add(supplier)
    db.session.commit()
    bill = ps.create_purchase_bill(tenant, client_admin, ps.PurchaseInput(
        supplier_id=supplier.id, gstin_id=tenant.gstins.first().id, supplier_invoice_no="SB/1",
        supplier_invoice_date=date.today(), location_id=ledger.default_location(tenant.id).id,
        lines=[ps.PurchaseLineInput(product_id=None, description="Misc", hsn_or_sac_code="9999", qty=D("10"),
                                    unit="BAG", rate=D("1000"), gst_rate=D("5"),
                                    itc_eligibility=ItcEligibility.ELIGIBLE)],
        bill_date=None, is_rcm=False, other_charges=D("0")))
    db.session.commit()
    return bill


def test_a_module_taken_away_with_records_turns_read_only(client, db, tenant, client_admin, super_admin, bought):
    period = date.today().strftime("%Y-%m")
    gstin = tenant.gstins.first()
    before = gstr3b_data(tenant, gstin, period)["itc"]
    assign_package(tenant, _pack(db, modules={"inventory": INCLUDED}), super_admin)   # purchases Off
    db.session.commit()
    assert module_mode(tenant, "purchases") == READ_ONLY and not tenant.has_feature("purchases")
    # GST returns still count the purchase bill already entered.
    assert gstr3b_data(tenant, gstin, period)["itc"] == before
    _login(client, client_admin.email, "ClientSecret123")
    page = client.get("/purchases/")
    assert page.status_code == 200 and "no longer in your package" in page.get_data(as_text=True)
    assert client.get(f"/purchases/{bought.id}").status_code == 200
    assert client.post("/purchases/new", data={}).status_code == 403
    modules = client.get("/settings/features").get_data(as_text=True)
    assert "Read-only: your records still open." in modules and 'href="/purchases/"' in modules
    # A module with no records simply switches off.
    assert module_mode(tenant, "loyalty") == OFF and client.get("/loyalty/").status_code == 404


def test_giving_the_module_back_ends_read_only(db, tenant, super_admin, bought):
    assign_package(tenant, _pack(db, modules={"inventory": INCLUDED}), super_admin)
    db.session.commit()
    assert "purchases" in tenant.package_grant.read_only_modules
    set_module_exception(tenant, "purchases", INCLUDED, super_admin)
    db.session.commit()
    assert tenant.package_grant.read_only_modules == [] and tenant.has_feature("purchases")
