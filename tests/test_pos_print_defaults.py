from decimal import Decimal

from app.models.pos_bill import PaymentMode
from app.pos.services import CartInput, CartLineInput, checkout


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _cart():
    return CartInput(
        lines=[
            CartLineInput(
                description="Chicken Biriyani", hsn_or_sac_code="996331",
                qty=Decimal("1"), rate=Decimal("150"), gst_rate=Decimal("5"), unit="plate",
            )
        ]
    )


def test_day_book_has_no_paper_size_picker(client, db, tenant, client_admin):
    checkout(tenant, client_admin, _cart(), PaymentMode.CASH)
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/pos/bills")

    assert resp.status_code == 200
    assert b"Reprint" in resp.data
    assert b"2 inch roll" not in resp.data
    assert b"3 inch roll" not in resp.data
    assert b"A4 copy" not in resp.data
    assert f"format={tenant.default_receipt_format}".encode() in resp.data


def test_pos_terminal_printed_dialog_has_no_paper_size_picker(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/pos/")

    assert resp.status_code == 200
    assert b"2 inch roll" not in resp.data
    assert b"3 inch roll" not in resp.data
    assert b"A4 copy" not in resp.data
    assert b"Reprint" in resp.data


def test_receipt_page_has_a_single_reprint_button(client, db, tenant, client_admin):
    bill = checkout(tenant, client_admin, _cart(), PaymentMode.CASH)
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get(f"/pos/bills/{bill.id}/receipt")

    assert resp.status_code == 200
    assert b"2 inch roll" not in resp.data
    assert b"3 inch roll" not in resp.data
    assert b"A4 copy" not in resp.data
    assert b"Reprint" in resp.data
    assert f"format={tenant.default_receipt_format}".encode() in resp.data


def test_settings_page_shows_invoice_size_fixed_and_pos_size_editable(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/settings/")

    assert resp.status_code == 200
    assert b"Invoice print size" in resp.data
    assert b"POS bill print size" in resp.data
    assert b'value="A4" disabled' in resp.data
