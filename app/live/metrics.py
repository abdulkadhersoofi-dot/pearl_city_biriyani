"""Today's branch-vs-branch and item-vs-item performance - the data
behind the Live dashboard (app.live.routes). Unlike the Reports area's
date-range reports, this is always "today": it is meant to be watched
live during trading hours, polled on an interval, not filtered to an
arbitrary period.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from app.extensions import db
from app.models.pos_bill import POSBill, POSBillLine, POSBillStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole

ZERO = Decimal("0")


@dataclass
class BranchPerformance:
    branch_id: int
    branch_name: str
    revenue: Decimal
    order_count: int


def branch_leaderboard(tenant: Tenant, on_date: date | None = None) -> list[BranchPerformance]:
    """Every active branch (Staff) login, ranked by today's total POS
    revenue, highest first - the chosen leaderboard metric. A branch
    with no sales yet today still appears, at 0, so it visibly shows up
    as idle rather than being left off the board."""
    on_date = on_date or date.today()
    day_start, day_end = datetime.combine(on_date, time.min), datetime.combine(on_date, time.max)

    base_filter = (
        POSBill.tenant_id == tenant.id,
        POSBill.status == POSBillStatus.COMPLETED,
        POSBill.completed_at >= day_start,
        POSBill.completed_at <= day_end,
    )
    revenue_by_branch = dict(
        db.session.query(POSBill.created_by_id, db.func.sum(POSBill.grand_total))
        .filter(*base_filter)
        .group_by(POSBill.created_by_id)
        .all()
    )
    orders_by_branch = dict(
        db.session.query(POSBill.created_by_id, db.func.count(POSBill.id))
        .filter(*base_filter)
        .group_by(POSBill.created_by_id)
        .all()
    )

    branches = User.query.filter_by(tenant_id=tenant.id, role=UserRole.STAFF, is_active=True).order_by(User.name).all()
    rows = [
        BranchPerformance(
            branch_id=b.id,
            branch_name=b.name,
            revenue=revenue_by_branch.get(b.id, ZERO),
            order_count=orders_by_branch.get(b.id, 0),
        )
        for b in branches
    ]
    # Order count is shown alongside revenue but never used to break a
    # tie - revenue alone decides rank, per how this leaderboard was
    # specified.
    rows.sort(key=lambda r: r.revenue, reverse=True)
    return rows


@dataclass
class ItemPerformance:
    product_id: int | None
    product_name: str
    qty: Decimal
    revenue: Decimal
    share_percent: float


def item_sales_today(tenant: Tenant, on_date: date | None = None, limit: int = 8) -> list[ItemPerformance]:
    """Top-selling items today by revenue, each carrying its share of
    today's POS revenue - what the Live dashboard's pie chart is drawn
    from. POS-only, not invoices: "best selling item" on a live floor
    dashboard means retail till activity."""
    on_date = on_date or date.today()
    day_start, day_end = datetime.combine(on_date, time.min), datetime.combine(on_date, time.max)

    lines = (
        POSBillLine.query.join(POSBill, POSBill.id == POSBillLine.bill_id)
        .filter(
            POSBill.tenant_id == tenant.id,
            POSBill.status == POSBillStatus.COMPLETED,
            POSBill.completed_at >= day_start,
            POSBill.completed_at <= day_end,
        )
        .all()
    )

    buckets: dict[tuple, ItemPerformance] = {}
    for line in lines:
        key = ("p", line.product_id) if line.product_id else ("d", line.description)
        row = buckets.get(key)
        if row is None:
            row = ItemPerformance(
                product_id=line.product_id, product_name=line.description, qty=ZERO, revenue=ZERO, share_percent=0.0
            )
            buckets[key] = row
        row.qty += line.qty
        row.revenue += line.line_total

    rows = sorted(buckets.values(), key=lambda r: r.revenue, reverse=True)
    total_revenue = sum((r.revenue for r in rows), ZERO)
    for row in rows:
        row.share_percent = float(row.revenue / total_revenue * 100) if total_revenue else 0.0

    return rows[:limit]
