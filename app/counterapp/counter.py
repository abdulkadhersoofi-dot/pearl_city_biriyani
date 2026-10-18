"""The ARFA Counter app's Held and Today (counter app, stage 4).

Held: a bill put aside at this counter (a customer fetching one more
thing) - the counter's own HELD bills, the same ones the full counter
screen lists, minus a table's running order (that lives on its table).
Today: what this counter has billed since midnight (Indian time), by how it
was paid, the cash that should be in the drawer, and its bills to print
again.
"""

from datetime import datetime, time
from decimal import Decimal

from app.einvoice.providers.common import IST
from app.extensions import db
from app.models.pos_bill import PAYMENT_MODE_LABELS, POSBill, POSBillStatus, POSPayment
from app.models.product import Product

ZERO = Decimal("0")
MAX_HELD_LINES = 200


class CounterError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- held bills ------------------------------------------------------------------------------

def held_bills(tenant, user):
    return (POSBill.query.filter(POSBill.tenant_id == tenant.id, POSBill.status == POSBillStatus.HELD,
                                 POSBill.table_id.is_(None), POSBill.created_by_id == user.id)
            .order_by(POSBill.created_at.desc()).all())


def held_json(bill) -> dict:
    lines = [l for l in bill.lines if not l.charge_kind]
    return {"id": bill.id, "label": bill.hold_label or "", "total": float(bill.grand_total),
            "count": len(lines), "at": bill.created_at.astimezone(IST).strftime("%H:%M"),
            "order_type": bill.order_type or "",
            "lines": [{"product_id": l.product_id, "name": l.description, "qty": float(l.qty), "note": l.note or ""}
                      for l in lines]}


def hold(tenant, user, payload: dict) -> POSBill:
    """Hold the phone's cart: catalog items and quantities; prices and tax
    from the item master, as at the counter."""
    from app.pos.services import CartInput, CartLineInput, hold_cart
    from app.utils.module_settings import ORDER_TYPES

    raw = payload.get("lines")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_HELD_LINES:
        raise CounterError("There's nothing on the bill to hold.")
    wanted = []
    for item in raw:
        try:
            wanted.append((int(item.get("product_id")), Decimal(str(item.get("qty"))),
                           str(item.get("note") or "").strip()[:100]))
        except (TypeError, ValueError, AttributeError, ArithmeticError):
            raise CounterError("An item on the bill can't be read.")
    products = {p.id: p for p in Product.query.filter(Product.tenant_id == tenant.id,
                                                      Product.id.in_({pid for pid, _, _ in wanted}))}
    lines = []
    for pid, qty, note in wanted:
        p = products.get(pid)
        if p is None:
            raise CounterError("An item on the bill isn't in your catalog any more.")
        if not qty.is_finite() or qty <= 0:
            raise CounterError(f"The quantity of {p.name} isn't right.")
        lines.append(CartLineInput(description=p.name, hsn_or_sac_code=p.hsn_or_sac_code or "", qty=qty,
                                   rate=p.sale_price, gst_rate=p.gst_rate, product_id=p.id, unit=p.unit,
                                   price_includes_tax=p.price_includes_tax, note=note))
    order_type = payload.get("order_type") or None
    if order_type not in ORDER_TYPES or order_type == "dine_in" or not tenant.has_feature("restaurant_mode"):
        order_type = None
    label = str(payload.get("label") or "").strip()[:100]
    return hold_cart(tenant, user, CartInput(lines=lines, order_type=order_type), label or None)


def own_held(tenant, user, bill_id):
    bill = POSBill.query.filter_by(id=bill_id, tenant_id=tenant.id, status=POSBillStatus.HELD,
                                   created_by_id=user.id).first()
    if bill is None or bill.table_id is not None:
        raise CounterError("That bill isn't held any more - another screen may have opened it.")
    return bill


# --- today -----------------------------------------------------------------------------------

def today(tenant, user) -> dict:
    from app.pos.sessions import current_session, day_close_on, session_activity

    now = datetime.now(IST)
    start = datetime.combine(now.date(), time.min, IST)
    bills = (POSBill.query.filter(POSBill.tenant_id == tenant.id, POSBill.created_by_id == user.id,
                                  POSBill.bill_number.isnot(None), POSBill.completed_at >= start,
                                  POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED,
                                                      POSBillStatus.VOIDED]))
             .order_by(POSBill.completed_at.desc()).all())
    live = [b for b in bills if b.status != POSBillStatus.VOIDED]
    by_mode = {}
    if live:
        rows = (db.session.query(POSPayment.mode, db.func.sum(POSPayment.amount))
                .filter(POSPayment.bill_id.in_([b.id for b in live])).group_by(POSPayment.mode).all())
        by_mode = {mode: amount for mode, amount in rows}
    drawer = None
    if day_close_on(tenant):
        session = current_session(tenant, user)
        if session is not None:
            activity = session_activity(session)
            drawer = {"opening": session.opening_cash, "expected": activity["expected_cash"]}
    return {
        "date": now.date(),
        "count": len(live),
        "total": sum((b.grand_total for b in live), ZERO),
        "modes": [(PAYMENT_MODE_LABELS.get(m, m.value), amount) for m, amount in
                  sorted(by_mode.items(), key=lambda kv: -kv[1])],
        "voided": sum(1 for b in bills if b.status == POSBillStatus.VOIDED),
        "bills": bills[:40],
        "more": max(0, len(bills) - 40),
        "drawer": drawer,
    }
