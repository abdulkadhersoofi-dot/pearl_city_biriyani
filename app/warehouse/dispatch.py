"""Warehouse dispatch: the one service the website, the ARFA Warehouse app's
API and the invoice all go through (module "warehouse_dispatch").

  * Reservations: a warehouse's free quantity = what is in stock there -
    what open, un-invoiced sales orders shipping from it still hold.
    Saving an order (or moving it to another warehouse) can't take more
    than is free at the chosen warehouse.
  * Claims: any picker of the order's warehouse may take it; the claim is
    one conditional UPDATE, so of two phones only one ever wins. A claim
    with no scan for IDLE_RELEASE_MINUTES goes back to the pipeline, its
    scans undone.
  * Picks: every scan is checked here, with the serial's row locked, and
    kept as a SerialPick whatever the result. A serial that was never
    entered in ARFA is added to stock and picked when the picker confirms
    the product on the box is the one on the order (allow_new).
  * Confirm: only when every unit of every line is picked; the order then
    waits at the warehouse computer to be invoiced (app/warehouse/routes).
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.inventory import ledger
from app.models.inventory import LocationKind, ProductSerial, SerialStatus, StockLocation
from app.models.mixins import utcnow
from app.models.product import Product
from app.models.sales import SalesDocument, SalesDocumentKind, SalesDocumentLine, SalesDocumentStatus
from app.models.warehouse import (
    EMI_HOLD, EMI_READY, HOLDING, RACK_PREFIX, DeliveryScan, DispatchStatus, PickMethod, PickResult, SerialPick,
    SerialSwap, SwapStatus, WarehouseLogin,
)
from app.warehouse import notify

IDLE_RELEASE_MINUTES = 30
ALLOWED_SERIALS_CAP = 2000
ZERO = Decimal("0")


class DispatchError(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


def is_on(tenant) -> bool:
    return tenant is not None and tenant.has_feature("warehouse_dispatch")


# --- warehouses and what is free there ----------------------------------------------------------

def warehouses(tenant_id: int) -> list[StockLocation]:
    return (StockLocation.query.filter_by(tenant_id=tenant_id, kind=LocationKind.GODOWN, is_active=True)
            .order_by(StockLocation.name).all())


def resolve_warehouse(tenant, location_id) -> StockLocation:
    try:
        location_id = int(location_id)
    except (TypeError, ValueError):
        raise DispatchError("Pick the warehouse this order ships from.")
    location = StockLocation.query.filter_by(id=location_id, tenant_id=tenant.id).first()
    if location is None or location.kind != LocationKind.GODOWN or not location.is_active:
        raise DispatchError("Pick one of your active warehouses (godowns) for this order.")
    return location


def default_warehouse(tenant_id: int, user) -> StockLocation | None:
    """The warehouse an order made by this login ships from unless changed:
    its branch's (or the main store's) usual one, else the only warehouse."""
    home = ledger.location_for_user(tenant_id, user)
    usual = home.default_dispatch_location
    if usual is not None and usual.is_active and usual.kind == LocationKind.GODOWN:
        return usual
    all_ = warehouses(tenant_id)
    return all_[0] if len(all_) == 1 else None


def held(tenant_id: int, product_ids, location_id: int | None = None, exclude_doc_id: int | None = None
         ) -> dict[tuple[int, int], Decimal]:
    """(product_id, location_id) -> quantity open sales orders hold there."""
    product_ids = [p for p in product_ids if p]
    if not product_ids:
        return {}
    query = (db.session.query(SalesDocumentLine.product_id, SalesDocument.dispatch_location_id,
                              func.coalesce(func.sum(SalesDocumentLine.qty), 0))
             .join(SalesDocument, SalesDocumentLine.document_id == SalesDocument.id)
             .filter(SalesDocument.tenant_id == tenant_id, SalesDocument.kind == SalesDocumentKind.SALES_ORDER,
                     SalesDocument.status == SalesDocumentStatus.OPEN,
                     SalesDocument.dispatch_status.in_(HOLDING), SalesDocument.dispatch_location_id.isnot(None),
                     SalesDocumentLine.product_id.in_(product_ids)))
    if location_id is not None:
        query = query.filter(SalesDocument.dispatch_location_id == location_id)
    if exclude_doc_id is not None:
        query = query.filter(SalesDocument.id != exclude_doc_id)
    rows = query.group_by(SalesDocumentLine.product_id, SalesDocument.dispatch_location_id).all()
    return {(pid, lid): Decimal(q) for pid, lid, q in rows}


def free_by_warehouse(tenant_id: int, product_ids, exclude_doc_id: int | None = None) -> list[dict]:
    """For the Warehouse picker: each warehouse with, per product, what is in
    stock there, what orders hold and what is free."""
    product_ids = [int(p) for p in product_ids if p]
    stock = ledger.stock_by_location(tenant_id, product_ids) if product_ids else {}
    holds = held(tenant_id, product_ids, exclude_doc_id=exclude_doc_id)
    out = []
    for loc in warehouses(tenant_id):
        items = {}
        for pid in product_ids:
            have = stock.get((pid, loc.id), ZERO)
            hold = holds.get((pid, loc.id), ZERO)
            items[pid] = {"stock": have, "held": hold, "free": have - hold}
        out.append({"location": loc, "items": items})
    return out


def _wanted(lines) -> dict[int, Decimal]:
    wanted: dict[int, Decimal] = {}
    for line in lines:
        pid = getattr(line, "product_id", None)
        if pid:
            wanted[pid] = wanted.get(pid, ZERO) + Decimal(line.qty)
    return wanted


def check_free(tenant, location: StockLocation, lines, exclude_doc_id: int | None = None) -> None:
    """Refuse an order that would take more than is free at its warehouse.
    Serial-tracked items are always held to it (a unit can't be shipped
    twice); other items only when they're set not to go below zero."""
    if not tenant.has_feature("inventory"):
        return
    wanted = _wanted(lines)
    if not wanted:
        return
    products = {p.id: p for p in Product.query.filter(Product.id.in_(wanted.keys())).all()}
    strict = {pid: q for pid, q in wanted.items() if ledger.sells_from_stock(products.get(pid))
              and (products[pid].track_serial or not products[pid].allow_negative_stock)}
    if not strict:
        return
    on_hand = ledger.stock_on_hand(tenant.id, strict.keys(), location.id)
    holds = held(tenant.id, strict.keys(), location.id, exclude_doc_id)
    for pid, qty in strict.items():
        have = on_hand.get(pid, ZERO)
        hold = holds.get((pid, location.id), ZERO)
        free = have - hold
        if free < qty:
            name = products[pid].name
            others = f", {ledger._fmt(hold)} held for other orders" if hold > 0 else ""
            raise DispatchError(
                f"Only {ledger._fmt(max(free, ZERO))} of {name} free at {location.name} "
                f"({ledger._fmt(have)} in stock{others}) - can't take {ledger._fmt(qty)}. "
                "Pick another warehouse or lower the quantity.")


