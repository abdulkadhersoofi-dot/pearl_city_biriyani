from datetime import date, datetime, timezone
from decimal import Decimal

from app.invoicing.services import InvoiceInput, LineInput, create_invoice
from app.models.customer import Customer
from app.models.invoice_series import DocumentType


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _make_invoice(db, tenant, client_admin):
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
    return invoice


def test_invoice_has_no_irn_by_default(db, tenant, client_admin):
    invoice = _make_invoice(db, tenant, client_admin)
    assert invoice.irn is None
    assert invoice.qr_code_data is None


def test_irn_block_renders_once_populated(client, db, tenant, client_admin):
    invoice = _make_invoice(db, tenant, client_admin)
    invoice.irn = "1" * 64
    invoice.irn_ack_number = "112010000000"
    invoice.irn_ack_date = datetime.now(timezone.utc)
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get(f"/invoices/{invoice.id}")

    assert resp.status_code == 200
    assert invoice.irn.encode() in resp.data
    assert b"112010000000" in resp.data


def test_irn_block_hidden_when_not_set(client, db, tenant, client_admin):
    invoice = _make_invoice(db, tenant, client_admin)

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get(f"/invoices/{invoice.id}")

    assert resp.status_code == 200
    assert b"e-Invoice" not in resp.data
