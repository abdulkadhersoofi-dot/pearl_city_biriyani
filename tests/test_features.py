"""Business type + per-client feature flags (app.utils.features)."""

from app.models.audit_log import AuditLog
from app.models.tenant import BusinessType, RegistrationType, Tenant
from app.utils.features import AVAILABLE_FEATURES, FEATURES, preset_features


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


_SETTINGS_BASE = {
    "primary_color": "#1f7a4d",
    "background_color": "#f6f7f5",
    "text_color": "#1f2a24",
    "font_family": "system",
    "default_receipt_format": "3in",
}


def test_every_business_type_has_a_preset_using_only_known_flags():
    for business_type in BusinessType:
        preset = preset_features(business_type)
        assert set(preset) == set(FEATURES)


def test_presets_differ_by_business_type():
    assert preset_features(BusinessType.PHARMACY)["batch_expiry"] is True
    assert preset_features(BusinessType.RESTAURANT)["batch_expiry"] is False
    assert preset_features(BusinessType.RESTAURANT)["categories"] is True
    assert not any(preset_features(BusinessType.OTHER).values())


def test_settings_only_offers_modules_that_are_built():
    # Every module is built since Phase 9 (business-type modules).
    assert [f.key for f in AVAILABLE_FEATURES] == ["categories", "inventory", "purchases", "expenses", "variants", "batch_expiry", "serial_imei", "margin_scheme", "receivables", "payables", "sales_documents", "recurring_invoices", "cash_bank_book", "business_reports", "weighing", "restaurant_mode", "job_cards", "pharmacy", "electronics", "jewellery", "wholesale", "credit_sales", "multi_price_list", "favourites", "day_close", "einvoice", "eway_bill", "loyalty", "offline_pos", "cashier_pin", "daily_summary"]
    assert len(AVAILABLE_FEATURES) == len(FEATURES)


def test_a_tenant_with_no_flags_has_every_feature_off(tenant):
    assert tenant.features == {} or not any(tenant.features.values())
    assert tenant.has_feature("categories") is False


def test_onboarding_presets_modules_from_the_business_type(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(
        "/admin/clients/new",
        data={
            "legal_name": "Apollo Medicals",
            "registration_type": "regular",
            "state_code": "33",
            "business_type": "pharmacy",
            "aato": "12000000",
            "admin_name": "Pharma Admin",
            "admin_email": "pharma@example.com",
            "admin_phone": "",
            "admin_password": "InitialPass123",
            "admin_confirm_password": "InitialPass123",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    t = Tenant.query.filter_by(legal_name="Apollo Medicals").one()
    assert t.business_type == BusinessType.PHARMACY
    assert t.has_feature("batch_expiry") and t.has_feature("inventory")
    assert not t.has_feature("restaurant_mode")
    assert t.default_price_includes_tax is True
    assert str(t.aato) == "12000000.00"


def test_onboarding_without_a_business_type_switches_nothing_on(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    client.post(
        "/admin/clients/new",
        data={
            "legal_name": "Plain Co",
            "registration_type": "regular",
            "state_code": "27",
            "admin_name": "Plain Admin",
            "admin_email": "plain@example.com",
            "admin_password": "InitialPass123",
            "admin_confirm_password": "InitialPass123",
        },
    )
    t = Tenant.query.filter_by(legal_name="Plain Co").one()
    assert t.business_type == BusinessType.OTHER
    assert not any(t.features.values())


def test_settings_switches_a_module_on_and_audits_it(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post("/settings/", data={**_SETTINGS_BASE, "business_type": "retail", "feature_categories": "y"}, follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(tenant)
    assert tenant.has_feature("categories")
    assert tenant.business_type == BusinessType.RETAIL
    log = AuditLog.query.filter_by(action="tenant_features_changed", tenant_id=tenant.id).one()
    assert log.details["after"]["categories"] is True


def test_changing_business_type_alone_keeps_existing_flags(client, db, tenant, client_admin):
    tenant.set_feature("categories", True)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/settings/", data={**_SETTINGS_BASE, "business_type": "pharmacy", "feature_categories": "y"})
    db.session.refresh(tenant)
    assert tenant.business_type == BusinessType.PHARMACY
    assert tenant.has_feature("categories")
    assert not tenant.has_feature("batch_expiry")


def test_settings_can_reset_modules_to_the_business_type_preset(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/settings/", data={**_SETTINGS_BASE, "business_type": "pharmacy", "apply_business_preset": "y"})
    db.session.refresh(tenant)
    assert tenant.features == preset_features(BusinessType.PHARMACY)
    assert tenant.default_price_includes_tax is True


def test_a_switched_off_module_is_invisible_and_its_routes_404(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get("/products/categories").status_code == 404
    assert client.post("/products/categories/new", data={"name": "X"}).status_code == 404
    assert b"Manage categories" not in client.get("/products/new").data

    tenant.set_feature("categories", True)
    db.session.commit()
    assert client.get("/products/categories").status_code == 200


def test_feature_flags_are_per_tenant(client, db, tenant, client_admin):
    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR)
    other.set_feature("categories", True)
    db.session.add(other)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get("/products/categories").status_code == 404
