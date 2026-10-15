"""Cash book and bank book (feature "cash_bank_book") - derived, never typed
in: every rupee that went into or out of the cash drawer / bank account,
from the documents that moved it.

  Cash book                          Bank book
  + counter sales paid in cash       + counter sales by UPI / card / bank /
  + cash put in the drawer             cheque / wallet
  + receipts from customers in cash  + receipts by UPI / card / bank, and
  - counter refunds / returns paid     cheques once cleared (on the day they
    back in cash                       cleared; a bounced one never lands)
  - cash taken out of the drawer     - counter refunds / returns paid back
    (petty cash; its expense voucher   that way
    isn't counted again)             - expenses, supplier payments and refund
  - expenses, supplier payments,       vouchers paid that way (cheques once
    refund vouchers paid in cash       cleared)

The book starts from the opening cash in hand / bank balance on the
books opening date (Settings on the book page); without one it starts at
zero from the first document."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.models.accounts import ChequeStatus, CustomerReceipt, MoneyMode, RefundVoucher, SupplierPayment, VoucherStatus
from app.models.cash_session import CashMovement
from app.models.expense import Expense, ExpensePaymentMode, ExpenseStatus
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus
from app.models.pos_return import POSReturn, POSReturnStatus

ZERO = Decimal("0.00")
POS_BANK_MODES = {PaymentMode.UPI, PaymentMode.CARD, PaymentMode.BANK_TRANSFER, PaymentMode.CHEQUE, PaymentMode.WALLET}
EXPENSE_BANK_MODES = {ExpensePaymentMode.UPI, ExpensePaymentMode.CARD, ExpensePaymentMode.BANK, ExpensePaymentMode.CHEQUE}


@dataclass
class BookEntry:
    entry_date: date
    particulars: str
    number: str
    money_in: Decimal = ZERO
    money_out: Decimal = ZERO
    link: tuple | None = None
    balance: Decimal = ZERO


def _local(dt) -> date | None:
    return dt.astimezone().date() if dt else None


def _pos_book(mode: PaymentMode) -> str | None:
    if mode == PaymentMode.CASH:
        return "cash"
    if mode in POS_BANK_MODES:
        return "bank"
    return None  # credit / exchange move no money


def _voucher_lands(voucher) -> tuple[str, date] | None:
    """(book, date) a receipt / payment / refund lands in, or None."""
    if voucher.mode == MoneyMode.CASH:
        return "cash", voucher.voucher_date
    if voucher.mode == MoneyMode.CHEQUE:
        status = getattr(voucher, "cheque_status", ChequeStatus.CLEARED)
        if status is None:  # a refund voucher by cheque has no clearing step
            return "bank", voucher.voucher_date
        return ("bank", voucher.cleared_on) if status == ChequeStatus.CLEARED and voucher.cleared_on else None
    return "bank", voucher.voucher_date


def all_entries(tenant, book: str) -> list[BookEntry]:
    out: list[BookEntry] = []
    tid = tenant.id

    bills = POSBill.query.filter(POSBill.tenant_id == tid,
                                 POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED])).all()
    for bill in bills:
        on = _local(bill.completed_at)
        payments = [(p.mode, p.amount) for p in bill.payments] or (
            [(bill.payment_mode, bill.grand_total)] if bill.payment_mode else [])
        amount = sum((Decimal(a) for m, a in payments if _pos_book(m) == book), ZERO)
        if amount:
            out.append(BookEntry(on, "Counter sale", bill.bill_number, money_in=amount,
                                 link=("pos.view_receipt", {"bill_id": bill.id})))
        if bill.status == POSBillStatus.REFUNDED and _pos_book(bill.refund_mode or PaymentMode.CASH) == book:
            out.append(BookEntry(_local(bill.refunded_at) or on, "Counter refund", bill.bill_number,
                                 money_out=Decimal(bill.refunded_amount or 0),
                                 link=("pos.view_receipt", {"bill_id": bill.id})))

    for ret in POSReturn.query.filter_by(tenant_id=tid, status=POSReturnStatus.COMPLETED).all():
        if _pos_book(ret.refund_mode) == book:
            out.append(BookEntry(_local(ret.returned_at), "Goods returned at the counter", ret.return_number,
                                 money_out=Decimal(ret.grand_total), link=("pos.view_return", {"return_id": ret.id})))

    if book == "cash":
        for mv in CashMovement.query.filter_by(tenant_id=tid).all():
            entry = BookEntry(_local(mv.created_at), f"Drawer: {mv.reason}", "")
            if mv.kind == CashMovement.IN:
                entry.money_in = Decimal(mv.amount)
            else:
                entry.money_out = Decimal(mv.amount)
            out.append(entry)

    expenses = Expense.query.filter(Expense.tenant_id == tid, Expense.status == ExpenseStatus.POSTED,
                                    Expense.cash_movement_id.is_(None)).all()
    for exp in expenses:
        exp_book = "cash" if exp.payment_mode == ExpensePaymentMode.CASH else (
            "bank" if exp.payment_mode in EXPENSE_BANK_MODES else None)
        if exp_book == book:
            out.append(BookEntry(exp.expense_date, f"Expense - {exp.category.name} ({exp.payee})", exp.number,
                                 money_out=Decimal(exp.amount), link=("expenses.view_expense", {"expense_id": exp.id})))

    for receipt in CustomerReceipt.query.filter_by(tenant_id=tid, status=VoucherStatus.POSTED).all():
        landed = _voucher_lands(receipt)
        if landed and landed[0] == book:
            out.append(BookEntry(landed[1], f"Received from {receipt.customer.name}", receipt.number,
                                 money_in=Decimal(receipt.amount),
                                 link=("accounts.view_receipt", {"receipt_id": receipt.id})))
    for refund in RefundVoucher.query.filter_by(tenant_id=tid, status=VoucherStatus.POSTED).all():
        landed = _voucher_lands(refund)
        if landed and landed[0] == book:
            out.append(BookEntry(landed[1], f"Refund to {refund.customer.name}", refund.number,
                                 money_out=Decimal(refund.amount), link=("accounts.view_refund", {"refund_id": refund.id})))
    for pay in SupplierPayment.query.filter_by(tenant_id=tid, status=VoucherStatus.POSTED).all():
        landed = _voucher_lands(pay)
        if landed and landed[0] == book:
            out.append(BookEntry(landed[1], f"Paid to {pay.supplier.name}", pay.number, money_out=Decimal(pay.amount),
                                 link=("accounts.view_payment", {"payment_id": pay.id})))
    return [e for e in out if e.entry_date is not None]


def book(tenant, which: str, start: date, end: date) -> dict:
    opening_date = tenant.books_opening_date
    opening = Decimal((tenant.cash_opening_balance if which == "cash" else tenant.bank_opening_balance) or 0)
    entries = sorted(all_entries(tenant, which), key=lambda e: (e.entry_date, e.number))
    if opening_date:
        entries = [e for e in entries if e.entry_date >= opening_date]
    brought = opening
    rows = []
    for e in entries:
        if e.entry_date < start:
            brought += e.money_in - e.money_out
        elif e.entry_date <= end:
            rows.append(e)
    running = brought
    for e in rows:
        running += e.money_in - e.money_out
        e.balance = running
    return {"start": start, "end": end, "opening": brought, "rows": rows, "closing": running,
            "total_in": sum((e.money_in for e in rows), ZERO), "total_out": sum((e.money_out for e in rows), ZERO),
            "opening_date": opening_date, "book": which,
            "before_opening": bool(opening_date and start < opening_date)}
