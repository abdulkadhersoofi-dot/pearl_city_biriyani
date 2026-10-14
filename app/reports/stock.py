"""Stock report - the same allocated-vs-sold counter as
app.pos.stock.branch_stock_status, generalized from "today, one branch"
to any date range and any branch selection, for the Reports area's
filter bar (app.reports.filters).
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from app.extensions import db
from app.models.pos_bill import POSBill, POSBillLine, POSBillStatus
from app.pos.stock import returned_qty
from app.models.product import Product
from app.models.stock import BranchStockAllocation
from app.models.tenant import Tenant

ZERO = Decimal("0")


@dataclass
class ProductStockRow:
    product_id: int
    product_name: str
    unit: str
    allocated: Decimal
    sold: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.allocated - self.sold


def stock_report_data(tenant: Tenant, start: date, end: date, branch_ids: list[int] | None = None) -> dict:
    allocation_query = db.session.query(
        BranchStockAllocation.product_id, db.func.sum(BranchStockAllocation.qty)
    ).filter(
        BranchStockAllocation.tenant_id == tenant.id,
        BranchStockAllocation.allocated_date >= start,
        BranchStockAllocation.allocated_date <= end,
    )
    if branch_ids:
        allocation_query = allocation_query.filter(BranchStockAllocation.branch_user_id.in_(branch_ids))
    allocated = dict(allocation_query.group_by(BranchStockAllocation.product_id).all())

    day_start, day_end = datetime.combine(start, time.min), datetime.combine(end, time.max)
    sold_query = (
        db.session.query(POSBillLine.product_id, db.func.sum(POSBillLine.qty))
        .join(POSBill, POSBill.id == POSBillLine.bill_id)
        .filter(
            POSBill.tenant_id == tenant.id,
            POSBill.status == POSBillStatus.COMPLETED,
            POSBill.completed_at >= day_start,
            POSBill.completed_at <= day_end,
            POSBillLine.product_id.isnot(None),
        )
    )
    if branch_ids:
        sold_query = sold_query.filter(POSBill.created_by_id.in_(branch_ids))
    sold = dict(sold_query.group_by(POSBillLine.product_id).all())
    # Goods that came back (sales returns) are back in stock.
    for product_id, qty in returned_qty(day_start, day_end, branch_ids or None, tenant_id=tenant.id).items():
        sold[product_id] = sold.get(product_id, ZERO) - qty

    products = Product.query.filter_by(tenant_id=tenant.id, is_active=True).order_by(Product.name).all()
    rows = [
        ProductStockRow(
            product_id=p.id,
            product_name=p.name,
            unit=p.unit,
            allocated=allocated.get(p.id, ZERO),
            sold=sold.get(p.id, ZERO),
        )
        for p in products
    ]

    totals = {
        "allocated": sum((r.allocated for r in rows), ZERO),
        "sold": sum((r.sold for r in rows), ZERO),
        "remaining": sum((r.remaining for r in rows), ZERO),
    }

    return {"tenant": tenant, "start": start, "end": end, "rows": rows, "totals": totals}
