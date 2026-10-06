"""Branch stock counters - how much a kitchen (Client Admin) has sent a
branch (a Staff login) today, how much that branch has sold today, and
whether its POS can still sell more.

Only products the kitchen has actually allocated to a branch today are
tracked - everything else sells with no limit, same as before this
feature existed. "Resets daily" falls out of every query here being
scoped to today's date; nothing carries over from yesterday.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models.pos_bill import POSBill, POSBillLine, POSBillStatus
from app.models.product import Product
from app.models.stock import BranchStockAllocation

ZERO = Decimal("0")


class StockLimitError(Exception):
    """A branch's POS tried to check out more of a product than its
    today's allocation (plus grace) allows."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class StockAllocationError(Exception):
    """The Client Admin's "send stock to this branch" form was invalid."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def parse_allocation_lines(product_ids, qtys) -> list[tuple[int, Decimal]]:
    """Parallel product_id[]/qty[] arrays (same pattern as the invoice and
    POS cart lines) -> [(product_id, qty), ...], skipping blank rows and
    rejecting anything that isn't a positive quantity."""
    lines = []
    for i, raw_id in enumerate(product_ids):
        raw_id = (raw_id or "").strip()
        raw_qty = qtys[i] if i < len(qtys) else ""
        if not raw_id and not raw_qty:
            continue
        if not raw_id:
            raise StockAllocationError("Pick an item for every quantity you entered.")
        try:
            product_id = int(raw_id)
        except ValueError:
            raise StockAllocationError("Invalid item selected.")
        try:
            qty = Decimal(str(raw_qty))
        except (InvalidOperation, TypeError):
            raise StockAllocationError("Quantity must be a number.")
        if qty <= 0:
            raise StockAllocationError("Quantity must be greater than zero.")
        lines.append((product_id, qty))
    if not lines:
        raise StockAllocationError("Add at least one item and quantity.")
    return lines


@dataclass
class ProductStockStatus:
    product_id: int
    product_name: str
    unit: str
    allocated: Decimal
    sold: Decimal
    grace: Decimal

    @property
    def remaining(self) -> Decimal:
        """Never negative - once sold reaches what was sent, the counter
        just shows 0, it doesn't go negative (the grace buffer past this
        point is deliberately invisible, not advertised to the till)."""
        return max(ZERO, self.allocated - self.sold)

    @property
    def available_with_grace(self) -> Decimal:
        """What the POS actually enforces - allocated + grace, minus
        what's already sold. Can be 0 (sold out, even with grace) but
        never negative for display purposes."""
        return max(ZERO, self.allocated + self.grace - self.sold)


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


def branch_stock_status(branch_user_id: int, grace_qty: int, on_date: date | None = None) -> list[ProductStockStatus]:
    """One entry per product this branch has an allocation for today,
    sorted by product name - the list the collapsible counter and the
    Client Admin's tracking view both render from."""
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
                grace=Decimal(grace_qty),
            )
        )
    statuses.sort(key=lambda s: s.product_name)
    return statuses


def allocate_stock(
    tenant_id: int, branch_user_id: int, created_by_id: int, lines: list[tuple[int, Decimal]], on_date: date | None = None
) -> None:
    """Records one delivery (possibly several products) from the kitchen
    to a branch - adds to, never replaces, whatever's already been sent
    to that branch today."""
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


def check_cart_against_stock(branch_user_id: int, cart_lines, grace_qty: int) -> None:
    """Raises StockLimitError if checking out these lines would push any
    allocated product past its allocated+grace limit for today. Lines
    with no product_id (ad-hoc items) aren't tracked, same as a product
    with no allocation at all today - nothing to check them against."""
    requested: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for line in cart_lines:
        if line.product_id:
            requested[line.product_id] += line.qty
    if not requested:
        return

    allocated = today_allocations(branch_user_id)
    tracked_ids = [pid for pid in requested if pid in allocated]
    if not tracked_ids:
        return

    sold = today_sold(branch_user_id)
    products = {p.id: p for p in Product.query.filter(Product.id.in_(tracked_ids)).all()}

    for product_id in tracked_ids:
        limit = allocated[product_id] + Decimal(grace_qty)
        already_sold = sold.get(product_id, ZERO)
        if already_sold + requested[product_id] > limit:
            name = products[product_id].name if product_id in products else "this item"
            left = max(ZERO, limit - already_sold)
            raise StockLimitError(
                f"Only {left:g} {name} left for today's stock - ask the kitchen for more before billing this sale."
            )
