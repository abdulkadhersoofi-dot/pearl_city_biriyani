"""Date-range sales report - invoices and POS bills combined, across every
active GSTIN, for any date range and any registration type. Unlike the
GSTR-1/3B working papers (one calendar month, one GSTIN, Regular scheme
only, line-item level for HSN aggregation), this is a document-level
list meant for the client's own records: "what did I sell between these
two dates", exportable to Excel or PDF. The Day-book covers one day of
POS activity only; this covers any range and includes invoices too.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_series import DOCUMENT_TYPE_LABELS
from app.models.pos_bill import POSBill, POSBillStatus
from app.models.tenant import Tenant
from app.reports.gstr import normalized_customer_gstin

ZERO = Decimal("0")


@dataclass
class SalesReportRow:
    doc_type: str
    doc_number: str
    doc_date: date
    billing_gstin: str
    customer_name: str
    customer_gstin: str | None
    payment_mode: str | None
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total: Decimal
    sign: int = 1

    @property
    def signed_total(self) -> Decimal:
        return self.sign * self.total


def sales_report_data(tenant: Tenant, start: date, end: date) -> dict:
    day_start, day_end = datetime.combine(start, time.min), datetime.combine(end, time.max)

    invoices = (
        Invoice.query.filter(
            Invoice.tenant_id == tenant.id,
            Invoice.invoice_date >= start,
            Invoice.invoice_date <= end,
            Invoice.status != InvoiceStatus.VOID,
        )
        .order_by(Invoice.invoice_date, Invoice.invoice_number)
        .all()
    )
    pos_bills = (
        POSBill.query.filter(
            POSBill.tenant_id == tenant.id,
            POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
            POSBill.completed_at >= day_start,
            POSBill.completed_at <= day_end,
        )
        .order_by(POSBill.completed_at)
        .all()
    )

    rows: list[SalesReportRow] = []

    for inv in invoices:
        cust = inv.customer_snapshot or {}
        rows.append(
            SalesReportRow(
                doc_type=DOCUMENT_TYPE_LABELS.get(inv.document_type, inv.document_type.value),
                doc_number=inv.invoice_number,
                doc_date=inv.invoice_date,
                billing_gstin=inv.gstin.display_label if inv.gstin else "",
                customer_name=cust.get("name", ""),
                customer_gstin=normalized_customer_gstin(cust),
                payment_mode=None,
                taxable_value=inv.total_taxable_value,
                cgst=inv.total_cgst,
                sgst=inv.total_sgst,
                igst=inv.total_igst,
                total=inv.grand_total,
            )
        )

    for bill in pos_bills:
        cust = bill.customer_snapshot or {}
        billing_gstin = bill.gstin.display_label if bill.gstin else ""
        payment_mode = bill.payment_mode.value if bill.payment_mode else None
        rows.append(
            SalesReportRow(
                doc_type="POS Bill",
                doc_number=bill.bill_number or f"held-{bill.id}",
                doc_date=bill.completed_at.date() if bill.completed_at else start,
                billing_gstin=billing_gstin,
                customer_name=cust.get("name", "Walk-in"),
                customer_gstin=normalized_customer_gstin(cust),
                payment_mode=payment_mode,
                taxable_value=bill.total_taxable_value,
                cgst=bill.total_cgst,
                sgst=bill.total_sgst,
                igst=bill.total_igst,
                total=bill.grand_total,
            )
        )
        # Same two-entry philosophy as the Day-book: the original sale
        # stays a full + row, and a refund is a separate - row for
        # however much was actually refunded (partial refunds are
        # common), not a flipped status on the original.
        if bill.status == POSBillStatus.REFUNDED and bill.grand_total:
            refund_ratio = min(Decimal(bill.refunded_amount or 0) / Decimal(bill.grand_total), Decimal("1"))
            if refund_ratio:
                rows.append(
                    SalesReportRow(
                        doc_type="POS Bill (refund)",
                        doc_number=f"{bill.bill_number or f'held-{bill.id}'} (refund)",
                        doc_date=bill.refunded_at.date() if bill.refunded_at else start,
                        billing_gstin=billing_gstin,
                        customer_name=cust.get("name", "Walk-in"),
                        customer_gstin=normalized_customer_gstin(cust),
                        payment_mode=payment_mode,
                        taxable_value=bill.total_taxable_value * refund_ratio,
                        cgst=bill.total_cgst * refund_ratio,
                        sgst=bill.total_sgst * refund_ratio,
                        igst=bill.total_igst * refund_ratio,
                        total=bill.grand_total * refund_ratio,
                        sign=-1,
                    )
                )

    rows.sort(key=lambda r: (r.doc_date, r.doc_number))

    totals = {
        "taxable_value": sum((r.sign * r.taxable_value for r in rows), ZERO),
        "cgst": sum((r.sign * r.cgst for r in rows), ZERO),
        "sgst": sum((r.sign * r.sgst for r in rows), ZERO),
        "igst": sum((r.sign * r.igst for r in rows), ZERO),
        "total": sum((r.signed_total for r in rows), ZERO),
    }

    return {
        "tenant": tenant,
        "start": start,
        "end": end,
        "rows": rows,
        "invoice_count": len(invoices),
        "pos_bill_count": len(pos_bills),
        "totals": totals,
    }