# --- an order on its way -------------------------------------------------------------------------

def _branch_label(doc: SalesDocument) -> str:
    user = doc.created_by
    if user is not None and getattr(user, "is_staff", False):
        return user.name or "Branch"
    return "Office"


def start(tenant, doc: SalesDocument, location: StockLocation) -> None:
    """A new sales order: held at its warehouse and sent to every picker there."""
    check_free(tenant, location, doc.lines, exclude_doc_id=doc.id)
    doc.dispatch_location_id = location.id
    doc.dispatch_status = DispatchStatus.TO_PICK
    doc.dispatch_activity_at = utcnow()
    if doc.is_finance_sale:
        doc.priority = "high"
    db.session.flush()
    notify.new_order(doc, location, _branch_label(doc))


def change_warehouse(tenant, doc: SalesDocument, location: StockLocation) -> StockLocation | None:
    """Move an open, unclaimed order to another warehouse (its hold moves
    with it; the new warehouse is told, the old one's phones refresh)."""
    if doc.status != SalesDocumentStatus.OPEN or doc.dispatch_status != DispatchStatus.TO_PICK \
            or doc.claimed_by_id is not None:
        raise DispatchError("A picker has already taken this order - the warehouse can't change now.", 409)
    old = doc.dispatch_location
    if old is not None and old.id == location.id:
        return None
    check_free(tenant, location, doc.lines, exclude_doc_id=doc.id)
    doc.dispatch_location_id = location.id
    doc.dispatch_activity_at = utcnow()
    db.session.flush()
    if old is not None:
        notify.refresh(doc, old.id, "moved")
    notify.new_order(doc, location, _branch_label(doc))
    return old


def lock(doc_id: int, tenant_id: int) -> SalesDocument | None:
    return (SalesDocument.query.filter_by(id=doc_id, tenant_id=tenant_id)
            .populate_existing().with_for_update().first())


def claim(doc: SalesDocument, login: WarehouseLogin) -> None:
    auto_release(doc.tenant_id, doc.dispatch_location_id)
    now = utcnow()
    result = db.session.execute(
        update(SalesDocument)
        .where(SalesDocument.id == doc.id, SalesDocument.tenant_id == login.tenant_id,
               SalesDocument.claimed_by_id.is_(None), SalesDocument.dispatch_status == DispatchStatus.TO_PICK,
               SalesDocument.status == SalesDocumentStatus.OPEN)
        .values(claimed_by_id=login.id, claimed_at=now, dispatch_activity_at=now,
                dispatch_status=DispatchStatus.PICKING)
        .execution_options(synchronize_session=False))
    db.session.refresh(doc)
    if result.rowcount != 1:
        if doc.claimed_by_id == login.id and doc.dispatch_status == DispatchStatus.PICKING:
            return                                               # already theirs (a retried tap)
        who = doc.claimed_by.name if doc.claimed_by is not None else None
        if doc.status != SalesDocumentStatus.OPEN:
            raise DispatchError("This order was cancelled.", 409)
        raise DispatchError(f"Already taken by {who}." if who else "This order isn't waiting to be picked.", 409,
                            claimed_by=who)
    notify.refresh(doc, doc.dispatch_location_id, "claimed")


