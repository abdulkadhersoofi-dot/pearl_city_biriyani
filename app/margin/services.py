"""Selling second-hand goods under the margin scheme (Rule 32(5), module
"margin_scheme").

  - Which purchase each unit sold came from: the serial/IMEI unit's own
    purchase when serial numbers are tracked, otherwise the oldest
    margin-scheme purchase of the item with units left (first in, first
    out). A unit with no purchase on record can't be sold under the
    scheme - its purchase price is half of the margin.
  - A margin-scheme sale is its own invoice: it can't be mixed with
    regular goods (separate invoices - the client's own rule).
  - The margin math is app.utils.margin; this module only finds the lots
    and records what each sale took from them (MarginAllocation - also
    the margin register).
"""

from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models.margin import MarginAllocation
from app.models.mixins import utcnow
from app.models.product import Product
from app.models.purchase import PurchaseBill, PurchaseBillLine, PurchaseReturn, PurchaseReturnLine, PurchaseStatus
from app.models.tenant import RegistrationType
from app.utils.gst import compute_line, round_money
from app.utils.margin import MarginChunk, compute_margin_line, split_sale_value

ZERO = Decimal("0")


class MarginError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def margin_on(tenant) -> bool:
    """The scheme only means something for a regular GST registration -
    composition and unregistered dealers charge no GST at all."""
    return tenant.has_feature("margin_scheme") and tenant.registration_type == RegistrationType.REGULAR


def margin_products(tenant, product_ids) -> dict[int, Product]:
    ids = {pid for pid in product_ids if pid}
    if not ids or not margin_on(tenant):
        return {}
    return {p.id: p for p in Product.query.filter(Product.tenant_id == tenant.id, Product.id.in_(ids),
                                                  Product.is_margin_scheme.is_(True)).all()}


def sale_kind(tenant, product_ids: list) -> bool:
    """True for a margin-scheme sale (every line a margin-scheme item),
    False for a regular one. Refuses a mix - they are billed on separate
    invoices. A line with no catalog item counts as regular."""
    margin = margin_products(tenant, product_ids)
    if not margin:
        return False
    regular = [pid for pid in product_ids if pid not in margin]
    if regular:
        names = ", ".join(sorted(p.name for p in margin.values()))
        raise MarginError(f"{names} {'is' if len(margin) == 1 else 'are'} sold under the margin scheme and must be "
                          "billed on a separate invoice from regular items. Bill them in two sales.")
    return True


# --- lots -------------------------------------------------------------------------------

def _taken(purchase_line_ids) -> dict[int, Decimal]:
    rows = (
        db.session.query(MarginAllocation.purchase_line_id,
                         db.func.sum(MarginAllocation.qty - MarginAllocation.returned_qty))
        .filter(MarginAllocation.purchase_line_id.in_(list(purchase_line_ids)), MarginAllocation.released_at.is_(None))
        .group_by(MarginAllocation.purchase_line_id).all()
    )
    return {lid: Decimal(q or 0) for lid, q in rows}


def _sent_back(purchase_line_ids) -> dict[int, Decimal]:
    rows = (
        db.session.query(PurchaseReturnLine.bill_line_id, db.func.sum(PurchaseReturnLine.qty))
        .join(PurchaseReturn, PurchaseReturn.id == PurchaseReturnLine.return_id)
        .filter(PurchaseReturnLine.bill_line_id.in_(list(purchase_line_ids)), PurchaseReturn.status == PurchaseStatus.POSTED)
        .group_by(PurchaseReturnLine.bill_line_id).all()
    )
    return {lid: Decimal(q or 0) for lid, q in rows}


def available_lots(tenant_id: int, product_id: int) -> list[tuple[PurchaseBillLine, Decimal]]:
    """[(purchase line, units still unsold)], oldest purchase first."""
    lines = (
        PurchaseBillLine.query.join(PurchaseBill, PurchaseBill.id == PurchaseBillLine.bill_id)
        .filter(PurchaseBill.tenant_id == tenant_id, PurchaseBill.status == PurchaseStatus.POSTED,
                PurchaseBillLine.product_id == product_id, PurchaseBillLine.is_margin_scheme.is_(True))
        .order_by(PurchaseBill.bill_date, PurchaseBill.id, PurchaseBillLine.id).all()
    )
    if not lines:
        return []
    ids = [l.id for l in lines]
    taken, back = _taken(ids), _sent_back(ids)
    lots = []
    for line in lines:
        left = Decimal(line.qty) - taken.get(line.id, ZERO) - back.get(line.id, ZERO)
        if left > 0:
            lots.append((line, left))
    return lots


def lot_remaining(purchase_line: PurchaseBillLine) -> Decimal:
    taken = _taken([purchase_line.id]).get(purchase_line.id, ZERO)
    back = _sent_back([purchase_line.id]).get(purchase_line.id, ZERO)
    return Decimal(purchase_line.qty) - taken - back


