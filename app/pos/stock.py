"""Branch stock counters - how much a kitchen (Client Admin) has sent a
branch (a Staff login) today via a branch-billed invoice, and how much
that branch has sold today through its own POS.

Only products the kitchen has actually allocated to a branch today are
tracked - everything else shows no counter at all. "Resets daily" falls
out of every query here being scoped to today's date; nothing carries
over from yesterday. There is no checkout limit: a branch can keep
billing past its allocation (serving sizes vary too much to enforce a
hard stop) - the counter is purely a tracking signal, for the branch and
for the Client Admin watching it from Branches, and `remaining` is
allowed to go negative to show that honestly rather than hiding it at
zero.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.extensions import db
from app.models.pos_bill import POSBill, POSBillLine, POSBillStatus
from app.models.product import Product
from app.models.stock import BranchStockAllocation

ZERO = Decimal("0")


def _day_bounds(on_date: date) -> tuple[datetime, datetime]:
    return datetime.combine(on_date, datetime.min.time()), datetime.combine(on_date, datetime.max.time())


def today_allocations(branch_user_id: int, on_date: date | None = None) -> dict[int, Decimal]:
    """product_id -> total quantity sent to this branch on on_date (today
    by default)."""
    on_date = on_date or date.today()
    rows = (
        db.session.query(BranchStockAllocation.product_id, db.func.sum(BranchStockAllocation.qty))
        .filter(
            BranchStockAllocation.branch_user_id == branch_user_id,
            BranchStockAllocation.allocated_date == on_date,
        )
        .group_by(BranchStockAllocation.product_id)
        .all()
    )
    return {product_id: qty for product_id, qty in rows}


def today_sold(branch_user_id: int, on_date: date | None = None) -> dict[int, Decimal]:
    """product_id -> total quantity this branch has actually sold
    (completed POS bills only) on on_date (today by default)."""
    on_date = on_date or date.today()
    start, end = _day_bounds(on_date)
    rows = (
        db.session.query(POSBillLine.product_id, db.func.sum(POSBillLine.qty))
        .join(POSBill, POSBill.id == POSBillLine.bill_id)
        .filter(
            POSBill.created_by_id == branch_user_id,
            POSBill.status == POSBillStatus.COMPLETED,
            POSBill.completed_at >= start,
            POSBill.completed_at <= end,
            POSBillLine.product_id.isnot(None),
        )
        .group_by(POSBillLine.product_id)
        .all()
    )
    return {product_id: qty for product_id, qty in rows}


@dataclass
class ProductStockStatus:
    product_id: int
    product_name: str
    unit: str
    allocated: Decimal
    sold: Decimal

    @property
    def remaining(self) -> Decimal:
        """Can go negative - there's no checkout limit, so this is a
        tracking signal, not a hard floor at zero."""
        return self.allocated - self.sold


def branch_stock_status(branch_user_id: int, on_date: date | None = None) -> list[ProductStockStatus]:
    """One entry per product this branch has an allocation for today,
    sorted by product name - the list the collapsible counter and the
    Client Admin's Branches tracking view both render from."""
    allocated = today_allocations(branch_user_id, on_date)
    if not allocated:
        return []
    sold = today_sold(branch_user_id, on_date)
    products = {p.id: p for p in Product.query.filter(Product.id.in_(allocated.keys())).all()}

    statuses = []
    for product_id, allocated_qty in allocated.items():
        product = products.get(product_id)
        if not product:
            continue
        statuses.append(
            ProductStockStatus(
                product_id=product_id,
                product_name=product.name,
                unit=product.unit,
                allocated=allocated_qty,
                sold=sold.get(product_id, ZERO),
            )
        )
    statuses.sort(key=lambda s: s.product_name)
    return statuses


def allocate_stock(
    tenant_id: int, branch_user_id: int, created_by_id: int, lines: list[tuple[int, Decimal]], on_date: date | None = None
) -> None:
    """Records stock arriving at a branch - called from
    app.invoicing.services.create_invoice when an invoice is billed to a
    branch. Adds to, never replaces, whatever's already been sent to that
    branch on on_date (today by default)."""
    on_date = on_date or date.today()
    for product_id, qty in lines:
        db.session.add(
            BranchStockAllocation(
                tenant_id=tenant_id,
                branch_user_id=branch_user_id,
                product_id=product_id,
                qty=qty,
                allocated_date=on_date,
                created_by_id=created_by_id,
            )
        )
