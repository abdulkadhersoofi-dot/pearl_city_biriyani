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


def test_settings_page_updates_branch_stock_grace_qty(client, db, client_admin, tenant):
    _login(client, client_admin.email, "ClientSecret123")

    resp = client.post(
        "/settings/",
        data={
            "primary_color": "#ff8800",
            "background_color": "#202020",
            "font_family": "serif",
            "default_receipt_format": "2in",
            "stock_grace_qty": "25",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(tenant)
    assert tenant.stock_grace_qty == 25


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

    image = (_real_image_bytes(), "logo.png")
    resp = client.post(
        "/settings/",
        data={
            "primary_color": "#1f7a4d",
            "background_color": "#f6f7f5",
            "text_color": "#1f2a24",
            "font_family": "system",
            "default_receipt_format": "3in",
            "logo": image,
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200

    db.session.refresh(tenant)
    assert tenant.logo_image_id is not None


def test_logo_and_product_image_persist_through_a_fresh_browser_session(app, db, client_admin, tenant):
    # The actual bug this whole file is guarding against: a logo/photo
    # uploaded in one session used to vanish on the next sign-in because
    # it only ever lived as a file on local disk, which doesn't survive
    # a container restart or redeploy. Nothing here touches disk, so a
    # brand-new client (no cookies carried over, same as a cleared
    # browser) must still see both images after signing in again.
    first_session = app.test_client()
    _login(first_session, client_admin.email, "ClientSecret123")
    first_session.post(
        "/settings/",
        data={
            "primary_color": "#1f7a4d",
            "background_color": "#f6f7f5",
            "text_color": "#1f2a24",
            "font_family": "system",
            "default_receipt_format": "3in",
            "logo": (_real_image_bytes(), "logo.png"),
        },
        content_type="multipart/form-data",
    )
    first_session.post(
        "/products/new",
        data={
            "name": "Persistent Dish",
            "description": "",
            "hsn_or_sac_code": "2106",
            "gst_rate": "5",
            "unit": "plate",
            "default_price": "120",
            "image": (_real_image_bytes(), "dish.jpg"),
        },
        content_type="multipart/form-data",
    )

    db.session.refresh(tenant)
    logo_id = tenant.logo_image_id
    product = Product.query.filter_by(name="Persistent Dish").first()
    assert logo_id is not None
    assert product is not None and product.image_id is not None

    second_session = app.test_client()
    _login(second_session, client_admin.email, "ClientSecret123")

    logo_resp = second_session.get(f"/media/{logo_id}")
    assert logo_resp.status_code == 200
    assert logo_resp.data

    photo_resp = second_session.get(f"/media/{product.image_id}")
    assert photo_resp.status_code == 200
    assert photo_resp.data

    nav_resp = second_session.get("/pos/")
    assert f"/media/{logo_id}".encode() in nav_resp.data


def test_invoice_pdf_embeds_the_tenant_logo_as_a_data_uri(client, db, tenant, client_admin):
    from datetime import date
    from decimal import Decimal

    from app.invoicing.pdf import render_invoice_pdf
    from app.invoicing.services import InvoiceInput, LineInput, create_invoice
    from app.models.customer import Customer
    from app.models.invoice_series import DocumentType
    from app.models.media import UploadedImage

    image = UploadedImage(content_type="image/jpeg", data=_real_image_bytes().getvalue())
    db.session.add(image)
    db.session.flush()
    tenant.logo_image_id = image.id

    customer = Customer(tenant_id=tenant.id, name="Walk-in", state_code="27", state_name="Maharashtra")
    db.session.add(customer)
    db.session.flush()
    invoice = create_invoice(
        tenant, client_admin,
        InvoiceInput(
            gstin_id=tenant.gstins.first().id, document_type=DocumentType.TAX_INVOICE, customer_id=customer.id,
            place_of_supply_state_code="27", invoice_date=date.today(), notes=None,
            lines=[LineInput(description="Biriyani", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("150"), discount_percent=Decimal("0"), gst_rate=Decimal("5"))],
        ),
    )
    db.session.commit()

    pdf_bytes = render_invoice_pdf(invoice)
    assert pdf_bytes.startswith(b"%PDF")


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

    assert tenant.logo_image_id is None
    image = (_real_image_bytes(), "logo.png")
    resp = client.post(
        f"/admin/clients/{tenant.id}/logo",
        data={"logo": image},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Logo updated" in resp.data

    db.session.refresh(tenant)
    assert tenant.logo_image_id is not None


def test_replacing_a_logo_deletes_the_old_one(client, db, super_admin, tenant):
    # Regression: this needs the new image referenced (and flushed)
    # before the old row is deleted, or deleting a still-referenced row
    # trips the foreign key constraint.
    _login(client, super_admin.email, "SuperSecret123")

    for _ in range(2):
        resp = client.post(
            f"/admin/clients/{tenant.id}/logo",
            data={"logo": (_real_image_bytes(), "logo.png")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 302

    from app.models.media import UploadedImage

    db.session.refresh(tenant)
    assert tenant.logo_image_id is not None
    assert UploadedImage.query.count() == 1


def test_staff_cannot_upload_a_logo_for_a_client(client, db, staff, tenant):
    _login(client, staff.email, "CashierSecret123")
    image = (_real_image_bytes(), "logo.png")
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
    assert product.image_id is not None


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
    from app.models.media import UploadedImage

    record = UploadedImage.query.get(product.image_id)
    assert record is not None
    with Image.open(io.BytesIO(record.data)) as saved:
        assert saved.size == (512, 512)


def test_replacing_a_product_image_deletes_the_old_one(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    client.post(
        "/products/new",
        data={
            "name": "Mezze Platter",
            "description": "",
            "hsn_or_sac_code": "2106",
            "gst_rate": "5",
            "unit": "plate",
            "default_price": "150",
            "image": (_real_image_bytes(), "first.jpg"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    product = Product.query.filter_by(name="Mezze Platter").first()
    assert product is not None

    client.post(
        f"/products/{product.id}/edit",
        data={
            "name": "Mezze Platter",
            "description": "",
            "hsn_or_sac_code": "2106",
            "gst_rate": "5",
            "unit": "plate",
            "default_price": "150",
            "image": (_real_image_bytes(color=(10, 200, 10)), "second.jpg"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    from app.models.media import UploadedImage

    db.session.refresh(product)
    assert product.image_id is not None
    assert UploadedImage.query.count() == 1


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
