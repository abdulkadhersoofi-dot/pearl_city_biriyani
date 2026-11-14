"""Date-range sales report - invoices, credit and debit notes, POS bills,
refunds and returns combined, across every active GSTIN, for any date range
and any registration type. Unlike the
GSTR-1/3B working papers (one calendar month, one GSTIN, Regular scheme
only, line-item level for HSN aggregation), this is a document-level
list meant for the client's own records: "what did I sell between these
two dates", exportable to Excel or PDF. The Day-book covers one day of
POS activity only; this covers any range and includes invoices too.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.finance import sales_figures
from app.models.invoice_series import DOCUMENT_TYPE_LABELS
from app.models.pos_bill import PaymentMode
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


def _payment(bill) -> str | None:
    # A split bill shows as e.g. "cash 300.00 + upi 200.00".
    if bill.payment_mode == PaymentMode.MIXED and bill.payments:
        return " + ".join(f"{p.mode.value} {p.amount:.2f}" for p in bill.payments)
    return bill.payment_mode.value if bill.payment_mode else None


def _row(e: sales_figures.Entry) -> SalesReportRow:
    doc = e.doc
    cust = doc.customer_snapshot or {}
    gstin = doc.gstin.display_label if doc.gstin else ""
    walk_in = e.kind in (sales_figures.POS_BILL, sales_figures.POS_REFUND, sales_figures.POS_RETURN)
    common = dict(doc_date=e.on, billing_gstin=gstin, customer_name=cust.get("name", "Walk-in" if walk_in else ""),
                  customer_gstin=normalized_customer_gstin(cust), taxable_value=e.taxable, cgst=e.cgst, sgst=e.sgst,
                  igst=e.igst, total=e.total, sign=e.sign)
    if e.kind == sales_figures.INVOICE:
        return SalesReportRow(doc_type=DOCUMENT_TYPE_LABELS.get(doc.document_type, doc.document_type.value),
                              doc_number=doc.invoice_number, payment_mode=None, **common)
    if e.kind in (sales_figures.CREDIT_NOTE, sales_figures.DEBIT_NOTE):
        return SalesReportRow(doc_type=DOCUMENT_TYPE_LABELS.get(doc.document_type, doc.document_type.value),
                              doc_number=doc.note_number, payment_mode=None, **common)
    if e.kind == sales_figures.POS_BILL:
        return SalesReportRow(doc_type="POS Bill", doc_number=doc.bill_number or f"held-{doc.id}",
                              payment_mode=_payment(doc), **common)
    if e.kind == sales_figures.POS_REFUND:
        # The sale stays a full + row on its own day; a refund is a separate
        # - row on the day of the refund, for however much was refunded.
        return SalesReportRow(doc_type="POS Bill (refund)",
                              doc_number=f"{doc.bill_number or f'held-{doc.id}'} (refund)",
                              payment_mode=_payment(doc), **common)
    return SalesReportRow(doc_type="POS Sales Return", doc_number=doc.return_number,
                          payment_mode=doc.refund_mode.value, **common)


def sales_report_data(tenant: Tenant, start: date, end: date, branch_ids: list[int] | None = None) -> dict:
    """Every document that counts as sales in the period - the same ones,
    on the same days, as the dashboard and Profit & loss
    (app/finance/sales_figures.py). A "branch" filter means "sales
    attributable to this branch login": a counter bill rung up there, or an
    invoice billed to that branch (an ordinary customer invoice drops out
    once a branch filter is active)."""
    found = sales_figures.entries(tenant, start, end, branch_ids)
    rows = [_row(e) for e in found]
    rows.sort(key=lambda r: (r.doc_date, r.doc_number))

    def count(*kinds):
        return sum(1 for e in found if e.kind in kinds)

    return {
        "tenant": tenant,
        "start": start,
        "end": end,
        "rows": rows,
        "invoice_count": count(sales_figures.INVOICE),
        "pos_bill_count": count(sales_figures.POS_BILL),
        "pos_return_count": count(sales_figures.POS_RETURN),
        "note_count": count(sales_figures.CREDIT_NOTE, sales_figures.DEBIT_NOTE),
        "totals": sales_figures.totals(found),
    }
