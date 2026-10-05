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
