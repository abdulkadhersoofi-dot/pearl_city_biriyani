from datetime import date
from decimal import Decimal

from app.invoicing.services import InvoiceInput, LineInput, create_invoice
from app.invoicing.services import void_invoice
from app.models.customer import Customer
from app.models.invoice_series import DocumentType
from app.models.pos_bill import PaymentMode
from app.notes.services import NoteInput, create_note
from app.pos.services import CartInput, CartLineInput, checkout
from app.reports.export import build_gstr1_workbook, build_gstr3b_workbook, render_gstr1_pdf, render_gstr3b_pdf, to_json_bytes
from app.reports.gstr import ReportPeriodError, gstr1_data, gstr3b_data, month_bounds


def _b2b_customer(db, tenant):
    customer = Customer(tenant_id=tenant.id, name="Acme Pvt Ltd", gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka")
    db.session.add(customer)
    db.session.flush()
    return customer


def test_month_bounds_handles_short_and_long_months():
    assert month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_bounds("2026-01") == (date(2026, 1, 1), date(2026, 1, 31))


def test_month_bounds_rejects_a_bad_period():
    import pytest

    with pytest.raises(ReportPeriodError):
        month_bounds("not-a-period")


def test_gstr1_separates_b2b_and_b2c_and_nets_a_credit_note(db, tenant, client_admin):
    gstin = tenant.gstins.first()
    b2b_customer = _b2b_customer(db, tenant)

    invoice = create_invoice(
        tenant, client_admin,
        InvoiceInput(
            gstin_id=gstin.id, document_type=DocumentType.TAX_INVOICE, customer_id=b2b_customer.id,
            place_of_supply_state_code="29", invoice_date=date(2026, 10, 5), notes=None,
            lines=[LineInput(description="Catering", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("10000"), discount_percent=Decimal("0"), gst_rate=Decimal("18"))],
        ),
    )
    db.session.commit()

    checkout(
        tenant, client_admin,
        CartInput(lines=[CartLineInput(description="Chicken Biriyani", hsn_or_sac_code="996331", qty=Decimal("3"), rate=Decimal("150"), gst_rate=Decimal("5"), unit="plate")]),
        PaymentMode.CASH,
    )
    db.session.commit()

    create_note(
        tenant, client_admin,
        NoteInput(
            original_invoice_id=invoice.id, document_type=DocumentType.CREDIT_NOTE, note_date=date(2026, 10, 10),
            reason="Partial service shortfall",
            lines=[LineInput(description="Catering", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("1000"), discount_percent=Decimal("0"), gst_rate=Decimal("18"))],
        ),
    )
    db.session.commit()

    data = gstr1_data(tenant, gstin, "2026-10")

    assert len(data["b2b"]) == 1
    assert data["b2b"][0]["customer_gstin"] == "29BBBBB0000B1Z5"
    assert len(data["b2c"]) == 1
    assert data["b2c"][0].taxable_value == Decimal("450.00")
    assert len(data["credit_debit_notes"]) == 1

    # Invoice (10000 @ 18% IGST, inter-state) net of the credit note (1000 @ 18%)
    # plus the POS sale (450 @ 5%, intra-state CGST+SGST).
    assert data["totals"]["taxable_value"] == Decimal("9450.00")
    assert data["totals"]["igst"] == Decimal("1620.00")
    assert data["totals"]["cgst"] == Decimal("11.25")
    assert data["totals"]["sgst"] == Decimal("11.25")


def test_gstr1_excludes_voided_invoices_from_totals_but_lists_them_as_cancelled(db, tenant, client_admin):
    gstin = tenant.gstins.first()
    b2b_customer = _b2b_customer(db, tenant)

    invoice = create_invoice(
        tenant, client_admin,
        InvoiceInput(
            gstin_id=gstin.id, document_type=DocumentType.TAX_INVOICE, customer_id=b2b_customer.id,
            place_of_supply_state_code="29", invoice_date=date(2026, 10, 5), notes=None,
            lines=[LineInput(description="Catering", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("1000"), discount_percent=Decimal("0"), gst_rate=Decimal("18"))],
        ),
    )
    db.session.commit()
    void_invoice(invoice, client_admin, "Order cancelled")
    db.session.commit()

    data = gstr1_data(tenant, gstin, "2026-10")

    assert data["totals"]["taxable_value"] == Decimal("0")
    assert len(data["b2b"]) == 0
    cancelled = [s for s in data["document_summary"] if s.cancelled_count]
    assert len(cancelled) == 1
    assert cancelled[0].cancelled_count == 1


def test_gstr3b_buckets_exports_as_zero_rated(db, tenant, client_admin):
    gstin = tenant.gstins.first()
    customer = _b2b_customer(db, tenant)

    create_invoice(
        tenant, client_admin,
        InvoiceInput(
            gstin_id=gstin.id, document_type=DocumentType.EXPORT_INVOICE_LUT, customer_id=customer.id,
            place_of_supply_state_code="29", invoice_date=date(2026, 10, 5), notes=None,
            lines=[LineInput(description="Export service", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("5000"), discount_percent=Decimal("0"), gst_rate=Decimal("0"))],
        ),
    )
    db.session.commit()

    data = gstr3b_data(tenant, gstin, "2026-10")

    assert data["outward_zero_rated"]["taxable_value"] == Decimal("5000.00")
    assert data["outward_taxable"]["taxable_value"] == Decimal("0")
    assert data["gross_tax_payable"] == Decimal("0")


def test_pos_refund_nets_out_in_gstr1_b2c(db, tenant, client_admin):
    from app.pos.services import refund_bill

    gstin = tenant.gstins.first()
    bill = checkout(
        tenant, client_admin,
        CartInput(lines=[CartLineInput(description="Chicken Biriyani", hsn_or_sac_code="996331", qty=Decimal("2"), rate=Decimal("150"), gst_rate=Decimal("5"), unit="plate")]),
        PaymentMode.CASH,
    )
    db.session.commit()
    refund_bill(bill, client_admin, bill.grand_total, "Customer complaint")
    db.session.commit()

    data = gstr1_data(tenant, gstin, date.today().strftime("%Y-%m"))

    # A fully refunded bill must net to zero, not count as a positive sale.
    assert data["totals"]["taxable_value"] == Decimal("0.00")
    assert data["totals"]["cgst"] == Decimal("0.00")


def test_exports_produce_nonempty_files(db, tenant, client_admin):
    gstin = tenant.gstins.first()
    checkout(
        tenant, client_admin,
        CartInput(lines=[CartLineInput(description="Chicken Biriyani", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("150"), gst_rate=Decimal("5"), unit="plate")]),
        PaymentMode.CASH,
    )
    db.session.commit()

    g1 = gstr1_data(tenant, gstin, date.today().strftime("%Y-%m"))
    g3b = gstr3b_data(tenant, gstin, date.today().strftime("%Y-%m"))

    assert len(build_gstr1_workbook(g1).getvalue()) > 100
    assert len(build_gstr3b_workbook(g3b).getvalue()) > 100
    assert render_gstr1_pdf(g1).startswith(b"%PDF")
    assert render_gstr3b_pdf(g3b).startswith(b"%PDF")
    assert b'"period"' in to_json_bytes(g1)
