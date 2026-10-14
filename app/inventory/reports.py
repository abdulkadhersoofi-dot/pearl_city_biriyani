"""Stock reports - all read straight off the ledger (StockMovement), so
they always agree with each other and with what was actually posted."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.extensions import db
from app.inventory import ledger
from app.models.inventory import (
    MOVEMENT_TYPE_LABELS,
    MovementType,
    ProductBatch,
    StockLocation,
    StockMovement,
)
from app.models.product import Product
from app.models.purchase import PurchaseBill, PurchaseBillLine, PurchaseStatus

ZERO = Decimal("0")


def stocked_products(tenant_id: int, search: str | None = None, category_id: int | None = None):
    query = Product.query.filter(
        Product.tenant_id == tenant_id, Product.is_active.is_(True),
        Product.is_service.is_(False), Product.has_variants.is_(False),
    )
    if search:
        like = f"%{search}%"
        query = query.filter(db.or_(Product.name.ilike(like), Product.sku.ilike(like), Product.barcode.ilike(like)))
    if category_id:
        query = query.filter(Product.category_id == category_id)
    return query.order_by(Product.name).all()


def is_low(product: Product, qty: Decimal) -> bool:
    return product.min_stock_level is not None and Decimal(qty) <= Decimal(product.min_stock_level)


@dataclass
class SummaryRow:
    product: Product
    qty: Decimal
    cost: Decimal
    value: Decimal
    low: bool


def stock_summary(tenant_id: int, location_id: int | None = None, search: str | None = None,
                  category_id: int | None = None, low_only: bool = False) -> tuple[list[SummaryRow], Decimal]:
    """Every stocked item with its quantity and value (at weighted-average
    cost; stock below zero carries no value)."""
    products = stocked_products(tenant_id, search, category_id)
    on_hand = ledger.stock_on_hand(tenant_id, [p.id for p in products], location_id)
    rows, total = [], ZERO
    for p in products:
        qty = on_hand.get(p.id, ZERO)
        cost = Decimal(p.cost_price or 0)
        value = (qty * cost).quantize(Decimal("0.01")) if qty > 0 else ZERO
        low = is_low(p, qty)
        if low_only and not low:
            continue
        rows.append(SummaryRow(p, qty, cost, value, low))
        total += value
    return rows, total


def low_stock(tenant_id: int) -> list[SummaryRow]:
    """Items at or below their minimum stock level (across all locations)."""
    products = [p for p in stocked_products(tenant_id) if p.min_stock_level is not None]
    on_hand = ledger.stock_on_hand(tenant_id, [p.id for p in products])
    rows = []
    for p in products:
        qty = on_hand.get(p.id, ZERO)
        if is_low(p, qty):
            rows.append(SummaryRow(p, qty, Decimal(p.cost_price or 0), ZERO, True))
    rows.sort(key=lambda r: (r.qty - Decimal(r.product.min_stock_level), r.product.name))
    return rows


@dataclass
class ReorderRow:
    product: Product
    qty: Decimal
    min_level: Decimal
    suggested: Decimal
    supplier_name: str | None
    last_rate: Decimal | None


def reorder_report(tenant_id: int) -> list[ReorderRow]:
    """Low items with how much to order (back up to twice the minimum)
    and who last supplied them, at what rate."""
    rows = []
    for r in low_stock(tenant_id):
        min_level = Decimal(r.product.min_stock_level)
        suggested = max(min_level * 2 - r.qty, min_level, Decimal("1"))
        last = (
            db.session.query(PurchaseBillLine.rate, PurchaseBill.supplier_snapshot)
            .join(PurchaseBill, PurchaseBill.id == PurchaseBillLine.bill_id)
            .filter(PurchaseBill.tenant_id == tenant_id, PurchaseBill.status == PurchaseStatus.POSTED,
                    PurchaseBillLine.product_id == r.product.id)
            .order_by(PurchaseBill.bill_date.desc(), PurchaseBill.id.desc())
            .first()
        )
        rows.append(ReorderRow(r.product, r.qty, min_level, suggested,
                               (last[1] or {}).get("name") if last else None, last[0] if last else None))
    return rows


def location_matrix(tenant_id: int, search: str | None = None):
    """(locations, [(product, [qty per location], total)]) - godown-wise stock."""
    locations = (
        StockLocation.query.filter_by(tenant_id=tenant_id, is_active=True)
        .order_by(StockLocation.is_default.desc(), StockLocation.name)
        .all()
    )
    products = stocked_products(tenant_id, search)
    by_loc = ledger.stock_by_location(tenant_id, [p.id for p in products]) if products else {}
    rows = []
    for p in products:
        qtys = [by_loc.get((p.id, loc.id), ZERO) for loc in locations]
        rows.append((p, qtys, sum(qtys, ZERO)))
    return locations, rows


@dataclass
class LedgerRow:
    movement: StockMovement
    label: str
    qty_in: Decimal
    qty_out: Decimal
    balance: Decimal


def item_ledger(tenant_id: int, product_id: int, location_id: int | None = None,
                date_from: date | None = None, date_to: date | None = None) -> tuple[Decimal, list[LedgerRow], Decimal]:
    """(opening balance, movements with running balance, closing balance)."""
    base = StockMovement.query.filter(StockMovement.tenant_id == tenant_id, StockMovement.product_id == product_id)
    if location_id:
        base = base.filter(StockMovement.location_id == location_id)
    opening = ZERO
    if date_from:
        start = datetime.combine(date_from, time.min)
        opening = Decimal(
            base.with_entities(db.func.coalesce(db.func.sum(StockMovement.qty), 0))
            .filter(StockMovement.moved_at < start).scalar()
        )
        base = base.filter(StockMovement.moved_at >= start)
    if date_to:
        base = base.filter(StockMovement.moved_at < datetime.combine(date_to + timedelta(days=1), time.min))
    rows, balance = [], opening
    for m in base.order_by(StockMovement.moved_at, StockMovement.id).all():
        qty = Decimal(m.qty)
        balance += qty
        label = MOVEMENT_TYPE_LABELS[m.movement_type]
        if m.reversal_of_id:
            label += " (voided)"
        rows.append(LedgerRow(m, label, qty if qty > 0 else ZERO, -qty if qty < 0 else ZERO, balance))
    return opening, rows, balance


@dataclass
class MovementRow:
    product: Product
    sold: Decimal
    on_hand: Decimal
    last_sold: datetime | None
    klass: str


def movement_analysis(tenant_id: int, days: int = 90) -> list[MovementRow]:
    """Fast / slow / dead movers over the last `days` days. Items that sold
    are ranked by quantity sold: the top third are fast, the rest slow.
    Items with stock that didn't sell at all are dead."""
    since = datetime.combine(date.today() - timedelta(days=days), time.min)
    products = stocked_products(tenant_id)
    sold = dict(
        db.session.query(StockMovement.product_id, db.func.sum(-StockMovement.qty))
        .filter(StockMovement.tenant_id == tenant_id,
                StockMovement.movement_type.in_([MovementType.SALE, MovementType.SALE_RETURN]),
                StockMovement.moved_at >= since)
        .group_by(StockMovement.product_id).all()
    )
    last = dict(
        db.session.query(StockMovement.product_id, db.func.max(StockMovement.moved_at))
        .filter(StockMovement.tenant_id == tenant_id, StockMovement.movement_type == MovementType.SALE,
                StockMovement.reversal_of_id.is_(None))
        .group_by(StockMovement.product_id).all()
    )
    on_hand = ledger.stock_on_hand(tenant_id, [p.id for p in products])
    sellers = sorted((p for p in products if Decimal(sold.get(p.id) or 0) > 0),
                     key=lambda p: Decimal(sold[p.id]), reverse=True)
    fast_ids = {p.id for p in sellers[: max(1, (len(sellers) + 2) // 3)]} if sellers else set()
    rows = []
    for p in products:
        qty_sold = Decimal(sold.get(p.id) or 0)
        have = on_hand.get(p.id, ZERO)
        if qty_sold > 0:
            klass = "fast" if p.id in fast_ids else "slow"
        elif have > 0:
            klass = "dead"
        else:
            continue
        rows.append(MovementRow(p, qty_sold, have, last.get(p.id), klass))
    order = {"fast": 0, "slow": 1, "dead": 2}
    rows.sort(key=lambda r: (order[r.klass], -r.sold, r.product.name))
    return rows


@dataclass
class ExpiryRow:
    batch: ProductBatch
    location: StockLocation
    qty: Decimal
    days_left: int | None


def expiry_report(tenant_id: int, within_days: int = 30) -> tuple[list[ExpiryRow], list[ExpiryRow]]:
    """(already expired, expiring within `within_days`) - batches with
    stock left, per location, earliest first."""
    today = date.today()
    rows = (
        db.session.query(ProductBatch, StockLocation, db.func.sum(StockMovement.qty))
        .join(StockMovement, StockMovement.batch_id == ProductBatch.id)
        .join(StockLocation, StockLocation.id == StockMovement.location_id)
        .filter(ProductBatch.tenant_id == tenant_id, ProductBatch.expiry_date.isnot(None),
                ProductBatch.expiry_date <= today + timedelta(days=within_days))
        .group_by(ProductBatch.id, StockLocation.id)
        .all()
    )
    expired, soon = [], []
    for batch, location, qty in rows:
        qty = Decimal(qty)
        if qty <= 0:
            continue
        row = ExpiryRow(batch, location, qty, (batch.expiry_date - today).days)
        (expired if batch.expiry_date < today else soon).append(row)
    key = lambda r: (r.batch.expiry_date, r.batch.product.name)  # noqa: E731
    return sorted(expired, key=key), sorted(soon, key=key)
