"""Day open / day close per counter (feature "day_close").

A counter is one login. While the feature is on, a counter must open its
day (counting the cash it starts with) before it can bill, refund or take
returns; cash in/out is recorded against the day; closing it counts the
drawer and keeps expected vs actual. A closed day is locked: its bills
can't be voided any more, and goods coming back are a return in a later
day. With the feature off, nothing here is ever consulted.
"""

from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models.cash_session import CashMovement, CashSession
from app.models.mixins import utcnow
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus, POSPayment
from app.models.pos_return import POSReturn, POSReturnStatus
from app.pos.services import POSValidationError, summarize_pos_activity
from app.utils.gst import round_money

ZERO = Decimal("0")


def day_close_on(tenant) -> bool:
    return tenant.has_feature("day_close")


def current_session(tenant, user) -> CashSession | None:
    return CashSession.query.filter_by(tenant_id=tenant.id, user_id=user.id, closed_at=None).first()


def require_open_session(tenant, user) -> CashSession | None:
    """The counter's open day, or None when day open/close is off. Raises
    when it's on and the day hasn't been opened (or was closed)."""
    if not day_close_on(tenant):
        return None
    session = current_session(tenant, user)
    if session is None:
        raise POSValidationError("Open the day first - count the cash in the drawer and tap Open day.")
    return session


def _amount(value, label: str, allow_zero: bool = True) -> Decimal:
    try:
        amount = round_money(Decimal(str(value).strip() or "0"))
    except Exception:
        raise POSValidationError(f"{label} must be a number.")
    if amount < 0 or (amount == 0 and not allow_zero):
        raise POSValidationError(f"{label} must be more than 0." if not allow_zero else f"{label} can't be negative.")
    return amount


def open_session(tenant, user, opening_cash) -> CashSession:
    if current_session(tenant, user) is not None:
        raise POSValidationError("The day is already open on this counter.")
    session = CashSession(
        tenant_id=tenant.id,
        user_id=user.id,
        business_date=date.today(),
        opening_cash=_amount(opening_cash, "Opening cash"),
    )
    db.session.add(session)
    db.session.flush()
    return session


def add_movement(session: CashSession, user, kind: str, amount, reason: str) -> CashMovement:
    if not session.is_open:
        raise POSValidationError("This day is closed.")
    if kind not in (CashMovement.IN, CashMovement.OUT):
        raise POSValidationError("Pick cash in or cash out.")
    reason = (reason or "").strip()
    if not reason:
        raise POSValidationError("Say what the cash was for.")
    movement = CashMovement(
        tenant_id=session.tenant_id,
        session_id=session.id,
        kind=kind,
        amount=_amount(amount, "Amount", allow_zero=False),
        reason=reason[:255],
        created_by_id=user.id,
    )
    db.session.add(movement)
    db.session.flush()
    return movement


def session_activity(session: CashSession) -> dict:
    bills = (
        POSBill.query.filter(POSBill.cash_session_id == session.id)
        .order_by(POSBill.completed_at)
        .all()
    )
    live_bills = [b for b in bills if b.status != POSBillStatus.VOIDED]
    returns = POSReturn.query.filter_by(cash_session_id=session.id, status=POSReturnStatus.COMPLETED).all()
    # A refund belongs to the day it was paid out in, whichever day the
    # bill itself was from.
    refunded = POSBill.query.filter(
        POSBill.refund_session_id == session.id, POSBill.status == POSBillStatus.REFUNDED
    ).all()

    cash_sales = (
        db.session.query(db.func.coalesce(db.func.sum(POSPayment.amount), 0))
        .join(POSBill, POSBill.id == POSPayment.bill_id)
        .filter(
            POSBill.cash_session_id == session.id,
            POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
            POSPayment.mode == PaymentMode.CASH,
        )
        .scalar()
    )
    cash_refunds = sum((b.refunded_amount or ZERO for b in refunded if b.refund_mode in (None, PaymentMode.CASH)), ZERO)
    cash_returns = sum((r.grand_total for r in returns if r.refund_mode == PaymentMode.CASH), ZERO)
    cash_in = sum((m.amount for m in session.movements if m.kind == CashMovement.IN), ZERO)
    cash_out = sum((m.amount for m in session.movements if m.kind == CashMovement.OUT), ZERO)
    expected = session.opening_cash + Decimal(cash_sales) - cash_refunds - cash_returns + cash_in - cash_out

    summary = summarize_pos_activity(live_bills, returns, refunded_bills=refunded)

    return {
        "session": session,
        "bills": bills,
        "returns": returns,
        "refunded_bills": refunded,
        "voided_count": sum(1 for b in bills if b.status == POSBillStatus.VOIDED),
        "cash_sales": Decimal(cash_sales),
        "cash_refunds": cash_refunds,
        "cash_returns": cash_returns,
        "cash_in": cash_in,
        "cash_out": cash_out,
        "expected_cash": round_money(expected),
        **summary,
    }


def close_session(session: CashSession, user, counted_cash, note: str | None = None) -> CashSession:
    if not session.is_open:
        raise POSValidationError("This day is already closed.")
    counted = _amount(counted_cash, "Counted cash")
    expected = session_activity(session)["expected_cash"]
    session.counted_cash = counted
    session.expected_cash = expected
    session.difference = counted - expected
    session.closing_note = (note or "").strip() or None
    session.closed_at = utcnow()
    session.closed_by_id = user.id
    return session