def plan_line(tenant, product: Product, qty: Decimal, serials=()) -> list[tuple[PurchaseBillLine, object, Decimal]]:
    """[(purchase line, serial unit or None, qty)] this sale line takes.
    Raises MarginError when the units sold have no purchase on record."""
    from app.models.inventory import ProductSerial

    qty = Decimal(qty)
    if tenant.has_feature("serial_imei") and product.track_serial:
        serials = [s.strip() for s in serials if s and s.strip()]
        if not serials:
            raise MarginError(f"{product.name}: pick the serial/IMEI being sold - its purchase price sets the margin.")
        plan = []
        for no in serials:
            unit = ProductSerial.query.filter_by(tenant_id=tenant.id, product_id=product.id, serial_no=no).first()
            line = None
            if unit is not None and unit.purchase_bill_id:
                line = PurchaseBillLine.query.filter_by(bill_id=unit.purchase_bill_id, product_id=product.id,
                                                        is_margin_scheme=True).first()
            if line is None:
                raise MarginError(f"{product.name} {no}: no margin-scheme purchase on record for this unit.")
            plan.append((line, unit, Decimal("1")))
        return plan
    plan, remaining = [], qty
    for line, left in available_lots(tenant.id, product.id):
        if remaining <= 0:
            break
        take = min(left, remaining)
        plan.append((line, None, take))
        remaining -= take
    if remaining > 0:
        have = qty - remaining
        raise MarginError(
            f"{product.name}: only {have.normalize():f} unit(s) have a margin-scheme purchase on record - "
            "enter the purchase bill first (its price sets the margin)." if have > 0 else
            f"{product.name}: no margin-scheme purchase on record - enter the purchase bill first "
            "(its price sets the margin)."
        )
    return plan


# --- pricing ------------------------------------------------------------------------------

def price_line(qty, rate, discount_percent, discount_flat, gst_rate, is_intra_state: bool, plan) -> dict:
    """A margin-scheme sale line, in the same shape as gst.compute_line's
    result (so it saves like any line) plus the margin fields. The price
    is what the buyer pays - nothing is added on top."""
    gross = compute_line(qty=qty, rate=rate, discount_percent=discount_percent, gst_rate=Decimal("0"),
                         is_intra_state=is_intra_state, charge_gst=False, price_includes_tax=True,
                         discount_flat=discount_flat or 0)
    sale_total = Decimal(gross["line_total"])
    shares = split_sale_value(sale_total, [q for _, _, q in plan])
    chunks = [MarginChunk(q, share, round_money(Decimal(lot.margin_unit_price) * q))
              for (lot, _, q), share in zip(plan, shares)]
    margin = compute_margin_line(chunks, gst_rate, is_intra_state)
    return {
        "discount_amount": gross["discount_amount"],
        "taxable_value": margin["taxable_value"],
        "cgst_amount": margin["cgst_amount"],
        "sgst_amount": margin["sgst_amount"],
        "igst_amount": margin["igst_amount"],
        "line_total": margin["line_total"],
        "is_margin_scheme": True,
        "margin_purchase_value": margin["purchase_value"],
        "margin_value": margin["margin"],
        "plan": plan,
        "parts": margin["parts"],
    }


# --- recording / releasing --------------------------------------------------------------------

def record(tenant_id: int, source_type: str, source_id: int, source_line_id: int, document_number: str | None,
           sold_on: date, product_id: int, priced: dict) -> None:
    for (lot, serial, qty), part in zip(priced["plan"], priced["parts"]):
        db.session.add(MarginAllocation(
            tenant_id=tenant_id, source_type=source_type, source_id=source_id, source_line_id=source_line_id,
            document_number=document_number, sold_on=sold_on, purchase_line_id=lot.id,
            serial_id=serial.id if serial is not None else None, product_id=product_id, qty=qty,
            purchase_value=part["purchase_value"], sale_value=part["sale_value"], margin=part["margin"],
            taxable_value=part["taxable_value"], cgst_amount=part["cgst_amount"], sgst_amount=part["sgst_amount"],
            igst_amount=part["igst_amount"],
        ))
    db.session.flush()


def release(tenant_id: int, source_type: str, source_id: int) -> None:
    """A voided or refunded sale gives its units back to their lots."""
    for row in MarginAllocation.query.filter_by(tenant_id=tenant_id, source_type=source_type, source_id=source_id,
                                                released_at=None):
        row.released_at = utcnow()


def return_units(tenant_id: int, source_type: str, source_id: int, source_line_id: int, qty, undo: bool = False) -> None:
    """Units of a sale line coming back on a sales return go back into the
    lots they came from (latest first); undo=True reverses that when the
    return itself is voided."""
    rows = (MarginAllocation.query.filter_by(tenant_id=tenant_id, source_type=source_type, source_id=source_id,
                                             source_line_id=source_line_id, released_at=None)
            .order_by(MarginAllocation.id.desc()).all())
    remaining = Decimal(qty)
    for row in (reversed(rows) if undo else rows):
        if remaining <= 0:
            break
        if undo:
            take = min(Decimal(row.returned_qty), remaining)
            row.returned_qty = Decimal(row.returned_qty) - take
        else:
            take = min(Decimal(row.qty) - Decimal(row.returned_qty), remaining)
            row.returned_qty = Decimal(row.returned_qty) + take
        remaining -= take


def sold_from(purchase_line_ids) -> bool:
    """Has any unit of these purchase lines been sold (and not given back)?"""
    return any(q > 0 for q in _taken(purchase_line_ids).values())
