"""A supplier's bill and the purchase order it belongs to.

Finding the order (candidates):
  1. the order number printed on the bill - compared after cleaning (case,
     spaces, punctuation, a leading "PO", leading zeros) against the
     supplier's orders still to be billed;
  2. else every such order of that supplier is scored on its lines against
     the bill's: the same items (the same catalogue item, else the same HSN
     and a like name, else a like name), quantities no more than what is
     still to come, rates within tolerance, the bill dated on or after the
     order. The best is proposed only when it scores well AND clearly ahead
     of the next; otherwise the top few are offered and none is chosen.

Checking it (compare): each bill line beside its order line - ordered,
received at the warehouse (units scanned in against the order), billed
before, billed now - with what differs said in words. A difference is a
"problem" (more billed than ordered, a rate or GST rate that differs, an
item that isn't on the order) or just "to know" (part of a line billed now,
an order line not on this bill).

Posting (settle): the bill's lines are tied to the order's lines, each order
line counts what is billed, and the order is Open, Partly billed or Billed
by what is still to come. Several bills can settle one order. Voiding a
bill gives its share back (unsettle).

Nothing here posts a bill: a person always confirms the match on the check
screen (app/scan) or the bill form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace

from app.extensions import db
from app.models.purchase import PurchaseBill, PurchaseBillLine, PurchaseOrder, PurchaseOrderLine, PurchaseOrderStatus

ZERO = Decimal("0")
# How far a bill may differ from its order before it is called a difference.
QTY_TOLERANCE = Decimal("0")
RATE_TOLERANCE_PERCENT = Decimal("1")
RATE_TOLERANCE_RUPEES = Decimal("1")
STILL_TO_COME = (PurchaseOrderStatus.OPEN, PurchaseOrderStatus.PARTLY)
PROPOSE_SCORE = 70          # a match by lines is proposed from this score...
PROPOSE_LEAD = 15           # ...and this far ahead of the next best
PROBLEM, NOTE = "problem", "note"


# --- the order number ---------------------------------------------------------------------------------

def clean_number(text) -> str:
    """"PO-0045/26-27", "po 45/26-27", "Order No: 0045/26-27" -> "45/2627"-like
    keys that compare equal: letters and digits only, upper case, without a
    leading PO / ORDER word, and without leading zeros."""
    key = re.sub(r"[^A-Z0-9]", "", str(text or "").upper())
    key = re.sub(r"^(PURCHASEORDER|ORDERNO|ORDER|PONO|PO)", "", key)
    return key.lstrip("0")


def _digits(text) -> str:
    return re.sub(r"\D", "", str(text or "")).lstrip("0")


def numbers_match(printed, order_number, external_ref=None) -> bool:
    a = clean_number(printed)
    if not a:
        return False
    for known in (order_number, external_ref):
        b = clean_number(known)
        if not b:
            continue
        if a == b:
            return True
        # "4500012345" on the bill, "PO/4500012345" in ARFA: long digit runs that agree.
        if len(_digits(printed)) >= 5 and _digits(printed) == _digits(known):
            return True
    return False


# --- pairing lines ------------------------------------------------------------------------------------

@dataclass
class Row:
    """One line of the bill, as read or as typed."""
    description: str
    qty: Decimal
    rate: Decimal = ZERO
    gst_rate: Decimal | None = None
    product_id: int | None = None
    hsn: str = ""


@dataclass
class Pair:
    row: Row | None                         # None: an order line this bill doesn't have
    line: PurchaseOrderLine | None          # None: a bill line the order doesn't have
    how: str = ""                           # item | hsn | name
    received: Decimal | None = None         # units scanned in at the warehouse, when it scans
    remarks: list[tuple[str, str]] = field(default_factory=list)     # (PROBLEM | NOTE, words)

    @property
    def problems(self) -> list[str]:
        return [text for kind, text in self.remarks if kind == PROBLEM]


@dataclass
class Match:
    order: PurchaseOrder
    by: str                                 # number | lines | picked
    score: int
    pairs: list[Pair]

    @property
    def problems(self) -> list[str]:
        return [text for p in self.pairs for text in p.problems]

    @property
    def notes(self) -> list[str]:
        return [text for p in self.pairs for kind, text in p.remarks if kind == NOTE]

    @property
    def clean(self) -> bool:
        return not self.problems


def _alike(a: str, b: str) -> bool:
    from app.scan.services import names_alike

    return names_alike(a or "", b or "")


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def pair_lines(order: PurchaseOrder, rows: list[Row]) -> list[Pair]:
    """Each bill line with the order line it most likely is; an order line
    is used once. Then the order's lines the bill doesn't have."""
    free = list(order.lines)
    pairs: list[Pair | None] = [None] * len(rows)
    for how in ("item", "hsn", "name"):
        for i, row in enumerate(rows):
            if pairs[i] is not None:
                continue
            for line in free:
                if how == "item":
                    hit = row.product_id is not None and line.product_id == row.product_id
                elif how == "hsn":
                    hit = bool(row.hsn) and row.hsn == (line.hsn_or_sac_code or "") and _alike(row.description,
                                                                                               line.description)
                else:
                    hit = _alike(row.description, line.description)
                if hit:
                    pairs[i] = Pair(row, line, how)
                    free.remove(line)
                    break
    out = [p if p is not None else Pair(rows[i], None) for i, p in enumerate(pairs)]
    return out + [Pair(None, line) for line in free]


def _money(value) -> str:
    from app.utils.money import inr

    return inr(value)


def _qty(value) -> str:
    return f"{Decimal(value).normalize():f}"


def received_by_line(order: PurchaseOrder) -> dict[int, Decimal]:
    """order line id -> units scanned in at the warehouse against it."""
    from app.models.warehouse import ReceivedUnit

    rows = db.session.query(ReceivedUnit.order_line_id, db.func.count(ReceivedUnit.id)).filter(
        ReceivedUnit.order_line_id.in_([l.id for l in order.lines] or [0])).group_by(ReceivedUnit.order_line_id).all()
    return {line_id: Decimal(n) for line_id, n in rows}


def compare(order: PurchaseOrder, rows: list[Row], by: str = "picked", bill_date=None) -> Match:
    """The bill beside the order, with what differs in words, and a score."""
    received = received_by_line(order)
    pairs = pair_lines(order, rows)
    points, possible = 0, 0
    for p in pairs:
        if p.line is not None and p.line.id in received:
            p.received = received[p.line.id]
        if p.row is None:
            if p.line.outstanding > 0:
                p.remarks.append((NOTE, f"{p.line.description} ({_qty(p.line.outstanding)} {p.line.unit}) is on the "
                                        "order but not on this bill - it stays open."))
            continue
        possible += 4
        if p.line is None:
            p.remarks.append((PROBLEM, f"{p.row.description} is not on the order."))
            continue
        points += {"item": 2, "hsn": 2, "name": 1}[p.how]
        name = p.line.description
        outstanding = p.line.outstanding
        if p.row.qty > outstanding + QTY_TOLERANCE:
            already = f" ({_qty(p.line.billed_qty)} already billed)" if p.line.billed_qty else ""
            p.remarks.append((PROBLEM, f"{name}: {_qty(p.row.qty)} billed, only {_qty(outstanding)} still to come "
                                       f"on the order{already}."))
        else:
            points += 1
            if p.row.qty < outstanding:
                p.remarks.append((NOTE, f"{name}: {_qty(p.row.qty)} of {_qty(outstanding)} billed now - "
                                        f"{_qty(outstanding - p.row.qty)} still to come."))
        if p.received is not None and p.row.qty > p.received:
            p.remarks.append((PROBLEM, f"{name}: {_qty(p.row.qty)} billed, {_qty(p.received)} received at the "
                                       "warehouse so far."))
        allowed = max(Decimal(p.line.rate) * RATE_TOLERANCE_PERCENT / 100, RATE_TOLERANCE_RUPEES)
        if Decimal(p.line.rate) and abs(Decimal(p.row.rate) - Decimal(p.line.rate)) > allowed:
            p.remarks.append((PROBLEM, f"{name}: billed at Rs. {_money(p.row.rate)}, ordered at "
                                       f"Rs. {_money(p.line.rate)}."))
        else:
            points += 1
        if p.row.gst_rate is not None and Decimal(p.row.gst_rate) != Decimal(p.line.gst_rate):
            p.remarks.append((PROBLEM, f"{name}: GST {_qty(p.row.gst_rate)}% on the bill, "
                                       f"{_qty(p.line.gst_rate)}% on the order."))
    score = int(round(100 * points / possible)) if possible else 0
    if bill_date is not None and order.order_date and bill_date < order.order_date:
        score = max(score - 30, 0)          # a bill dated before its order is unlikely to be its bill
    return Match(order, by, 100 if by == "number" else score, pairs)


# --- finding the order --------------------------------------------------------------------------------

def open_orders(tenant, supplier=None) -> list[PurchaseOrder]:
    query = PurchaseOrder.query.filter(PurchaseOrder.tenant_id == tenant.id, PurchaseOrder.status.in_(STILL_TO_COME))
    if supplier is not None:
        query = query.filter(PurchaseOrder.supplier_id == supplier.id)
    return query.order_by(PurchaseOrder.order_date.desc(), PurchaseOrder.id.desc()).limit(200).all()


@dataclass
class Found:
    proposed: Match | None          # the one ARFA would tie the bill to
    others: list[Match]             # the next best, to choose from
    why: str = ""                   # one sentence on how it was found, or why none is proposed


def candidates(tenant, supplier, po_number, rows: list[Row], bill_date=None) -> Found:
    """The order this bill most likely belongs to."""
    orders = open_orders(tenant, supplier)
    if po_number:
        # By the printed number - among the supplier's orders, or with the
        # supplier not yet known, any order (when only one answers to it).
        pool = orders if supplier is not None else open_orders(tenant)
        hits = [o for o in pool if numbers_match(po_number, o.number, getattr(o, "external_ref", None))]
        if len(hits) == 1:
            match = compare(hits[0], rows, "number", bill_date)
            rest = [compare(o, rows, "lines", bill_date) for o in orders if o.id != hits[0].id]
            rest.sort(key=lambda m: -m.score)
            return Found(match, rest[:3], f"The bill names order {po_number}.")
    if supplier is None or not orders:
        why = "This supplier has no purchase order waiting for a bill." if supplier is not None else \
            "Pick the supplier to look for its purchase orders."
        if po_number and supplier is not None:
            why = f"The bill names order {po_number}, but this supplier has no open order with that number."
        return Found(None, [], why)
    scored = sorted((compare(o, rows, "lines", bill_date) for o in orders), key=lambda m: (-m.score, -m.order.id))
    best, second = scored[0], (scored[1].score if len(scored) > 1 else 0)
    printed = f"The bill names order {po_number}, which isn't one of this supplier's open orders. " if po_number else ""
    if best.score >= PROPOSE_SCORE and best.score - second >= PROPOSE_LEAD:
        return Found(best, scored[1:4], printed + "Matched by its items, quantities and rates.")
    if best.score < 40:
        return Found(None, [m for m in scored[:3] if m.score > 0],
                     printed + "No open order of this supplier looks like this bill.")
    if len(scored) == 1 or best.score - second >= PROPOSE_LEAD:
        return Found(None, scored[:3], printed + f"Order {best.order.number} may be this bill's, but they differ - "
                                                 "look, and pick it if it is.")
    return Found(None, scored[:3], printed + "More than one open order could be this bill's - pick one.")


def match_for(tenant, order_id, rows: list[Row], bill_date=None) -> Match | None:
    """The comparison for an order the user picked by hand."""
    order = PurchaseOrder.query.filter_by(id=order_id, tenant_id=tenant.id).first() if order_id else None
    if order is None or order.status not in STILL_TO_COME:
        return None
    return compare(order, rows, "picked", bill_date)


# --- settling -----------------------------------------------------------------------------------------

def refresh_status(order: PurchaseOrder) -> None:
    if order.status == PurchaseOrderStatus.CANCELLED:
        return
    billed = sum((Decimal(l.billed_qty or 0) for l in order.lines), ZERO)
    if billed <= 0:
        order.status = PurchaseOrderStatus.OPEN
    elif all(l.outstanding <= 0 for l in order.lines):
        order.status = PurchaseOrderStatus.CONVERTED
    else:
        order.status = PurchaseOrderStatus.PARTLY


def settle(order: PurchaseOrder, bill: PurchaseBill, order_line_ids=None) -> None:
    """Tie the bill's lines to the order's and count what is billed. A line
    naming its order line is taken at its word (if that line is the order's);
    the rest are paired by item."""
    lines = list(bill.lines)
    own = {l.id: l for l in order.lines}
    named = [(_int(v) if v else None) for v in (order_line_ids or [])]
    targets: dict[int, PurchaseOrderLine] = {}
    for bill_line, line_id in zip(lines, named + [None] * (len(lines) - len(named))):
        if line_id in own and own[line_id] not in targets.values():
            targets[bill_line.id] = own[line_id]
    left = [l for l in lines if l.id not in targets]
    if left:
        taken = set(t.id for t in targets.values())
        rows = [Row(l.description, Decimal(l.qty), Decimal(l.rate), l.gst_rate, l.product_id, l.hsn_or_sac_code or "")
                for l in left]

        rest = SimpleNamespace(lines=[l for l in order.lines if l.id not in taken])   # not yet spoken for
        for bill_line, pair in zip(left, pair_lines(rest, rows)):
            if pair.line is not None:
                targets[bill_line.id] = pair.line
    for bill_line in lines:
        target = targets.get(bill_line.id)
        if target is None:
            continue
        bill_line.order_line_id = target.id
        target.billed_qty = Decimal(target.billed_qty or 0) + Decimal(bill_line.qty)
    db.session.flush()
    refresh_status(order)


def unsettle(bill: PurchaseBill) -> None:
    """A voided bill gives back what it had settled."""
    order = bill.purchase_order
    if order is None:
        return
    own = {l.id: l for l in order.lines}
    for bill_line in bill.lines:
        target = own.get(bill_line.order_line_id) if bill_line.order_line_id else None
        if target is not None:
            target.billed_qty = max(Decimal(target.billed_qty or 0) - Decimal(bill_line.qty), ZERO)
    # Bills entered before orders were billed in parts closed their order whole.
    if not any(l.order_line_id for l in bill.lines) and order.status == PurchaseOrderStatus.CONVERTED:
        others = PurchaseBillLine.query.join(PurchaseBill).filter(
            PurchaseBill.purchase_order_id == order.id, PurchaseBill.id != bill.id,
            PurchaseBill.status == bill.status).count()
        if not others:
            for line in order.lines:
                line.billed_qty = ZERO
    db.session.flush()
    refresh_status(order)


def close_short(order: PurchaseOrder, reason: str) -> None:
    """The supplier won't send the rest: the order is closed as it stands."""
    from app.purchases.services import PurchaseError

    if order.status != PurchaseOrderStatus.PARTLY:
        raise PurchaseError("Only a partly billed order can be closed short - cancel an open one instead.")
    if not (reason or "").strip():
        raise PurchaseError("Say why the rest isn't coming.")
    order.status = PurchaseOrderStatus.CONVERTED
    order.cancel_reason = f"Closed short: {reason.strip()}"[:500]
