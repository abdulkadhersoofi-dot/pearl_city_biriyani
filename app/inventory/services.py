"""Stock documents a client posts by hand: adjustments (opening stock,
physical count, damage, wastage, found) and transfers between their own
locations (with a Delivery Challan). Both are numbered, never deleted,
voided with a reason - and move stock only through the ledger."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.inventory import ledger
from app.models.inventory import (
    AdjustmentReason,
    LocationKind,
    MovementType,
    ProductBatch,
    StockAdjustment,
    StockAdjustmentLine,
    StockDocStatus,
    StockLocation,
    StockTransfer,
    StockTransferLine,
)
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.product import Product
from app.utils.financial_year import financial_year_for
from app.utils.numbering import allocate_number, get_or_create_series

ZERO = Decimal("0")

_REASON_MOVEMENT = {
    AdjustmentReason.OPENING: MovementType.OPENING,
    AdjustmentReason.COUNT: MovementType.ADJUSTMENT,
    AdjustmentReason.DAMAGE: MovementType.DAMAGE,
    AdjustmentReason.WASTAGE: MovementType.WASTAGE,
    AdjustmentReason.FOUND: MovementType.FOUND,
    AdjustmentReason.OTHER: MovementType.ADJUSTMENT,
}
# Reasons where the quantity entered is always taken out of stock.
_OUT_REASONS = {AdjustmentReason.DAMAGE, AdjustmentReason.WASTAGE}


@dataclass
class StockLineInput:
    product_id: int
    qty: Decimal
    unit_cost: Decimal | None = None
    batch_no: str | None = None
    expiry_date: date | None = None
    mfg_date: date | None = None
    mrp: Decimal | None = None


def parse_stock_lines(product_ids, qtys, costs=(), batch_nos=(), expiries=()) -> list[StockLineInput]:
    lines = []
    for i, pid in enumerate(product_ids):
        if not pid or not (qtys[i] if i < len(qtys) else "").strip():
            continue
        try:
            qty = Decimal(qtys[i].strip())
            cost = Decimal(costs[i].strip()) if i < len(costs) and costs[i].strip() else None
            product_id = int(pid)
        except (InvalidOperation, ValueError):
            raise ledger.StockError("Quantities and costs must be numbers.")
        expiry = None
        if i < len(expiries) and expiries[i]:
            try:
                expiry = date.fromisoformat(expiries[i])
            except ValueError:
                raise ledger.StockError("An expiry date isn't a valid date.")
        lines.append(StockLineInput(product_id, qty, cost, (batch_nos[i].strip() if i < len(batch_nos) else "") or None, expiry))
    if not lines:
        raise ledger.StockError("Add at least one item.")
    return lines


def _primary_gstin(tenant):
    gstin = tenant.gstins.filter_by(is_active=True).order_by(db.desc("is_primary")).first()
    if gstin is None:
        raise ledger.StockError("This client has no business location on file.")
    return gstin


def _location(tenant, location_id) -> StockLocation:
    location = StockLocation.query.filter_by(id=location_id, tenant_id=tenant.id, is_active=True).first()
    if location is None:
        raise ledger.StockError("Pick a valid location.")
    return location


def _products(tenant, ids) -> dict[int, Product]:
    products = {p.id: p for p in Product.query.filter(Product.id.in_(set(ids)), Product.tenant_id == tenant.id).all()}
    for pid in ids:
        product = products.get(pid)
        if product is None:
            raise ledger.StockError("An item picked isn't in your catalog.")
        if not ledger.sells_from_stock(product):
            raise ledger.StockError(f"{product.name} isn't a stocked item (a service, or a design with variants).")
    return products


def get_or_create_batch(tenant_id: int, product: Product, batch_no: str, expiry_date=None, mfg_date=None, mrp=None) -> ProductBatch:
    batch_no = batch_no.strip()[:50]
    batch = ProductBatch.query.filter_by(tenant_id=tenant_id, product_id=product.id, batch_no=batch_no).first()
    if batch is None:
        batch = ProductBatch(tenant_id=tenant_id, product_id=product.id, batch_no=batch_no,
                             expiry_date=expiry_date, mfg_date=mfg_date, mrp=mrp)
        db.session.add(batch)
        db.session.flush()
    else:
        batch.expiry_date = batch.expiry_date or expiry_date
        batch.mfg_date = batch.mfg_date or mfg_date
        batch.mrp = batch.mrp or mrp
    return batch


# --- adjustments ---------------------------------------------------------------------

def create_adjustment(tenant, user, location_id, reason: AdjustmentReason, lines: list[StockLineInput],
                     note: str | None = None, adjusted_on: date | None = None) -> StockAdjustment:
    location = _location(tenant, location_id)
    products = _products(tenant, [l.product_id for l in lines])
    adjusted_on = adjusted_on or date.today()
    batches_on = tenant.has_feature("batch_expiry")
    if reason == AdjustmentReason.OTHER and not (note or "").strip():
        raise ledger.StockError("Say what the correction is for.")

    on_hand = ledger.stock_on_hand(tenant.id, products.keys(), location.id)
    planned = []
    for line in lines:
        product = products[line.product_id]
        if reason == AdjustmentReason.COUNT:
            if line.qty < 0:
                raise ledger.StockError("A counted quantity can't be negative.")
            delta, counted = line.qty - on_hand.get(product.id, ZERO), line.qty
        elif reason in _OUT_REASONS:
            if line.qty <= 0:
                raise ledger.StockError("Enter how many were damaged or wasted (more than 0).")
            delta, counted = -line.qty, None
        elif reason in (AdjustmentReason.OPENING, AdjustmentReason.FOUND):
            if line.qty <= 0:
                raise ledger.StockError("Enter how many (more than 0).")
            delta, counted = line.qty, None
        else:
            if line.qty == 0:
                raise ledger.StockError("A correction of 0 changes nothing.")
            delta, counted = line.qty, None
        planned.append((line, product, delta, counted))

    gstin = _primary_gstin(tenant)
    series = get_or_create_series(tenant.id, gstin.id, DocumentType.STOCK_ADJUSTMENT, financial_year_for(adjusted_on))
    adjustment = StockAdjustment(
        tenant_id=tenant.id, invoice_series_id=series.id, number=allocate_number(series), location_id=location.id,
        adjusted_on=adjusted_on, reason=reason, note=(note or "").strip() or None, created_by_id=user.id,
    )
    db.session.add(adjustment)
    db.session.flush()
    movement_type = _REASON_MOVEMENT[reason]
    for line, product, delta, counted in planned:
        batch = None
        if batches_on and product.track_batch and line.batch_no:
            batch = get_or_create_batch(tenant.id, product, line.batch_no, line.expiry_date)
        if delta > 0:
            cost = line.unit_cost if line.unit_cost is not None else ledger.issue_cost(product)
            ledger.receive_cost(product, delta, cost)
        else:
            cost = ledger.issue_cost(product)
        adj_line = StockAdjustmentLine(adjustment_id=adjustment.id, product_id=product.id, qty=delta,
                                       counted_qty=counted, unit_cost=cost, batch_id=batch.id if batch else None)
        db.session.add(adj_line)
        db.session.flush()
        if delta != 0:
            ledger.post(tenant.id, product.id, location.id, delta, movement_type, "adjustment", adjustment.id,
                        source_line_id=adj_line.id, unit_cost=cost, batch_id=batch.id if batch else None,
                        user_id=user.id, note=adjustment.note)
    db.session.flush()
    return adjustment


def void_adjustment(adjustment: StockAdjustment, user, reason: str) -> None:
    if adjustment.status != StockDocStatus.COMPLETED:
        raise ledger.StockError("This adjustment is already voided.")
    if not (reason or "").strip():
        raise ledger.StockError("A reason is required to void it.")
    ledger.reverse_source(adjustment.tenant_id, "adjustment", adjustment.id, user.id, f"{adjustment.number} voided")
    adjustment.status = StockDocStatus.VOIDED
    adjustment.void_reason = reason.strip()
    adjustment.voided_at = utcnow()
    adjustment.voided_by_id = user.id


# --- transfers ------------------------------------------------------------------------

def create_transfer(tenant, user, from_location_id, to_location_id, lines: list[StockLineInput],
                    transfer_date: date | None = None, note: str | None = None, vehicle_no: str | None = None) -> StockTransfer:
    source = _location(tenant, from_location_id)
    dest = _location(tenant, to_location_id)
    if source.id == dest.id:
        raise ledger.StockError("Pick two different locations.")
    products = _products(tenant, [l.product_id for l in lines])
    for line in lines:
        if line.qty <= 0:
            raise ledger.StockError("Transfer quantities must be more than 0.")
    wanted: dict[int, Decimal] = {}
    for line in lines:
        wanted[line.product_id] = wanted.get(line.product_id, ZERO) + line.qty
    # Goods can't leave a place that doesn't have them, when the item is
    # set to stop at zero.
    ledger.check_available(tenant, source.id, wanted, products)

    transfer_date = transfer_date or date.today()
    gstin = _primary_gstin(tenant)
    series = get_or_create_series(tenant.id, gstin.id, DocumentType.STOCK_TRANSFER, financial_year_for(transfer_date))
    transfer = StockTransfer(
        tenant_id=tenant.id, gstin_id=gstin.id, invoice_series_id=series.id, number=allocate_number(series),
        from_location_id=source.id, to_location_id=dest.id, transfer_date=transfer_date,
        note=(note or "").strip() or None, vehicle_no=(vehicle_no or "").strip()[:30] or None, created_by_id=user.id,
    )
    db.session.add(transfer)
    db.session.flush()
    batches_on = tenant.has_feature("batch_expiry")
    for line in lines:
        product = products[line.product_id]
        cost = ledger.issue_cost(product)
        portions = [(None, line.qty)]
        if batches_on and product.track_batch:
            portions, remaining = [], line.qty
            for batch, qty in ledger.batch_stock(tenant.id, product.id, source.id):
                if remaining <= 0:
                    break
                take = min(qty, remaining)
                portions.append((batch, take))
                remaining -= take
            if remaining > 0:
                portions.append((None, remaining))
        for batch, qty in portions:
            t_line = StockTransferLine(transfer_id=transfer.id, product_id=product.id, qty=qty, unit_cost=cost,
                                       batch_id=batch.id if batch else None)
            db.session.add(t_line)
            db.session.flush()
            for loc, sign, mtype in ((source, -1, MovementType.TRANSFER_OUT), (dest, 1, MovementType.TRANSFER_IN)):
                ledger.post(tenant.id, product.id, loc.id, sign * qty, mtype, "transfer", transfer.id,
                            source_line_id=t_line.id, unit_cost=cost, batch_id=batch.id if batch else None, user_id=user.id)
    db.session.flush()
    return transfer


def void_transfer(transfer: StockTransfer, user, reason: str) -> None:
    if transfer.status != StockDocStatus.COMPLETED:
        raise ledger.StockError("This transfer is already voided.")
    if not (reason or "").strip():
        raise ledger.StockError("A reason is required to void it.")
    ledger.reverse_source(transfer.tenant_id, "transfer", transfer.id, user.id, f"{transfer.number} voided")
    transfer.status = StockDocStatus.VOIDED
    transfer.void_reason = reason.strip()
    transfer.voided_at = utcnow()
    transfer.voided_by_id = user.id


# --- locations --------------------------------------------------------------------------

def create_location(tenant, name: str, kind: LocationKind, address: str | None = None) -> StockLocation:
    name = (name or "").strip()[:100]
    if not name:
        raise ledger.StockError("Give the location a name.")
    if StockLocation.query.filter(StockLocation.tenant_id == tenant.id, db.func.lower(StockLocation.name) == name.lower()).first():
        raise ledger.StockError(f"There's already a location called {name}.")
    if kind == LocationKind.BRANCH:
        raise ledger.StockError("Branch locations are created automatically for each branch login.")
    ledger.default_location(tenant.id)  # the main store always exists first
    location = StockLocation(tenant_id=tenant.id, name=name, kind=kind, address=(address or "").strip() or None)
    db.session.add(location)
    db.session.flush()
    return location


def deactivate_location(location: StockLocation) -> None:
    if location.is_default:
        raise ledger.StockError("The main store can't be switched off.")
    left = {pid: q for pid, q in ledger.stock_on_hand(location.tenant_id, None, location.id).items() if q != 0}
    if left:
        raise ledger.StockError("Move or adjust the stock still there first - a location must be empty to switch it off.")
    location.is_active = False
