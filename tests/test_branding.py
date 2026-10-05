import io

from PIL import Image

from app.models.product import Product
from app.models.tenant import Tenant


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _real_image_bytes(width=800, height=400, color=(220, 40, 40)):
    """A real, decodable image - product uploads are now validated and
    center-cropped server-side (app.utils.uploads), so a bare byte
    string no longer passes."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    buf.seek(0)
    return buf


def test_settings_page_updates_theme_and_print_size(client, db, client_admin, tenant):
    _login(client, client_admin.email, "ClientSecret123")

    resp = client.post(
        "/settings/",
        data={
            "primary_color": "#ff8800",
            "background_color": "#202020",
            "font_family": "serif",
            "default_receipt_format": "2in",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Settings updated" in resp.data

    db.session.refresh(tenant)
    assert tenant.primary_color == "#ff8800"
    assert tenant.background_color == "#202020"
    assert tenant.font_family == "serif"
    assert tenant.default_receipt_format == "2in"


def test_settings_page_rejects_an_unknown_font_choice(client, db, client_admin, tenant):
    _login(client, client_admin.email, "ClientSecret123")

    resp = client.post(
        "/settings/",
        data={
            "primary_color": "#ff8800",
            "background_color": "#202020",
            "font_family": "comic-sans",
            "default_receipt_format": "2in",
        },
    )
    assert resp.status_code == 200
    db.session.refresh(tenant)
    assert tenant.font_family == "system"


def test_tenant_theme_is_applied_as_css_on_signed_in_pages(client, db, client_admin, tenant):
    tenant.primary_color = "#ff00aa"
    tenant.background_color = "#111111"
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")

    resp = client.get("/pos/")
    assert resp.status_code == 200
    assert b"#ff00aa" in resp.data
    assert b"#111111" in resp.data


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

    image = (_real_image_bytes(), "dish.jpg")
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


def test_product_image_is_cropped_to_a_square_on_upload(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")

    # 800x400 - a wide, clearly non-square original.
    image = (_real_image_bytes(width=800, height=400), "wide-dish.jpg")
    client.post(
        "/products/new",
        data={
            "name": "Mutton Rolls",
            "description": "",
            "hsn_or_sac_code": "2106",
            "gst_rate": "5",
            "unit": "plate",
            "default_price": "90",
            "image": image,
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    product = Product.query.filter_by(name="Mutton Rolls").first()
    assert product is not None
    from flask import current_app

    saved_path = f"{current_app.root_path}/static/{product.image_path}"
    with Image.open(saved_path) as saved:
        assert saved.size == (512, 512)


def test_a_non_image_file_is_rejected_for_a_product_photo(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")

    image = (io.BytesIO(b"not actually an image"), "dish.png")
    resp = client.post(
        "/products/new",
        data={
            "name": "Fake Photo Item",
            "description": "",
            "hsn_or_sac_code": "2106",
            "gst_rate": "5",
            "unit": "plate",
            "default_price": "90",
            "image": image,
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert Product.query.filter_by(name="Fake Photo Item").first() is None
