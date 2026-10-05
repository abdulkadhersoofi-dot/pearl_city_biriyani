from datetime import date, timedelta
from decimal import Decimal

from app.invoicing.services import InvoiceInput, LineInput, create_invoice
from app.models.customer import Customer
from app.models.invoice_series import DocumentType
from app.models.pos_bill import PaymentMode
from app.models.tenant import RegistrationType
from app.pos.services import CartInput, CartLineInput, checkout, refund_bill
from app.reports.sales import sales_report_data


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _b2b_customer(db, tenant):
    customer = Customer(tenant_id=tenant.id, name="Acme Pvt Ltd", gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka")
    db.session.add(customer)
    db.session.flush()
    return customer


def test_sales_report_combines_invoices_and_pos_bills(db, tenant, client_admin):
    gstin = tenant.gstins.first()
    b2b_customer = _b2b_customer(db, tenant)

    create_invoice(
        tenant, client_admin,
        InvoiceInput(
            gstin_id=gstin.id, document_type=DocumentType.TAX_INVOICE, customer_id=b2b_customer.id,
            place_of_supply_state_code="29", invoice_date=date.today(), notes=None,
            lines=[LineInput(description="Catering", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("1000"), discount_percent=Decimal("0"), gst_rate=Decimal("18"))],
        ),
    )
    db.session.commit()

    checkout(
        tenant, client_admin,
        CartInput(lines=[CartLineInput(description="Chicken Biriyani", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("150"), gst_rate=Decimal("5"), unit="plate")]),
        PaymentMode.CASH,
    )
    db.session.commit()

    start = date.today() - timedelta(days=1)
    end = date.today() + timedelta(days=1)
    data = sales_report_data(tenant, start, end)

    assert data["invoice_count"] == 1
    assert data["pos_bill_count"] == 1
    assert len(data["rows"]) == 2
    # Both grand totals are rounded to the nearest whole rupee at checkout
    # time (compute_invoice_totals): 1000 @ 18% IGST = 1180 exactly;
    # 150 @ 5% CGST+SGST = 157.50, rounds up to 158.
    assert data["totals"]["total"] == Decimal("1180.00") + Decimal("158.00")


def test_sales_report_date_range_excludes_sales_outside_it(db, tenant, client_admin):
    gstin = tenant.gstins.first()
    b2b_customer = _b2b_customer(db, tenant)
    create_invoice(
        tenant, client_admin,
        InvoiceInput(
            gstin_id=gstin.id, document_type=DocumentType.TAX_INVOICE, customer_id=b2b_customer.id,
            place_of_supply_state_code="29", invoice_date=date(2020, 1, 1), notes=None,
            lines=[LineInput(description="Old sale", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("100"), discount_percent=Decimal("0"), gst_rate=Decimal("18"))],
        ),
    )
    db.session.commit()

    data = sales_report_data(tenant, date.today() - timedelta(days=1), date.today() + timedelta(days=1))
    assert data["invoice_count"] == 0
    assert data["rows"] == []


def test_sales_report_nets_a_pos_refund_to_zero(db, tenant, client_admin):
    bill = checkout(
        tenant, client_admin,
        CartInput(lines=[CartLineInput(description="Chicken Biriyani", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("150"), gst_rate=Decimal("5"), unit="plate")]),
        PaymentMode.CASH,
    )
    db.session.commit()
    refund_bill(bill, client_admin, bill.grand_total, "Customer changed mind")
    db.session.commit()

    data = sales_report_data(tenant, date.today(), date.today())
    assert len(data["rows"]) == 2
    assert data["totals"]["total"] == Decimal("0.00")


def test_sales_report_is_available_to_non_regular_tenants(client, db, tenant, client_admin):
    tenant.registration_type = RegistrationType.UNREGISTERED
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/reports/sales")
    assert resp.status_code == 200


def test_sales_report_excel_and_pdf_download(client, db, tenant, client_admin):
    checkout(
        tenant, client_admin,
        CartInput(lines=[CartLineInput(description="Chicken Biriyani", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("150"), gst_rate=Decimal("5"), unit="plate")]),
        PaymentMode.CASH,
    )
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")

    xlsx_resp = client.get("/reports/sales.xlsx")
    assert xlsx_resp.status_code == 200
    assert len(xlsx_resp.data) > 0

    pdf_resp = client.get("/reports/sales.pdf")
    assert pdf_resp.status_code == 200
    assert pdf_resp.data.startswith(b"%PDF")


def test_sales_report_swaps_a_reversed_date_range(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/reports/sales?start=2026-12-31&end=2026-01-01")
    assert resp.status_code == 200
