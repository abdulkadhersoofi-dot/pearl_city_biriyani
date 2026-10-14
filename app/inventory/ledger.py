"""The stock ledger - one append-only list of every change to stock
(app.models.inventory.StockMovement), for every client.

Rules:
  - Stock on hand at a place = the sum of its movements. Nothing else
    stores a stock number, so nothing can drift.
  - Movements are never edited or deleted. Voiding a document posts the
    opposite of each of its movements, pointing back at it.
  - Every sale, return, purchase, transfer and adjustment posts here,
    whether or not the client has switched the Inventory module on, so
    that the history is complete the day they do (they then set their
    real counts with a physical-count adjustment). Only the screens and
    the "can't sell below zero" rule depend on the module.
  - Goods coming in update the item's weighted-average cost
    (Product.cost_price); goods going out are valued at it.

The restaurant's daily branch counter (app.pos.stock, BranchStockAllocation)
keeps working exactly as before; branch invoices and branch sales post
here as well, as transfers into and sales out of the branch's location.
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from app.extensions import db
from app.models.inventory import LocationKind, MovementType, ProductBatch, StockLocation, StockMovement
from app.models.mixins import utcnow
from app.models.product import Product

ZERO = Decimal("0")
QTY = Decimal("0.001")
COST = Decimal("0.0001")
DEFAULT_LOCATION_NAME = "Main store"


class StockError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- locations -----------------------------------------------------------------

def default_location(tenant_id: int) -> StockLocation:
    location = StockLocation.query.filter_by(tenant_id=tenant_id, is_default=True).first()
    if location is None:
        location = StockLocation(tenant_id=tenant_id, name=_free_name(tenant_id, DEFAULT_LOCATION_NAME),
                                 kind=LocationKind.STORE, is_default=True)
        db.session.add(location)
        db.session.flush()
    return location


def branch_location(tenant_id: int, user) -> StockLocation:
    location = StockLocation.query.filter_by(tenant_id=tenant_id, branch_user_id=user.id).first()
    if location is None:
        location = StockLocation(tenant_id=tenant_id, name=_free_name(tenant_id, user.name or f"Branch {user.id}"),
                                 kind=LocationKind.BRANCH, branch_user_id=user.id)
        db.session.add(location)
        db.session.flush()
    return location


def location_for_user(tenant_id: int, user) -> StockLocation:
    """Where a login's counter sells from: a branch login's own location,
    everyone else the main store."""
    if user is not None and getattr(user, "is_staff", False):
        return branch_location(tenant_id, user)
    return default_location(tenant_id)


def _free_name(tenant_id: int, wanted: str) -> str:
    name, n = wanted[:100], 2
    while StockLocation.query.filter_by(tenant_id=tenant_id, name=name).first() is not None:
        name = f"{wanted[:94]} ({n})"
        n += 1
    return name


# --- reading stock ---------------------------------------------------------------

def stock_on_hand(tenant_id: int, product_ids=None, location_id: int | None = None) -> dict[int, Decimal]:
    """product_id -> quantity on hand (one location, or all of them)."""
    query = db.session.query(StockMovement.product_id, db.func.coalesce(db.func.sum(StockMovement.qty), 0)).filter(
        StockMovement.tenant_id == tenant_id
    )
    if product_ids is not None:
        product_ids = list(product_ids)
        if not product_ids:
            return {}
        query = query.filter(StockMovement.product_id.in_(product_ids))
    if location_id is not None:
        query = query.filter(StockMovement.location_id == location_id)
    return {pid: Decimal(qty) for pid, qty in query.group_by(StockMovement.product_id).all()}


def stock_by_location(tenant_id: int, product_ids=None) -> dict[tuple[int, int], Decimal]:
    """(product_id, location_id) -> quantity on hand."""
    query = db.session.query(
        StockMovement.product_id, StockMovement.location_id, db.func.sum(StockMovement.qty)
    ).filter(StockMovement.tenant_id == tenant_id)
    if product_ids is not None:
        query = query.filter(StockMovement.product_id.in_(list(product_ids)))
    rows = query.group_by(StockMovement.product_id, StockMovement.location_id).all()
    return {(pid, lid): Decimal(qty) for pid, lid, qty in rows}


def batch_stock(tenant_id: int, product_id: int, location_id: int | None = None) -> list[tuple[ProductBatch, Decimal]]:
    """[(batch, qty on hand)] for one item, earliest expiry first (FEFO
    order); batches with nothing left are left out."""
    query = (
        db.session.query(ProductBatch, db.func.sum(StockMovement.qty))
        .join(StockMovement, StockMovement.batch_id == ProductBatch.id)
        .filter(ProductBatch.tenant_id == tenant_id, ProductBatch.product_id == product_id)
    )
    if location_id is not None:
        query = query.filter(StockMovement.location_id == location_id)
    rows = query.group_by(ProductBatch.id).all()
    rows = [(batch, Decimal(qty)) for batch, qty in rows if Decimal(qty) > 0]
    rows.sort(key=lambda r: (r[0].expiry_date or date.max, r[0].id))
    return rows


# --- writing stock ---------------------------------------------------------------

def post(
    tenant_id: int,
    product_id: int,
    location_id: int,
    qty,
    movement_type: MovementType,
    source_type: str,
    source_id: int,
    *,
    source_line_id: int | None = None,
    unit_cost=None,
    batch_id: int | None = None,
    serial_id: int | None = None,
    user_id: int | None = None,
    note: str | None = None,
    reversal_of_id: int | None = None,
) -> StockMovement:
    qty = Decimal(qty).quantize(QTY, rounding=ROUND_HALF_UP)
    movement = StockMovement(
        tenant_id=tenant_id,
        product_id=product_id,
        location_id=location_id,
        qty=qty,
        movement_type=movement_type,
        source_type=source_type,
        source_id=source_id,
        source_line_id=source_line_id,
        unit_cost=Decimal(unit_cost or 0).quantize(COST, rounding=ROUND_HALF_UP),
        batch_id=batch_id,
        serial_id=serial_id,
        moved_at=utcnow(),
        created_by_id=user_id,
        note=(note or None) and note[:255],
        reversal_of_id=reversal_of_id,
    )
    db.session.add(movement)
    return movement


def reverse_source(tenant_id: int, source_type: str, source_id: int, user_id: int | None, note: str) -> int:
    """Posts the opposite of every not-yet-reversed movement a document
    made. Returns how many were reversed. Safe to call twice."""
    movements = StockMovement.query.filter_by(tenant_id=tenant_id, source_type=source_type, source_id=source_id).all()
    already = {m.reversal_of_id for m in movements if m.reversal_of_id}
    count = 0
    for m in movements:
        if m.reversal_of_id or m.id in already:
            continue
        post(
            tenant_id, m.product_id, m.location_id, -m.qty, m.movement_type, source_type, source_id,
            source_line_id=m.source_line_id, unit_cost=m.unit_cost, batch_id=m.batch_id, serial_id=m.serial_id,
            user_id=user_id, note=note, reversal_of_id=m.id,
        )
        count += 1
    db.session.flush()
    return count


def receive_cost(product: Product, qty, unit_cost) -> None:
    """Weighted-average cost on goods coming in: what the stock already
    on hand cost plus what this lot cost, over the new total. Stock at
    or below zero contributes nothing (its cost is unknown)."""
    qty, unit_cost = Decimal(qty), Decimal(unit_cost or 0)
    if qty <= 0:
        return
    on_hand = stock_on_hand(product.tenant_id, [product.id]).get(product.id, ZERO)
    old_cost = Decimal(product.cost_price or 0)
    if on_hand <= 0 or old_cost <= 0:
        new_cost = unit_cost
    else:
        new_cost = (on_hand * old_cost + qty * unit_cost) / (on_hand + qty)
    product.cost_price = new_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def unreceive_cost(product: Product, qty, unit_cost) -> None:
    """Undo receive_cost for goods going back out at the cost they came
    in at (a voided purchase, a purchase return)."""
    qty, unit_cost = Decimal(qty), Decimal(unit_cost or 0)
    on_hand = stock_on_hand(product.tenant_id, [product.id]).get(product.id, ZERO)
    remaining = on_hand - qty
    old_cost = Decimal(product.cost_price or 0)
    if remaining > 0 and on_hand > 0:
        new_cost = (on_hand * old_cost - qty * unit_cost) / remaining
        if new_cost > 0:
            product.cost_price = new_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def issue_cost(product: Product) -> Decimal:
    return Decimal(product.cost_price or 0)


def sells_from_stock(product: Product | None) -> bool:
    """A real, stocked item - not a service, not a variant parent."""
    return product is not None and not product.is_service and not product.has_variants


def check_available(tenant, location_id: int, wanted: dict[int, Decimal], products: dict[int, Product]) -> None:
    """With Inventory on, refuse to take an item below zero when it's set
    not to allow that. With Inventory off, nothing is ever refused."""
    if not tenant.has_feature("inventory"):
        return
    strict = {pid: qty for pid, qty in wanted.items() if pid in products and sells_from_stock(products[pid])
              and not products[pid].allow_negative_stock}
    if not strict:
        return
    on_hand = stock_on_hand(tenant.id, strict.keys(), location_id)
    for pid, qty in strict.items():
        have = on_hand.get(pid, ZERO)
        if have < qty:
            name = products[pid].name
            raise StockError(
                f"Only {_fmt(have)} {products[pid].unit} of {name} in stock here - can't sell {_fmt(qty)}."
                if have > 0 else f"{name} is out of stock here."
            )


def _fmt(qty: Decimal) -> str:
    return f"{Decimal(qty).normalize():f}"
