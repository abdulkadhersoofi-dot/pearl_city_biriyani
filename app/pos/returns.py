"""Item-wise sales returns (and exchanges) against a POS bill.

Picking the original bill and how many of each line came back creates a
POSReturn - its own numbered document (SR/<FY>/0001) that reduces sales
and output tax in the period it happens (a B2B return is reported like a
credit note in GSTR-1), puts the goods back into the branch's stock count
(app.pos.stock), and is paid back in cash/UPI/... or kept as exchange
credit for a new bill. The original bill is never touched. Amount-only
refunds (app.pos.services.refund_bill) still work for old bills.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.pos_bill import PaymentMode, POSBill, POSBillLine, POSBillStatus
from app.models.pos_return import POSReturn, POSReturnLine, POSReturnStatus
from app.pos.services import POSValidationError, enabled_payment_modes
from app.utils.financial_year import financial_year_for
from app.utils.gst import compute_invoice_totals, return_line_amounts
from app.utils.numbering import allocate_number, get_or_create_series

ZERO = Decimal("0")
_AMOUNT_FIELDS = ("discount_amount", "taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "line_total")


@dataclass
class ReturnableLine:
    line: POSBillLine
    returned_qty: Decimal
    returned: dict

    @property
    def remaining_qty(self) -> Decimal:
        return self.line.qty - self.returned_qty


def returnable_lines(bill: POSBill) -> list[ReturnableLine]:
    """Each line of the bill with how much of it has already come back
    (voided returns don't count)."""
    done: dict[int, dict] = {}
    for ret in bill.returns:
        if ret.status != POSReturnStatus.COMPLETED:
            continue
        for rl in ret.lines:
            entry = done.setdefault(rl.bill_line_id, {"qty": ZERO, **{f: ZERO for f in _AMOUNT_FIELDS}})
            entry["qty"] += rl.qty
            for f in _AMOUNT_FIELDS:
                entry[f] += getattr(rl, f)
    result = []
    for line in bill.lines:
        entry = done.get(line.id, {"qty": ZERO})
        result.append(ReturnableLine(line=line, returned_qty=entry["qty"], returned={k: v for k, v in entry.items() if k != "qty"}))
    return result


def return_modes_for(bill: POSBill) -> list[PaymentMode]:
    """How a return on this bill can be paid back: any mode the counter
    takes, exchange credit, and "reduce what they owe" (credit) only when
    the bill itself was on credit."""
    modes = [m for m in enabled_payment_modes(bill_tenant(bill)) if m != PaymentMode.CREDIT]
    if any(p.mode == PaymentMode.CREDIT for p in bill.payments):
        modes.append(PaymentMode.CREDIT)
    modes.append(PaymentMode.EXCHANGE)
    return modes


def bill_tenant(bill: POSBill):
    from app.models.tenant import Tenant

    return db.session.get(Tenant, bill.tenant_id)


def create_return(
    bill: POSBill,
    created_by,
    quantities: dict[int, Decimal],
    reason: str,
    refund_mode: PaymentMode,
    cash_session=None,
) -> POSReturn:
    if bill.status == POSBillStatus.REFUNDED:
        raise POSValidationError("This bill was already refunded.")
    if bill.status != POSBillStatus.COMPLETED:
        raise POSValidationError("Only a completed bill can have items returned.")
    reason = (reason or "").strip()
    if not reason:
        raise POSValidationError("A reason is required for a return.")
    if refund_mode not in return_modes_for(bill):
        raise POSValidationError("Pick how the money goes back to the customer.")

    picked = {line_id: qty for line_id, qty in quantities.items() if qty and qty > 0}
    if not picked:
        raise POSValidationError("Enter how many of at least one item came back.")

    computed = []
    for entry in returnable_lines(bill):
        qty = picked.pop(entry.line.id, None)
        if qty is None:
            continue
        line = entry.line
        if qty > entry.remaining_qty:
            raise POSValidationError(f"Only {entry.remaining_qty.normalize():f} of {line.description} can still come back.")
        amounts = return_line_amounts(
            original={
                "rate": line.rate,
                "discount_percent": line.discount_percent,
                "discount_flat": line.discount_flat,
                "gst_rate": line.gst_rate,
                "price_includes_tax": line.price_includes_tax,
                "is_intra_state": not line.igst_amount,
                **{f: getattr(line, f) for f in _AMOUNT_FIELDS},
            },
            original_qty=line.qty,
            already_returned=entry.returned,
            already_returned_qty=entry.returned_qty,
            return_qty=qty,
        )
        computed.append((line, qty, amounts))
    if picked:
        raise POSValidationError("An item picked isn't on this bill.")

    totals = compute_invoice_totals([amounts for _, _, amounts in computed])
    series = get_or_create_series(bill.tenant_id, bill.gstin_id, DocumentType.POS_RETURN, financial_year_for(date.today()))
    ret = POSReturn(
        tenant_id=bill.tenant_id,
        gstin_id=bill.gstin_id,
        invoice_series_id=series.id,
        return_number=allocate_number(series),
        bill=bill,  # through the relationship, so bill.returns sees it at once
        customer_snapshot=bill.customer_snapshot,
        status=POSReturnStatus.COMPLETED,
        reason=reason,
        refund_mode=refund_mode,
        cash_session_id=cash_session.id if cash_session is not None else None,
        created_by_id=created_by.id,
        returned_at=utcnow(),
        **totals,
    )
    db.session.add(ret)
    db.session.flush()
    for order, (line, qty, amounts) in enumerate(computed):
        ret.lines.append(
            POSReturnLine(
                bill_line_id=line.id,
                product_id=line.product_id,
                description=line.description,
                hsn_or_sac_code=line.hsn_or_sac_code,
                qty=qty,
                unit=line.unit,
                rate=line.rate,
                gst_rate=line.gst_rate,
                price_includes_tax=line.price_includes_tax,
                sort_order=order,
                **amounts,
            )
        )
    db.session.flush()
    from app.inventory import hooks as inventory_hooks
    from app.inventory import ledger as inventory_ledger

    location = inventory_ledger.location_for_user(bill.tenant_id, created_by)
    inventory_hooks.post_return(bill.tenant_id, location.id, ret, created_by.id)
    return ret


def void_return(ret: POSReturn, voided_by, reason: str, today: date | None = None) -> None:
    """Cancels a return entered by mistake - the same day (or while its
    counter day is open), and only if none of its exchange credit has
    been spent."""
    if ret.status != POSReturnStatus.COMPLETED:
        raise POSValidationError("This return is already voided.")
    reason = (reason or "").strip()
    if not reason:
        raise POSValidationError("A reason is required to void a return.")
    if ret.exchange_used and ret.exchange_used > 0:
        raise POSValidationError("Part of this return's exchange credit was already used on a bill.")
    if ret.cash_session_id:
        from app.models.cash_session import CashSession

        session = db.session.get(CashSession, ret.cash_session_id)
        if session is not None and not session.is_open:
            raise POSValidationError("That day is closed - the return can't be voided now.")
    elif ret.returned_at.astimezone().date() != (today or date.today()):
        raise POSValidationError("Only today's returns can be voided.")
    from app.inventory import ledger as inventory_ledger

    inventory_ledger.reverse_source(ret.tenant_id, "pos_return", ret.id, voided_by.id, f"{ret.return_number} voided")
    ret.status = POSReturnStatus.VOIDED
    ret.void_reason = reason
    ret.voided_at = utcnow()
    ret.voided_by_id = voided_by.id
