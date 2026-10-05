from datetime import date
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


def test_super_admin_can_drill_into_a_client_invoice(client, db, super_admin, tenant, client_admin):
    invoice = _make_invoice(db, tenant, client_admin)

    _login(client, super_admin.email, "SuperSecret123")
    resp = client.get(f"/admin/clients/{tenant.id}/invoices/{invoice.id}")

    assert resp.status_code == 200
    assert invoice.invoice_number.encode() in resp.data
    # Read-only: no void/duplicate/note-creation actions on the admin view.
    assert b"Void invoice" not in resp.data
    assert b"Credit note" not in resp.data


def test_super_admin_can_download_a_client_invoice_pdf(client, db, super_admin, tenant, client_admin):
    invoice = _make_invoice(db, tenant, client_admin)

    _login(client, super_admin.email, "SuperSecret123")
    resp = client.get(f"/admin/clients/{tenant.id}/invoices/{invoice.id}/pdf")

    assert resp.status_code == 200
    assert resp.content_type == "application/pdf"


def test_client_admin_cannot_use_the_admin_drilldown_route(client, db, tenant, client_admin):
    invoice = _make_invoice(db, tenant, client_admin)

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get(f"/admin/clients/{tenant.id}/invoices/{invoice.id}")

    assert resp.status_code == 403


def test_admin_invoice_drilldown_is_tenant_isolated(client, db, super_admin, tenant, client_admin):
    from app.models.tenant import Gstin, RegistrationType, Tenant
    from app.models.user import User, UserRole

    other_tenant = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR)
    db.session.add(other_tenant)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other_tenant.id, gstin="29CCCCC0000C1Z5", state_code="29", state_name="Karnataka", is_primary=True))
    other_admin = User(tenant_id=other_tenant.id, name="Other Admin", email="other@example.com", role=UserRole.CLIENT_ADMIN, must_change_password=False)
    other_admin.set_password("OtherSecret123")
    db.session.add(other_admin)
    db.session.commit()
    other_invoice = _make_invoice(db, other_tenant, other_admin)

    _login(client, super_admin.email, "SuperSecret123")
    # Asking for the other tenant's invoice under THIS tenant's id must 404.
    resp = client.get(f"/admin/clients/{tenant.id}/invoices/{other_invoice.id}")
    assert resp.status_code == 404


def test_bulk_gst_export_zip_contains_each_selected_clients_workbooks(client, db, super_admin, tenant, client_admin):
    from io import BytesIO
    from zipfile import ZipFile

    from app.models.tenant import Gstin, RegistrationType, Tenant
    from app.models.user import User, UserRole

    second_tenant = Tenant(legal_name="Second Biriyani Co", registration_type=RegistrationType.REGULAR)
    db.session.add(second_tenant)
    db.session.flush()
    db.session.add(Gstin(tenant_id=second_tenant.id, gstin="29CCCCC0000C1Z5", state_code="29", state_name="Karnataka", is_primary=True))
    second_admin = User(tenant_id=second_tenant.id, name="Second Admin", email="second@example.com", role=UserRole.CLIENT_ADMIN, must_change_password=False)
    second_admin.set_password("SecondSecret123")
    db.session.add(second_admin)
    db.session.commit()

    _make_invoice(db, tenant, client_admin)
    _make_invoice(db, second_tenant, second_admin)

    _login(client, super_admin.email, "SuperSecret123")
    resp = client.post(
        "/admin/gst-exports/download",
        data={"period": date.today().strftime("%Y-%m"), "tenant_ids": [str(tenant.id), str(second_tenant.id)]},
    )

    assert resp.status_code == 200
    assert resp.content_type == "application/zip"
    zf = ZipFile(BytesIO(resp.data))
    names = zf.namelist()
    assert any("Acme Traders" in n and "GSTR1" in n for n in names)
    assert any("Second Biriyani Co" in n and "GSTR3B" in n for n in names)


def test_bulk_gst_export_excludes_unregistered_tenants(client, db, super_admin, tenant, client_admin):
    from app.models.tenant import RegistrationType

    tenant.registration_type = RegistrationType.UNREGISTERED
    db.session.commit()

    _login(client, super_admin.email, "SuperSecret123")
    resp = client.get("/admin/gst-exports")

    assert resp.status_code == 200
    assert tenant.legal_name.encode() not in resp.data


def test_tenant_detail_lists_pos_bills_and_notes(client, db, super_admin, tenant, client_admin):
    from app.models.pos_bill import PaymentMode
    from app.pos.services import CartInput, CartLineInput, checkout

    bill = checkout(
        tenant, client_admin,
        CartInput(lines=[CartLineInput(description="Chicken Biriyani", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("150"), gst_rate=Decimal("5"), unit="plate")]),
        PaymentMode.CASH,
    )
    db.session.commit()

    _login(client, super_admin.email, "SuperSecret123")
    resp = client.get(f"/admin/clients/{tenant.id}")

    assert resp.status_code == 200
    assert bill.bill_number.encode() in resp.data