def _undo(pick: SerialPick, reason: str) -> None:
    pick.undone_at = utcnow()
    pick.undone_reason = reason[:60]
    unit = pick.serial
    if unit is None or pick.result not in (PickResult.OK, PickResult.ADDED):
        return
    if unit.status == SerialStatus.PICKED and unit.picked_sales_line_id == pick.sales_line_id:
        if unit.added_at_pick and pick.result == PickResult.ADDED:
            # Never in ARFA before this scan: undoing it takes it out again.
            SerialPick.query.filter_by(serial_id=unit.id).update({SerialPick.serial_id: None},
                                                                  synchronize_session=False)
            db.session.delete(unit)
            return
        unit.status = SerialStatus.IN_STOCK
        unit.picked_sales_line_id = None
        unit.picked_by_id = None
        unit.picked_at = None
        unit.pick_photo_id = None
        if unit.bin_code == EMI_HOLD:
            unit.bin_code = None


def good_picks(doc: SalesDocument) -> list[SerialPick]:
    return (SerialPick.query.filter(SerialPick.sales_document_id == doc.id, SerialPick.undone_at.is_(None),
                                    SerialPick.result.in_((PickResult.OK, PickResult.ADDED)))
            .order_by(SerialPick.id).all())


def release(doc: SalesDocument, reason: str) -> None:
    """Back to the pipeline: the claim is dropped and every scan undone."""
    if doc.dispatch_status not in (DispatchStatus.PICKING, DispatchStatus.PICKED) or doc.converted_invoice_id:
        raise DispatchError("This order isn't being picked (it may be invoiced already).", 409)
    for pick in good_picks(doc):
        _undo(pick, reason)
    doc.claimed_by_id = None
    doc.claimed_at = None
    doc.picked_confirmed_at = None
    doc.picked_confirmed_by_id = None
    doc.dispatch_status = DispatchStatus.TO_PICK
    doc.dispatch_activity_at = utcnow()
    db.session.flush()
    notify.refresh(doc, doc.dispatch_location_id, "released")


def reassign(doc: SalesDocument, login: WarehouseLogin) -> None:
    """A supervisor hands a claimed order to another picker (scans kept)."""
    if doc.dispatch_status != DispatchStatus.PICKING:
        raise DispatchError("Only an order being picked can be handed to someone else.", 409)
    if not login.is_active or doc.dispatch_location_id not in login.location_ids:
        raise DispatchError(f"{login.name} doesn't work in {doc.dispatch_location.name}.")
    doc.claimed_by_id = login.id
    doc.claimed_at = utcnow()
    doc.dispatch_activity_at = utcnow()
    db.session.flush()
    notify.refresh(doc, doc.dispatch_location_id, "claimed")


def cancelled(doc: SalesDocument) -> None:
    """The order was cancelled: units picked for it go back, phones refresh."""
    if doc.dispatch_status is None:
        return
    for pick in good_picks(doc):
        _undo(pick, "order cancelled")
    location_id = doc.dispatch_location_id
    doc.claimed_by_id = None
    doc.dispatch_status = None
    db.session.flush()
    if location_id:
        notify.refresh(doc, location_id, "cancelled")


