import io

from app.models.product import Product
from app.models.tenant import Tenant


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def test_settings_page_updates_color_and_print_size(client, db, client_admin, tenant):
    _login(client, client_admin.email, "ClientSecret123")

    resp = client.post(
        "/settings/",
        data={"primary_color": "#ff8800", "default_receipt_format": "2in"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Settings updated" in resp.data

    db.session.refresh(tenant)
    assert tenant.primary_color == "#ff8800"
    assert tenant.default_receipt_format == "2in"


def test_settings_page_accepts_a_logo_upload(client, db, client_admin, tenant):
    _login(client, client_admin.email, "ClientSecret123")

    image = (io.BytesIO(b"fake-png-bytes"), "logo.png")
    resp = client.post(
        "/settings/",
        data={"primary_color": "#1f7a4d", "default_receipt_format": "3in", "logo": image},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200

    db.session.refresh(tenant)
    assert tenant.logo_path is not None
    assert tenant.logo_path.startswith("uploads/logos/")


def test_staff_cannot_reach_settings(client, db, staff):
    _login(client, staff.email, "CashierSecret123")
    resp = client.get("/settings/")
    assert resp.status_code == 403


def test_tenant_branded_login_page_shows_tenant_name(client, db, tenant):
    tenant.login_slug = "acme-traders-test"
    db.session.commit()

    resp = client.get(f"/auth/login/{tenant.login_slug}")
    assert resp.status_code == 200
    assert tenant.legal_name.encode() in resp.data


def test_unknown_login_slug_is_404(client, db):
    resp = client.get("/auth/login/does-not-exist")
    assert resp.status_code == 404


def test_super_admin_can_upload_a_logo_for_an_existing_client(client, db, super_admin, tenant):
    _login(client, super_admin.email, "SuperSecret123")

    assert tenant.logo_path is None
    image = (io.BytesIO(b"fake-png-bytes"), "logo.png")
    resp = client.post(
        f"/admin/clients/{tenant.id}/logo",
        data={"logo": image},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Logo updated" in resp.data

    db.session.refresh(tenant)
    assert tenant.logo_path is not None
    assert tenant.logo_path.startswith("uploads/logos/")


def test_staff_cannot_upload_a_logo_for_a_client(client, db, staff, tenant):
    _login(client, staff.email, "CashierSecret123")
    image = (io.BytesIO(b"fake-png-bytes"), "logo.png")
    resp = client.post(
        f"/admin/clients/{tenant.id}/logo",
        data={"logo": image},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 403


def test_product_image_upload_is_saved_on_the_product(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")

    image = (io.BytesIO(b"fake-png-bytes"), "dish.jpg")
    resp = client.post(
        "/products/new",
        data={
            "name": "Chicken Biriyani",
            "description": "",
            "hsn_or_sac_code": "2106",
            "gst_rate": "5",
            "unit": "plate",
            "default_price": "180",
            "image": image,
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200

    product = Product.query.filter_by(name="Chicken Biriyani").first()
    assert product is not None
    assert product.image_path is not None
    assert product.image_path.startswith("uploads/products/")
