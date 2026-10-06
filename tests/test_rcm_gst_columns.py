from datetime import date
from decimal import Decimal

from app.invoicing.services import InvoiceInput, LineInput, create_invoice
from app.models.customer import Customer
from app.models.invoice_series import DocumentType
from app.reports.gstr import gstr1_data, gstr3b_data
from app.reports.sales import sales_report_data


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _customer(db, tenant, **overrides):
    data = dict(tenant_id=tenant.id, name="Acme Pvt Ltd", gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka")
    data.update(overrides)
    customer = Customer(**data)
    db.session.add(customer)
    db.session.flush()
    return customer


def _rcm_invoice(db, tenant, client_admin, customer, qty="1", rate="1000", gst_rate="18"):
    gstin = tenant.gstins.first()
    return create_invoice(
        tenant,
        client_admin,
        InvoiceInput(
            gstin_id=gstin.id,
            document_type=DocumentType.RCM_INVOICE,
            customer_id=customer.id,
            place_of_supply_state_code=gstin.state_code,
            invoice_date=date(2026, 10, 5),
            notes=None,
            lines=[
                LineInput(
                    description="GTA freight", hsn_or_sac_code="996511", qty=Decimal(qty),
                    rate=Decimal(rate), discount_percent=Decimal("0"), gst_rate=Decimal(gst_rate),
                )
            ],
        ),
    )


def _tax_invoice(db, tenant, client_admin, customer, qty="1", rate="1000", gst_rate="18"):
    gstin = tenant.gstins.first()
    return create_invoice(
        tenant,
        client_admin,
        InvoiceInput(
            gstin_id=gstin.id,
            document_type=DocumentType.TAX_INVOICE,
            customer_id=customer.id,
            place_of_supply_state_code=gstin.state_code,
            invoice_date=date(2026, 10, 5),
            notes=None,
            lines=[
                LineInput(
                    description="Catering", hsn_or_sac_code="996331", qty=Decimal(qty),
                    rate=Decimal(rate), discount_percent=Decimal("0"), gst_rate=Decimal(gst_rate),
                )
            ],
        ),
    )


def test_rcm_invoice_stores_gst_in_separate_columns_not_cgst_sgst_igst(db, tenant, client_admin):
    customer = _customer(db, tenant)
    invoice = _rcm_invoice(db, tenant, client_admin, customer)
    db.session.commit()

    # Intra-state (both GSTIN and place of supply are Karnataka) -> CGST+SGST split.
    assert invoice.total_cgst == Decimal("0.00")
    assert invoice.total_sgst == Decimal("0.00")
    assert invoice.total_igst == Decimal("0.00")
    assert invoice.total_rcgst == Decimal("90.00")
    assert invoice.total_rsgst == Decimal("90.00")
    assert invoice.total_rigst == Decimal("0.00")

    line = invoice.lines[0]
    assert line.cgst_amount == Decimal("0.00")
    assert line.sgst_amount == Decimal("0.00")
    assert line.rcgst_amount == Decimal("90.00")
    assert line.rsgst_amount == Decimal("90.00")


def test_rcm_invoice_interstate_uses_rigst_not_igst(db, tenant, client_admin):
    # Tenant's own GSTIN (from the `tenant` fixture) is Maharashtra (27);
    # billing to Karnataka (29) makes this genuinely interstate.
    customer = _customer(db, tenant, state_code="29", state_name="Karnataka")
    gstin = tenant.gstins.first()
    assert gstin.state_code == "27"
    invoice = create_invoice(
        tenant,
        client_admin,
        InvoiceInput(
            gstin_id=gstin.id,
            document_type=DocumentType.RCM_INVOICE,
            customer_id=customer.id,
            place_of_supply_state_code="29",
            invoice_date=date(2026, 10, 5),
            notes=None,
            lines=[
                LineInput(
                    description="GTA freight", hsn_or_sac_code="996511", qty=Decimal("1"),
                    rate=Decimal("1000"), discount_percent=Decimal("0"), gst_rate=Decimal("18"),
                )
            ],
        ),
    )
    db.session.commit()

    assert invoice.total_igst == Decimal("0.00")
    assert invoice.total_rigst == Decimal("180.00")
    assert invoice.total_rcgst == Decimal("0.00")
    assert invoice.total_rsgst == Decimal("0.00")


def test_rcm_invoice_grand_total_still_includes_the_tax(db, tenant, client_admin):
    customer = _customer(db, tenant)
    invoice = _rcm_invoice(db, tenant, client_admin, customer)
    db.session.commit()
    # Storage column changed, not the amount actually on the invoice.
    assert invoice.grand_total == Decimal("1180.00")


def test_tax_invoice_still_uses_normal_columns_unaffected(db, tenant, client_admin):
    customer = _customer(db, tenant)
    invoice = _tax_invoice(db, tenant, client_admin, customer)
    db.session.commit()

    assert invoice.total_cgst == Decimal("90.00")
    assert invoice.total_sgst == Decimal("90.00")
    assert invoice.total_rcgst == Decimal("0.00")
    assert invoice.total_rsgst == Decimal("0.00")
    assert invoice.lines[0].rcgst_amount == Decimal("0.00")


def test_gstr1_still_discloses_rcm_tax_for_reconciliation(db, tenant, client_admin):
    customer = _customer(db, tenant)
    _rcm_invoice(db, tenant, client_admin, customer)
    db.session.commit()

    gstin = tenant.gstins.first()
    data = gstr1_data(tenant, gstin, "2026-10")
    assert data["totals"]["cgst"] == Decimal("90.00")
    assert data["totals"]["sgst"] == Decimal("90.00")
    assert len(data["b2b"]) == 1
    doc = data["b2b"][0]["documents"][0]
    assert doc["cgst"] == Decimal("90.00")
    assert doc["sgst"] == Decimal("90.00")


def test_gstr3b_excludes_rcm_tax_from_this_tenants_own_payable(db, tenant, client_admin):
    customer = _customer(db, tenant)
    _rcm_invoice(db, tenant, client_admin, customer)
    _tax_invoice(db, tenant, client_admin, customer)
    db.session.commit()

    gstin = tenant.gstins.first()
    data = gstr3b_data(tenant, gstin, "2026-10")
    # Only the Tax Invoice's 90+90 CGST/SGST should count - the RCM
    # invoice's 90+90 is the recipient's liability, not this tenant's.
    assert data["gross_tax_payable"] == Decimal("180.00")
    assert data["outward_taxable"]["cgst"] == Decimal("90.00")
    assert data["outward_taxable"]["sgst"] == Decimal("90.00")


def test_sales_report_shows_rcm_tax_combined_for_the_clients_own_ledger(db, tenant, client_admin):
    customer = _customer(db, tenant)
    _rcm_invoice(db, tenant, client_admin, customer)
    db.session.commit()

    data = sales_report_data(tenant, date(2026, 10, 1), date(2026, 10, 31))
    row = data["rows"][0]
    assert row.cgst == Decimal("90.00")
    assert row.sgst == Decimal("90.00")
    assert data["totals"]["cgst"] == Decimal("90.00")


def test_invoice_view_page_shows_rcgst_label_not_cgst(client, db, tenant, client_admin):
    customer = _customer(db, tenant)
    invoice = _rcm_invoice(db, tenant, client_admin, customer)
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get(f"/invoices/{invoice.id}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "RCGST" in body
    assert "RSGST" in body
