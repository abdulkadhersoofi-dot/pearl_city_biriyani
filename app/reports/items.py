"""Sales-by-item report - how much of each product sold, combining
invoice lines and POS bill lines over any date range and any branch
selection (app.reports.filters). A line with no product_id (a one-off
typed item, not picked from the catalog) is grouped by its description
instead, so it still shows up rather than being dropped.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from app.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from app.models.pos_bill import POSBill, POSBillLine, POSBillStatus
from app.models.tenant import Tenant

ZERO = Decimal("0")


@dataclass
class ItemSalesRow:
    product_id: int | None
    product_name: str
    unit: str
    qty: Decimal
    taxable_value: Decimal
    total: Decimal


def item_sales_data(tenant: Tenant, start: date, end: date, branch_ids: list[int] | None = None) -> dict:
    day_start, day_end = datetime.combine(start, time.min), datetime.combine(end, time.max)

    invoice_lines_query = (
        InvoiceLine.query.join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.invoice_date >= start,
            Invoice.invoice_date <= end,
            Invoice.status != InvoiceStatus.VOID,
        )
    )
    # Completed + refunded bills, same as the Sales report - this is
    # gross quantity sold, not netted for partial refunds (a refund is
    # tracked as an amount against the bill, not a line-level reversal).
    pos_lines_query = (
        POSBillLine.query.join(POSBill, POSBill.id == POSBillLine.bill_id)
        .filter(
            POSBill.tenant_id == tenant.id,
            POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
            POSBill.completed_at >= day_start,
            POSBill.completed_at <= day_end,
        )
    )
    if branch_ids:
        invoice_lines_query = invoice_lines_query.filter(Invoice.branch_user_id.in_(branch_ids))
        pos_lines_query = pos_lines_query.filter(POSBill.created_by_id.in_(branch_ids))

    buckets: dict[tuple, ItemSalesRow] = {}

    def _add(line):
        key = ("p", line.product_id) if line.product_id else ("d", line.description)
        row = buckets.get(key)
        if row is None:
            row = ItemSalesRow(
                product_id=line.product_id,
                product_name=line.description,
                unit=line.unit,
                qty=ZERO,
                taxable_value=ZERO,
                total=ZERO,
            )
            buckets[key] = row
        row.qty += line.qty
        row.taxable_value += line.taxable_value
        row.total += line.line_total

    for line in invoice_lines_query.all():
        _add(line)
    for line in pos_lines_query.all():
        _add(line)

    rows = sorted(buckets.values(), key=lambda r: r.total, reverse=True)
    totals = {
        "qty": sum((r.qty for r in rows), ZERO),
        "taxable_value": sum((r.taxable_value for r in rows), ZERO),
        "total": sum((r.total for r in rows), ZERO),
    }

    return {"tenant": tenant, "start": start, "end": end, "rows": rows, "totals": totals}
