"""Settings in sections (redesign stage 2): each section saves only its
own fields, module changes are audit-logged, set-up links appear only for
modules that are on, and one client's save never touches another's."""

import pytest

from app.models.audit_log import AuditLog
from app.models.tenant import BusinessType, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.utils.features import preset_features

SECTION_URLS = ["/settings/", "/settings/appearance", "/settings/features", "/settings/counter",
                "/settings/documents", "/settings/security", "/admin/subscriptions/mine"]


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _admin(client):
    _login(client, "admin@acme.example.com", "ClientSecret123")


@pytest.fixture()
def other_tenant(db, super_admin):
    t = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id)
    db.session.add(t)
    db.session.flush()
    db.session.add(Gstin(tenant_id=t.id, gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka",
                         is_primary=True))
    other = User(tenant_id=t.id, name="Other Admin", email="admin@other.example.com", role=UserRole.CLIENT_ADMIN,
                 must_change_password=False)
    other.set_password("OtherSecret123")
    db.session.add(other)
    t.set_feature("inventory", True)
    t.primary_color = "#123456"
    db.session.commit()
    return t


def test_every_section_opens_in_the_settings_frame(client, db, tenant, client_admin):
    _admin(client)
    for url in SECTION_URLS:
        resp = client.get(url)
        assert resp.status_code == 200, url
        html = resp.get_data(as_text=True)
        assert 'class="settings-nav"' in html, url
        for label in ("Business", "Appearance", "Modules", "Counter", "Documents &amp; printing",
                      "Sign-in &amp; security", "Subscription"):
            assert label in html, (url, label)


def test_branch_login_cannot_open_any_section(client, db, tenant, staff):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    for url in SECTION_URLS[:6]:
        assert client.get(url).status_code == 403, url
        assert client.post(url, data={"section": "business"}).status_code == 403, url


def test_business_section_saves_only_business_fields(client, db, tenant, client_admin):
    tenant.set_feature("inventory", True)
    tenant.primary_color = "#aa0000"
    db.session.commit()
    _admin(client)
    resp = client.post("/settings/", data={"section": "business", "business_type": "pharmacy", "aato": "1200000",
                                           "default_price_includes_tax": "y"})
    assert resp.status_code == 302
    db.session.refresh(tenant)
    assert tenant.business_type == BusinessType.PHARMACY
    assert str(tenant.aato) == "1200000.00"
    assert tenant.default_price_includes_tax is True
    # Untouched: modules and colours belong to other sections.
    assert tenant.has_feature("inventory") and tenant.primary_color == "#aa0000"
    assert AuditLog.query.filter_by(action="settings_saved", tenant_id=tenant.id).one().details == {"section": "business"}
    assert not AuditLog.query.filter_by(action="tenant_features_changed").count()
    assert (tenant.first_run or {}).get("profile_saved")


def test_business_section_can_apply_the_recommended_modules(client, db, tenant, client_admin):
    _admin(client)
    client.post("/settings/", data={"section": "business", "business_type": "pharmacy", "apply_business_preset": "y"})
    db.session.refresh(tenant)
    assert tenant.features == preset_features(BusinessType.PHARMACY)
    assert AuditLog.query.filter_by(action="tenant_features_changed", tenant_id=tenant.id).count() == 1


def test_modules_section_switches_modules_and_logs_the_change(client, db, tenant, client_admin):
    tenant.set_feature("expenses", True)
    tenant.font_family = "serif"
    db.session.commit()
    _admin(client)
    resp = client.post("/settings/features", data={"feature_inventory": "y", "feature_purchases": "y"},
                       follow_redirects=True)
    assert "Modules saved - 3 changed." in resp.get_data(as_text=True)
    db.session.refresh(tenant)
    assert tenant.has_feature("inventory") and tenant.has_feature("purchases")
    assert not tenant.has_feature("expenses")  # unticked = off
    assert tenant.font_family == "serif"
    log = AuditLog.query.filter_by(action="tenant_features_changed", tenant_id=tenant.id).one()
    assert log.details["before"].get("expenses") is True and log.details["after"]["inventory"] is True


