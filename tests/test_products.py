from decimal import Decimal

from app.models.product import Product


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def test_zero_rated_gst_product_can_be_created(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")

    resp = client.post(
        "/products/new",
        data={
            "name": "Idli",
            "description": "",
            "hsn_or_sac_code": "2106",
            "gst_rate": "0",
            "unit": "plate",
            "default_price": "30",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"added" in resp.data

    product = Product.query.filter_by(name="Idli").first()
    assert product is not None
    assert product.gst_rate == Decimal("0")


def test_free_item_with_zero_default_price_can_be_created(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")

    resp = client.post(
        "/products/new",
        data={
            "name": "Complimentary Papad",
            "description": "",
            "hsn_or_sac_code": "2106",
            "gst_rate": "5",
            "unit": "pcs",
            "default_price": "0",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    product = Product.query.filter_by(name="Complimentary Papad").first()
    assert product is not None
    assert product.default_price == Decimal("0")


def test_gst_rate_accepts_every_published_slab(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")

    for rate in ["0", "0.1", "0.25", "1", "1.5", "3", "5", "6", "7.5", "12", "18", "28", "40"]:
        resp = client.post(
            "/products/new",
            data={
                "name": f"Item at {rate}",
                "description": "",
                "hsn_or_sac_code": "2106",
                "gst_rate": rate,
                "unit": "pcs",
                "default_price": "10",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        product = Product.query.filter_by(name=f"Item at {rate}").first()
        assert product is not None, f"rate {rate} was not accepted"
        assert product.gst_rate == Decimal(rate)


def test_gst_rate_rejects_a_value_outside_the_published_slabs(client, db, client_admin):
    # The GST rate is a dropdown of the GST portal's own slabs now, not a
    # free-typed number - a rate that doesn't exist (e.g. a typo'd "2.8"
    # meant to be "28") must not be accepted.
    _login(client, client_admin.email, "ClientSecret123")

    resp = client.post(
        "/products/new",
        data={
            "name": "Bad Rate Item",
            "description": "",
            "hsn_or_sac_code": "2106",
            "gst_rate": "2.8",
            "unit": "pcs",
            "default_price": "10",
        },
    )
    assert resp.status_code == 200
    assert Product.query.filter_by(name="Bad Rate Item").first() is None


def test_gst_rate_dropdown_is_rendered_with_all_slabs(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/products/new")
    assert resp.status_code == 200
    for rate in ["0.1", "0.25", "1.5", "7.5", "40"]:
        assert f'value="{rate}"'.encode() in resp.data
