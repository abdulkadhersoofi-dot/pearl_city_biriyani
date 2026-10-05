from decimal import Decimal

from app.models.pos_bill import PaymentMode
from app.models.tenant import RegistrationType
from app.pos.services import CartInput, CartLineInput, checkout


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def test_reports_index_shows_a_gating_notice_for_non_regular_tenants(client, db, tenant, client_admin):
    tenant.registration_type = RegistrationType.UNREGISTERED
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/reports/")

    assert resp.status_code == 200
    assert b"doesn&#39;t file these returns" in resp.data or b"doesn't file these returns" in resp.data


def test_gstr1_view_redirects_for_non_regular_tenant(client, db, tenant, client_admin):
    tenant.registration_type = RegistrationType.COMPOSITION
    db.session.commit()
    gstin = tenant.gstins.first()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get(f"/reports/gstr1?gstin_id={gstin.id}&period=2026-10", follow_redirects=True)

    assert resp.status_code == 200
    assert b"/reports/" in resp.request.path.encode() or resp.request.path == "/reports/"


def test_gstr1_and_gstr3b_views_render_for_a_regular_tenant(client, db, tenant, client_admin):
    gstin = tenant.gstins.first()
    checkout(
        tenant, client_admin,
        CartInput(lines=[CartLineInput(description="Chicken Biriyani", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("150"), gst_rate=Decimal("5"), unit="plate")]),
        PaymentMode.CASH,
    )
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get(f"/reports/gstr1?gstin_id={gstin.id}&period=2026-10")
    assert resp.status_code == 200
    assert b"GSTR-1 working paper" in resp.data

    resp = client.get(f"/reports/gstr3b?gstin_id={gstin.id}&period=2026-10")
    assert resp.status_code == 200
    assert b"GSTR-3B working paper" in resp.data


def test_gstr1_exports_download_with_correct_content_types(client, db, tenant, client_admin):
    gstin = tenant.gstins.first()
    _login(client, client_admin.email, "ClientSecret123")

    resp = client.get(f"/reports/gstr1.xlsx?gstin_id={gstin.id}&period=2026-10")
    assert resp.status_code == 200
    assert "spreadsheetml" in resp.content_type

    resp = client.get(f"/reports/gstr1.pdf?gstin_id={gstin.id}&period=2026-10")
    assert resp.status_code == 200
    assert resp.content_type == "application/pdf"

    resp = client.get(f"/reports/gstr1.json?gstin_id={gstin.id}&period=2026-10")
    assert resp.status_code == 200
    assert resp.content_type == "application/json"


def test_reports_are_tenant_isolated(client, db, tenant, client_admin):
    from app.models.tenant import Gstin, Tenant
    from app.models.user import User, UserRole

    other_tenant = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR)
    db.session.add(other_tenant)
    db.session.flush()
    other_gstin = Gstin(tenant_id=other_tenant.id, gstin="29CCCCC0000C1Z5", state_code="29", state_name="Karnataka", is_primary=True)
    db.session.add(other_gstin)
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get(f"/reports/gstr1?gstin_id={other_gstin.id}&period=2026-10")
    assert resp.status_code == 404