def test_set_up_links_only_for_modules_that_are_on(client, db, tenant, client_admin):
    _admin(client)
    html = client.get("/settings/features").get_data(as_text=True)
    assert "/inventory/locations" not in html and "/pharmacy/register" not in html
    tenant.set_feature("inventory", True)
    tenant.set_feature("pharmacy", True)
    db.session.commit()
    html = client.get("/settings/features").get_data(as_text=True)
    assert "/inventory/locations" in html and "/pharmacy/register" in html
    # Expiry's screen also needs inventory: on its own it gets no link.
    tenant.set_feature("inventory", False)
    tenant.set_feature("batch_expiry", True)
    db.session.commit()
    html = client.get("/settings/features").get_data(as_text=True)
    assert "/inventory/expiry" not in html
    assert client.get("/inventory/expiry").status_code == 404


def test_counter_section_keeps_one_payment_mode_and_checks_the_upi_id(client, db, tenant, client_admin):
    _admin(client)
    before = list(tenant.pos_payment_modes or [])
    resp = client.post("/settings/counter", data={"upi_vpa": ""}, follow_redirects=True)
    assert "Keep at least one payment mode switched on." in resp.get_data(as_text=True)
    db.session.refresh(tenant)
    assert list(tenant.pos_payment_modes or []) == before
    resp = client.post("/settings/counter", data={"pos_payment_modes": ["cash"], "upi_vpa": "bad id"})
    assert resp.status_code == 200 and b"Enter a UPI ID" in resp.data
    client.post("/settings/counter", data={"pos_payment_modes": ["upi", "cash"], "upi_vpa": "acme@okicici"})
    db.session.refresh(tenant)
    assert tenant.pos_payment_modes == ["cash", "upi"] and tenant.upi_vpa == "acme@okicici"


def test_documents_section_ignores_the_margin_wording_while_that_module_is_off(client, db, tenant, client_admin):
    _admin(client)
    client.post("/settings/documents", data={"default_receipt_format": "2in", "margin_scheme_declaration": "Ours"})
    db.session.refresh(tenant)
    assert tenant.default_receipt_format == "2in" and tenant.margin_scheme_declaration is None
    tenant.set_feature("margin_scheme", True)
    db.session.commit()
    assert b"margin_scheme_declaration" in client.get("/settings/documents").data
    client.post("/settings/documents", data={"default_receipt_format": "3in", "margin_scheme_declaration": "Ours"})
    db.session.refresh(tenant)
    assert tenant.margin_scheme_declaration == "Ours"


def test_appearance_section_rejects_a_bad_colour_and_saves_a_good_one(client, db, tenant, client_admin):
    _admin(client)
    resp = client.post("/settings/appearance", data={"primary_color": "red", "background_color": "#ffffff",
                                                      "text_color": "#111111", "font_family": "system"})
    assert resp.status_code == 200 and b"Enter a valid colour" in resp.data
    client.post("/settings/appearance", data={"primary_color": "#8e24aa", "background_color": "#ffffff",
                                               "text_color": "#111111", "font_family": "rounded"})
    db.session.refresh(tenant)
    assert (tenant.primary_color, tenant.font_family) == ("#8e24aa", "rounded")


def test_a_section_save_never_touches_another_client(client, db, tenant, client_admin, other_tenant):
    _admin(client)
    client.post("/settings/features", data={})
    client.post("/settings/appearance", data={"primary_color": "#8e24aa", "background_color": "#ffffff",
                                               "text_color": "#111111", "font_family": "system"})
    db.session.refresh(other_tenant)
    assert other_tenant.has_feature("inventory") and other_tenant.primary_color == "#123456"


def test_sections_are_reachable_from_search(client, db, tenant, client_admin):
    _admin(client)
    html = client.get("/invoices/").get_data(as_text=True)
    for url in ("/settings/appearance", "/settings/features", "/settings/counter", "/settings/documents",
                "/settings/security", "/admin/subscriptions/mine"):
        assert f'"url": "{url}"' in html, url