def idle_minutes(doc: SalesDocument, now=None) -> int:
    last = doc.dispatch_activity_at or doc.claimed_at
    if last is None:
        return 0
    return int(((now or utcnow()) - last).total_seconds() // 60)


def auto_release(tenant_id: int, location_id: int | None = None, now=None) -> list[SalesDocument]:
    """Claims with no scan for IDLE_RELEASE_MINUTES go back to the pipeline."""
    cutoff = (now or utcnow()) - timedelta(minutes=IDLE_RELEASE_MINUTES)
    query = SalesDocument.query.filter(SalesDocument.tenant_id == tenant_id,
                                       SalesDocument.dispatch_status == DispatchStatus.PICKING,
                                       SalesDocument.dispatch_activity_at < cutoff)
    if location_id is not None:
        query = query.filter(SalesDocument.dispatch_location_id == location_id)
    released = []
    for doc in query.with_for_update(skip_locked=True).all():
        release(doc, f"idle {IDLE_RELEASE_MINUTES} min")
        released.append(doc)
    return released


# --- scanning ------------------------------------------------------------------------------------

def normalise(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).upper()[:64]


def compact(text: str) -> str:
    """Spaces and dashes out, upper case, O/0 I/1 S/5 folded - for matching only."""
    out = re.sub(r"[\s\-_/.:]", "", (text or "").upper())
    return out.translate(str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5"}))


def find_serial(tenant_id: int, text: str, lock_row: bool = True) -> ProductSerial | None:
    norm = normalise(text)
    if not norm:
        return None
    query = ProductSerial.query.filter(ProductSerial.tenant_id == tenant_id,
                                       func.upper(ProductSerial.serial_no) == norm)
    if lock_row:
        query = query.with_for_update()
    return query.first()


def line_progress(doc: SalesDocument) -> dict[int, dict]:
    picks = good_picks(doc)
    out = {}
    for line in doc.lines:
        mine = [p for p in picks if p.sales_line_id == line.id]
        out[line.id] = {"qty": int(Decimal(line.qty)), "picked": len(mine),
                        "serials": [p.serial_no for p in mine if p.serial_no], "picks": mine}
    return out


def needs_scan(line: SalesDocumentLine) -> bool:
    return line.product is not None and ledger.sells_from_stock(line.product)


def is_complete(doc: SalesDocument) -> bool:
    progress = line_progress(doc)
    return all(progress[l.id]["picked"] >= progress[l.id]["qty"] for l in doc.lines if needs_scan(l))


def _require_claimer(doc: SalesDocument, login: WarehouseLogin) -> None:
    if doc.status != SalesDocumentStatus.OPEN:
        raise DispatchError("This order was cancelled.", 409)
    if doc.claimed_by_id != login.id or doc.dispatch_status not in (DispatchStatus.PICKING, DispatchStatus.PICKED):
        raise DispatchError("Only the picker who took this order can scan for it - tap Start picking first.", 403)


def pick(doc: SalesDocument, line: SalesDocumentLine, login: WarehouseLogin, *, text: str, method: PickMethod,
         client_pick_id: str | None = None, allow_new: bool = False, session_id: int | None = None,
         photo_id: int | None = None) -> tuple[SerialPick, str]:
    """One scan against one order line. Returns (the kept SerialPick, a message
    for the picker). The same client_pick_id twice is the same pick."""
    client_pick_id = (client_pick_id or "").strip()[:64] or None
    if client_pick_id:
        seen = SerialPick.query.filter_by(tenant_id=doc.tenant_id, client_pick_id=client_pick_id).first()
        if seen is not None:
            return seen, message_for(seen)
    _require_claimer(doc, login)
    if line.document_id != doc.id:
        raise DispatchError("That line isn't on this order.", 404)
    product = line.product
    if product is None or not needs_scan(line):
        raise DispatchError("This line has nothing to pick from stock.")
    progress = line_progress(doc)[line.id]
    serial_no = normalise(text)
    row = SerialPick(tenant_id=doc.tenant_id, sales_document_id=doc.id, sales_line_id=line.id,
                     raw_text=(text or "")[:200], serial_no=serial_no or None, method=method, login_id=login.id,
                     session_id=session_id, client_pick_id=client_pick_id, photo_id=photo_id,
                     result=PickResult.OK)
    if not product.track_serial:
        # An item without serials: the scan (its barcode / model no.) or a tap counts one unit.
        if progress["picked"] >= progress["qty"]:
            row.result = PickResult.LINE_FULL
        elif serial_no and not _is_this_product(product, serial_no):
            row.result = PickResult.WRONG_PRODUCT
        row.serial_no = None
        return _keep(doc, row), ""

    if method == PickMethod.MANUAL and not login.manual_serial:
        raise DispatchError("Typing a serial by hand isn't switched on for your login - scan the label.", 403)
    if not serial_no:
        raise DispatchError("Nothing was read - scan the label again.")
    unit = find_serial(doc.tenant_id, serial_no)
    if unit is not None:
        row.serial_id = unit.id
        row.serial_no = unit.serial_no
    if progress["picked"] >= progress["qty"] and not (unit is not None and unit.picked_sales_line_id == line.id):
        row.result = PickResult.LINE_FULL
    elif unit is None:
        if allow_new and not _is_this_product(product, serial_no):
            row.result = _add_unit(doc, line, login, row, photo_id)
        else:
            row.result = PickResult.UNKNOWN
    elif unit.product_id != product.id:
        row.result = PickResult.WRONG_PRODUCT
    elif unit.status == SerialStatus.PICKED:
        row.result = PickResult.DUPLICATE
    elif unit.status != SerialStatus.IN_STOCK:
        row.result = PickResult.NOT_IN_STOCK
    elif unit.location_id is not None and unit.location_id != doc.dispatch_location_id:
        row.result = PickResult.WRONG_LOCATION
    else:
        unit.status = SerialStatus.PICKED
        unit.location_id = doc.dispatch_location_id
        unit.picked_sales_line_id = line.id
        unit.picked_by_id = login.id
        unit.picked_at = utcnow()
        unit.pick_photo_id = photo_id
    return _keep(doc, row), ""


def _is_this_product(product: Product, text: str) -> bool:
    """The text is the product's own model no. or barcode (not a serial)."""
    c = compact(text)
    return bool(c) and c in {compact(product.model_no or ""), compact(product.barcode or ""),
                             compact(product.sku or "")} - {""}


def _add_unit(doc, line, login, row, photo_id) -> PickResult:
    """A serial ARFA never had, on a box of the right product: into stock at
    this warehouse and picked for this line (listed for the owner to check)."""
    try:
        with db.session.begin_nested():
            unit = ProductSerial(tenant_id=doc.tenant_id, product_id=line.product_id, serial_no=row.serial_no,
                                 location_id=doc.dispatch_location_id, status=SerialStatus.PICKED,
                                 picked_sales_line_id=line.id, picked_by_id=login.id, picked_at=utcnow(),
                                 pick_photo_id=photo_id, added_at_pick=True)
            db.session.add(unit)
            db.session.flush()
    except IntegrityError:
        return PickResult.DUPLICATE                       # someone added the same serial a moment ago
    row.serial_id = unit.id
    return PickResult.ADDED


def _keep(doc: SalesDocument, row: SerialPick) -> SerialPick:
    db.session.add(row)
    doc.dispatch_activity_at = utcnow()
    if doc.dispatch_status == DispatchStatus.PICKED and row.result in (PickResult.OK, PickResult.ADDED):
        doc.dispatch_status = DispatchStatus.PICKING
    db.session.flush()
    return row


def message_for(row: SerialPick) -> str:
    unit = row.serial
    line = row.line
    needs = line.product.name if line is not None and line.product is not None else "this item"
    if row.result == PickResult.OK:
        return f"{row.serial_no or needs} picked."
    if row.result == PickResult.ADDED:
        return f"{row.serial_no} wasn't in ARFA - added to stock here and picked."
    if row.result == PickResult.WRONG_PRODUCT:
        if unit is not None and unit.product is not None:
            return f"This is {unit.product.name}, the order needs {needs}."
        return f"That label isn't {needs}."
    if row.result == PickResult.DUPLICATE:
        if unit is not None and unit.picked_sales_line_id:
            other = db.session.get(SalesDocumentLine, unit.picked_sales_line_id)
            if other is not None and other.document_id == row.sales_document_id:
                return f"{row.serial_no} is already scanned on this order."
            if other is not None:
                return f"{row.serial_no} is held for {other.document.number}."
        return f"{row.serial_no} is already picked."
    if row.result == PickResult.NOT_IN_STOCK:
        return f"{row.serial_no} isn't in stock (already sold or returned)."
    if row.result == PickResult.WRONG_LOCATION:
        where = unit.location.name if unit is not None and unit.location is not None else "another place"
        return f"{row.serial_no} is at {where}, not this warehouse."
    if row.result == PickResult.UNKNOWN:
        return f"{row.serial_no} isn't in ARFA. If the box is {needs}, add it and pick it."
    if row.result == PickResult.LINE_FULL:
        return f"Every unit of {needs} is already scanned."
    return ""


def undo(pick_row: SerialPick, reason: str = "undone by picker") -> None:
    doc = pick_row.line.document
    if doc.dispatch_status in (DispatchStatus.INVOICED, DispatchStatus.DELIVERED) or doc.status != SalesDocumentStatus.OPEN:
        raise DispatchError("This order is already invoiced - the pick can't be undone.", 409)
    if pick_row.undone_at is not None:
        return
    _undo(pick_row, reason)
    doc.dispatch_activity_at = utcnow()
    if doc.dispatch_status == DispatchStatus.PICKED:
        doc.dispatch_status = DispatchStatus.PICKING
        doc.picked_confirmed_at = None
        doc.picked_confirmed_by_id = None
    db.session.flush()


def confirm(doc: SalesDocument, login: WarehouseLogin) -> None:
    _require_claimer(doc, login)
    if doc.dispatch_status == DispatchStatus.PICKED:
        return
    progress = line_progress(doc)
    missing = sum(max(0, progress[l.id]["qty"] - progress[l.id]["picked"]) for l in doc.lines if needs_scan(l))
    if missing:
        raise DispatchError(f"{missing} unit{'s' if missing != 1 else ''} still to scan.", 409, missing=missing)
    _mark_picked(doc, login)
    notify.refresh(doc, doc.dispatch_location_id, "locked" if doc.is_finance_sale else "picked")


def _mark_picked(doc: SalesDocument, login: WarehouseLogin | None) -> None:
    """Every unit picked: an ordinary order goes to the warehouse computer; an
    EMI sale is *locked* - its units wait in the EMI HOLD bin and the showroom
    invoices with their serials."""
    doc.dispatch_status = DispatchStatus.PICKED
    doc.picked_confirmed_at = utcnow()
    doc.picked_confirmed_by_id = login.id if login is not None else None
    doc.dispatch_activity_at = utcnow()
    if doc.is_finance_sale:
        for pick in good_picks(doc):
            if pick.serial is not None:
                pick.serial.bin_code = EMI_HOLD
    db.session.flush()


def short(doc: SalesDocument, line: SalesDocumentLine, login: WarehouseLogin, reason: str) -> str:
    """"Can't find the unit": a note on the order for the showroom."""
    reason = (reason or "").strip()[:200] or "Can't find the unit"
    if line.document_id != doc.id:
        raise DispatchError("That line isn't on this order.", 404)
    stamp = utcnow().strftime("%d %b %H:%M")
    where = doc.dispatch_location.name if doc.dispatch_location else "Warehouse"
    note = f"[{stamp}] {where} ({login.name}): {line.description} - {reason}"
    doc.notes = f"{doc.notes}\n{note}" if doc.notes else note
    doc.dispatch_activity_at = utcnow()
    db.session.flush()
    return note


def allowed_serials(doc: SalesDocument, line: SalesDocumentLine) -> tuple[list[str], bool]:
    """The serials of this line's product in stock at the order's warehouse
    (what the phone matches labels against), plus those already picked for it."""
    if line.product is None or not line.product.track_serial:
        return [], False
    rows = (db.session.query(ProductSerial.serial_no)
            .filter(ProductSerial.tenant_id == doc.tenant_id, ProductSerial.product_id == line.product_id,
                    ProductSerial.status == SerialStatus.IN_STOCK,
                    db.or_(ProductSerial.location_id == doc.dispatch_location_id, ProductSerial.location_id.is_(None)))
            .order_by(ProductSerial.serial_no).limit(ALLOWED_SERIALS_CAP + 1).all())
    serials = [r[0] for r in rows]
    return serials[:ALLOWED_SERIALS_CAP], len(serials) > ALLOWED_SERIALS_CAP


def mark_invoiced(doc: SalesDocument, invoice) -> None:
    """Invoiced: at the warehouse computer (INVOICED), or an EMI sale at the
    showroom (stays PICKED, invoiced_at_showroom) - either way next is the
    delivery check."""
    if doc.is_finance_sale:
        doc.invoiced_at_showroom = True
    else:
        doc.dispatch_status = DispatchStatus.INVOICED
    doc.dispatch_activity_at = utcnow()
    db.session.flush()
    notify.refresh(doc, doc.dispatch_location_id, "invoiced")


def to_deliver(doc: SalesDocument) -> bool:
    from app.models.invoice import InvoiceStatus

    return (doc.converted_invoice_id is not None and doc.dispatch_location_id is not None
            and doc.dispatch_status in (DispatchStatus.PICKED, DispatchStatus.INVOICED)
            and doc.converted_invoice is not None and doc.converted_invoice.status == InvoiceStatus.ISSUED)


# --- racks (bins) ------------------------------------------------------------------------------------

def clean_bin(text: str) -> str:
    """A rack code from a label (RACK:C3) or typed (c3) -> "C3"."""
    code = (text or "").strip().upper()
    if code.startswith(RACK_PREFIX):
        code = code[len(RACK_PREFIX):]
    code = re.sub(r"\s+", "", code)[:32]
    if not code or not re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]*", code):
        raise DispatchError("That isn't a rack label.")
    return code


def move_to_bin(tenant_id: int, location_ids, text: str, bin_code: str) -> ProductSerial:
    """Move: a unit put on a rack (or into EMI READY). Only a unit in stock at
    one of this login's warehouses; a unit locked for EMI stays in EMI HOLD."""
    bin_code = clean_bin(bin_code)
    if bin_code == EMI_HOLD:
        raise DispatchError("Units go into EMI HOLD only by being locked for an EMI sale.")
    unit = find_serial(tenant_id, text)
    if unit is None:
        raise DispatchError(f"{normalise(text)} isn't in ARFA.", 404)
    if unit.location_id not in set(location_ids):
        raise DispatchError(f"{unit.serial_no} isn't at your warehouse.", 409)
    if unit.status != SerialStatus.IN_STOCK:
        raise DispatchError(f"{unit.serial_no} is {unit.status.value.replace('_', ' ')} - it can't be moved.", 409)
    unit.bin_code = bin_code
    db.session.flush()
    return unit


def bin_rank(unit: ProductSerial) -> int:
    """The showroom's serial list: EMI READY first, then units on a rack,
    then units with no rack."""
    if unit.bin_code == EMI_READY:
        return 0
    if unit.bin_code:
        return 1
    return 2


def showroom_choices(doc: SalesDocument, line: SalesDocumentLine, show_all: bool = False) -> list[ProductSerial]:
    """Serials the showroom may choose for a line when the warehouse can't pick:
    in stock at the order's warehouse, EMI READY first, then racked; units
    with no rack only with show_all (nobody could find them)."""
    if line.product is None or not line.product.track_serial:
        return []
    units = (ProductSerial.query.filter(ProductSerial.tenant_id == doc.tenant_id,
                                        ProductSerial.product_id == line.product_id,
                                        ProductSerial.status == SerialStatus.IN_STOCK,
                                        ProductSerial.location_id == doc.dispatch_location_id)
             .order_by(ProductSerial.serial_no).limit(ALLOWED_SERIALS_CAP).all())
    units.sort(key=lambda u: (bin_rank(u), u.bin_code or "", u.serial_no))
    return [u for u in units if show_all or bin_rank(u) < 2]


def showroom_pick(doc: SalesDocument, chosen: dict[int, list[str]], user) -> None:
    """The showroom chooses the serials itself (warehouse closed, no signal, a
    rush). Each is checked like a scan and held for the order; the warehouse
    then fetches exactly those (the app shows their racks) and the delivery
    check makes sure they are the ones that leave."""
    if doc.status != SalesDocumentStatus.OPEN or doc.dispatch_status != DispatchStatus.TO_PICK \
            or doc.claimed_by_id is not None:
        raise DispatchError("The warehouse is already picking this order - choose serials there.", 409)
    lines = {l.id: l for l in doc.lines}
    for line_id, serials in chosen.items():
        line = lines.get(line_id)
        if line is None or not needs_scan(line):
            raise DispatchError("That line isn't on this order.", 404)
        serials = [normalise(s) for s in serials if s and s.strip()]
        qty = int(Decimal(line.qty))
        if line.product.track_serial and (len(serials) != qty or len(set(serials)) != qty):
            raise DispatchError(f"{line.description}: choose {qty} different serial{'s' if qty != 1 else ''}.")
        for no in serials:
            unit = find_serial(doc.tenant_id, no)
            if unit is None or unit.product_id != line.product_id:
                raise DispatchError(f"{line.description}: {no} isn't one of its serials.")
            if unit.status != SerialStatus.IN_STOCK or unit.location_id != doc.dispatch_location_id:
                raise DispatchError(f"{line.description}: {no} isn't in stock at {doc.dispatch_location.name}.")
            unit.status = SerialStatus.PICKED
            unit.picked_sales_line_id = line.id
            unit.picked_at = utcnow()
            db.session.add(SerialPick(tenant_id=doc.tenant_id, sales_document_id=doc.id, sales_line_id=line.id,
                                      serial_id=unit.id, serial_no=unit.serial_no, raw_text=no,
                                      method=PickMethod.SHOWROOM, result=PickResult.OK))
    db.session.flush()
    if is_complete(doc):
        _mark_picked(doc, None)
        notify.refresh(doc, doc.dispatch_location_id, "locked" if doc.is_finance_sale else "picked")


# --- the delivery check --------------------------------------------------------------------------------

def invoiced_units(doc: SalesDocument) -> list[ProductSerial]:
    """The units that must leave with this order: the invoice's serials (after
    any approved swap)."""
    if not doc.converted_invoice_id:
        return []
    return (ProductSerial.query.filter_by(tenant_id=doc.tenant_id, sold_invoice_id=doc.converted_invoice_id)
            .order_by(ProductSerial.serial_no).all())


def delivered_ids(doc: SalesDocument) -> set[int]:
    return {s.serial_id for s in DeliveryScan.query.filter_by(sales_document_id=doc.id, ok=True) if s.serial_id}


def pending_swaps(doc: SalesDocument) -> list[SerialSwap]:
    return SerialSwap.query.filter_by(sales_document_id=doc.id, status=SwapStatus.PENDING).all()


def delivery_scan(doc: SalesDocument, login: WarehouseLogin, text: str, client_scan_id: str | None = None
                  ) -> tuple[DeliveryScan, str]:
    """A unit scanned at dispatch. Only an invoiced serial passes - anything
    else is blocked (and kept): no silent swap."""
    client_scan_id = (client_scan_id or "").strip()[:64] or None
    if client_scan_id:
        seen = DeliveryScan.query.filter_by(tenant_id=doc.tenant_id, client_scan_id=client_scan_id).first()
        if seen is not None:
            return seen, ("ok" if seen.ok else "blocked")
    if not to_deliver(doc):
        raise DispatchError("This order isn't waiting to be delivered.", 409)
    serial_no = normalise(text)
    if not serial_no:
        raise DispatchError("Nothing was read - scan the label again.")
    unit = find_serial(doc.tenant_id, serial_no, lock_row=False)
    expected = {u.id: u for u in invoiced_units(doc)}
    row = DeliveryScan(tenant_id=doc.tenant_id, sales_document_id=doc.id, serial_no=serial_no, login_id=login.id,
                       client_scan_id=client_scan_id, serial_id=unit.id if unit else None)
    if unit is not None and unit.id in expected:
        row.ok = True
        row.sales_line_id = unit.picked_sales_line_id
        message = ("already checked" if unit.id in delivered_ids(doc) else "ok")
    else:
        row.ok = False
        message = "blocked"
    db.session.add(row)
    doc.dispatch_activity_at = utcnow()
    db.session.flush()
    return row, message


def deliver(doc: SalesDocument, login: WarehouseLogin) -> None:
    if not to_deliver(doc):
        raise DispatchError("This order isn't waiting to be delivered.", 409)
    if pending_swaps(doc):
        raise DispatchError("A swap is waiting for the owner's approval.", 409)
    left = [u.serial_no for u in invoiced_units(doc) if u.id not in delivered_ids(doc)]
    if left:
        raise DispatchError(f"Scan {len(left)} more: {', '.join(left[:5])}.", 409, missing=left)
    doc.dispatch_status = DispatchStatus.DELIVERED
    doc.delivered_at = utcnow()
    doc.delivered_by_id = login.id
    db.session.flush()
    notify.refresh(doc, doc.dispatch_location_id, "delivered")


def request_swap(doc: SalesDocument, login: WarehouseLogin, old_text: str, new_text: str, reason: str) -> SerialSwap:
    """The box is damaged / the unit faulty: a replacement, held for the order
    until the owner approves (or rejects) the swap."""
    if not to_deliver(doc):
        raise DispatchError("This order isn't waiting to be delivered.", 409)
    reason = (reason or "").strip()[:255]
    if not reason:
        raise DispatchError("Say why the unit is being swapped.")
    expected = {u.serial_no.upper(): u for u in invoiced_units(doc)}
    old = expected.get(normalise(old_text))
    if old is None:
        raise DispatchError(f"{normalise(old_text)} isn't on this order's invoice.", 404)
    if any(s.old_serial_id == old.id for s in pending_swaps(doc)):
        raise DispatchError(f"A swap for {old.serial_no} is already waiting.", 409)
    new = find_serial(doc.tenant_id, new_text)
    if new is None:
        raise DispatchError(f"{normalise(new_text)} isn't in ARFA.", 404)
    if new.product_id != old.product_id:
        raise DispatchError(f"{new.serial_no} is {new.product.name}, not {old.product.name}.")
    if new.status != SerialStatus.IN_STOCK or new.location_id != doc.dispatch_location_id:
        raise DispatchError(f"{new.serial_no} isn't in stock at {doc.dispatch_location.name}.", 409)
    new.status = SerialStatus.PICKED              # held for the swap
    new.picked_sales_line_id = old.picked_sales_line_id
    new.picked_by_id = login.id
    new.picked_at = utcnow()
    swap = SerialSwap(tenant_id=doc.tenant_id, sales_document_id=doc.id, sales_line_id=old.picked_sales_line_id,
                      old_serial_id=old.id, new_serial_id=new.id, reason=reason, requested_by_id=login.id)
    db.session.add(swap)
    db.session.flush()
    return swap


def decide_swap(swap: SerialSwap, user, approve: bool, note: str = "") -> None:
    """The owner's decision. Approved: the invoice stays as issued; the old
    serial goes back in stock at the warehouse, the new one is sold on the
    same invoice line, and the delivery slip records the swap."""
    if swap.status != SwapStatus.PENDING:
        raise DispatchError("This swap is already decided.", 409)
    old, new = swap.old_serial, swap.new_serial
    swap.decided_by_id = user.id
    swap.decided_at = utcnow()
    swap.note = (note or "").strip()[:255] or None
    if not approve:
        swap.status = SwapStatus.REJECTED
        if new.status == SerialStatus.PICKED:
            new.status = SerialStatus.IN_STOCK
            new.picked_sales_line_id = new.picked_by_id = new.picked_at = None
        db.session.flush()
        return
    swap.status = SwapStatus.APPROVED
    from app.models.inventory import MovementType

    location_id = old.location_id or new.location_id
    cost = ledger.issue_cost(old.product)
    new.status = SerialStatus.SOLD
    new.sold_at = utcnow()
    new.sold_invoice_id, new.sold_invoice_line_id = old.sold_invoice_id, old.sold_invoice_line_id
    new.bin_code = None
    old.status = SerialStatus.IN_STOCK
    old.location_id = location_id
    old.sold_at = None
    old.sold_invoice_id = old.sold_invoice_line_id = None
    old.picked_sales_line_id = old.picked_by_id = old.picked_at = None
    old.bin_code = None
    ledger.post(swap.tenant_id, old.product_id, location_id, 1, MovementType.SALE_RETURN, "serial_swap", swap.id,
                unit_cost=cost, serial_id=old.id, user_id=user.id, note=f"Swapped out: {swap.reason}"[:255])
    ledger.post(swap.tenant_id, new.product_id, location_id, -1, MovementType.SALE, "serial_swap", swap.id,
                unit_cost=cost, serial_id=new.id, user_id=user.id, note=f"Swapped in for {old.serial_no}"[:255])
    db.session.flush()


def swaps_for_slip(doc: SalesDocument) -> dict[str, SerialSwap]:
    """Approved swaps by the invoiced (old) serial - printed on the slip."""
    return {s.old_serial.serial_no: s for s in
            SerialSwap.query.filter_by(sales_document_id=doc.id, status=SwapStatus.APPROVED).all()}
