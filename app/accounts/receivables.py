"""What each customer owes - worked out from the documents themselves, never
stored as a running balance that could drift.

A customer's account (feature "receivables"):
  + opening balance (what they owed when the client started here)
  + issued invoices billed to them (not branch transfers or delivery
    challans; a reverse-charge invoice without the tax the customer pays
    the government directly)
  + debit notes, - credit notes against those invoices
  + the "credit" (on account) part of counter bills; a bill voided at the
    counter owes nothing, and a return / refund paid back "to credit"
    comes off it
  - receipts (a bounced cheque settles nothing)
  + refund vouchers (an advance given back)

Bill-wise: every invoice / credit bill / opening balance is an open item;
receipts are set against items (allocations) and what's left of a
receipt stays "on account". Balance = open items' outstanding - on
account, the same number the running ledger ends on.

Only documents dated on/after the tenant's books opening date count (if
one is set) - anything older is in the opening balances."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models.accounts import (
    ChequeStatus,
    CustomerReceipt,
    ReceiptAllocation,
    RefundVoucher,
    VoucherStatus,
)
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_series import DocumentType
from app.models.note import CreditDebitNote, NoteStatus
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus, POSPayment
from app.models.pos_return import POSReturn, POSReturnStatus

ZERO = Decimal("0.00")
AGE_BUCKETS = [("0-30", 0, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("90+", 91, None)]
NOT_RECEIVABLE_TYPES = (DocumentType.DELIVERY_CHALLAN,)


def bill_date(bill: POSBill) -> date | None:
    return bill.completed_at.astimezone().date() if bill.completed_at else None


def invoice_receivable(invoice: Invoice) -> Decimal:
    """What the customer pays on an invoice: under reverse charge the tax
    shown on it is paid by them to the government, not to the client."""
    return Decimal(invoice.grand_total) - (Decimal(invoice.total_rcgst) + Decimal(invoice.total_rsgst)
                                           + Decimal(invoice.total_rigst))


@dataclass
class OpenItem:
    kind: str  # "opening" | "invoice" | "pos_bill"
    id: int | None
    number: str
    doc_date: date
    due_date: date
    amount: Decimal  # owed on it (after notes / returns)
    settled: Decimal = ZERO

    @property
    def outstanding(self) -> Decimal:
        return self.amount - self.settled

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id or 0}"


@dataclass
class LedgerEntry:
    entry_date: date
    kind: str
    number: str
    description: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    link: tuple | None = None  # (endpoint, kwargs)
    balance: Decimal = ZERO
    order: int = 0


@dataclass
class CustomerAccount:
    customer: Customer
    items: list[OpenItem] = field(default_factory=list)
    on_account: Decimal = ZERO  # receipts not yet set against any bill
    entries: list[LedgerEntry] = field(default_factory=list)

    @property
    def open_items(self) -> list[OpenItem]:
        return [i for i in self.items if i.outstanding > 0]

    @property
    def balance(self) -> Decimal:
        return sum((i.outstanding for i in self.items), ZERO) - self.on_account

    def ageing(self, as_of: date) -> dict:
        buckets = {name: ZERO for name, _, _ in AGE_BUCKETS}
        for item in self.open_items:
            days = (as_of - item.doc_date).days
            for name, low, high in AGE_BUCKETS:
                if days >= low and (high is None or days <= high) or (days < 0 and low == 0):
                    buckets[name] += item.outstanding
                    break
        buckets["on_account"] = self.on_account
        buckets["balance"] = self.balance
        return buckets

    def overdue(self, as_of: date) -> Decimal:
        return sum((i.outstanding for i in self.open_items if i.due_date < as_of), ZERO)


def opening_date(tenant, customer: Customer) -> date:
    return customer.opening_balance_date or tenant.books_opening_date or (
        customer.created_at.date() if customer.created_at else date.today())


def _start(tenant) -> date | None:
    return tenant.books_opening_date


def _due(doc_date: date, customer: Customer) -> date:
    from datetime import timedelta

    return doc_date + timedelta(days=customer.credit_days or 0)


def _receipt_settles(receipt: CustomerReceipt) -> bool:
    return receipt.status == VoucherStatus.POSTED and receipt.cheque_status != ChequeStatus.BOUNCED


def customer_invoices(tenant, customer_id: int):
    query = Invoice.query.filter(
        Invoice.tenant_id == tenant.id, Invoice.customer_id == customer_id, Invoice.status == InvoiceStatus.ISSUED,
        Invoice.branch_user_id.is_(None), Invoice.document_type.notin_(NOT_RECEIVABLE_TYPES),
    )
    if _start(tenant):
        query = query.filter(Invoice.invoice_date >= _start(tenant))
    return query.order_by(Invoice.invoice_date, Invoice.id).all()


def credit_bills(tenant, customer_id: int):
    """Counter bills to this customer with a credit (on account) part."""
    credit_ids = db.session.query(POSPayment.bill_id).filter(
        POSPayment.tenant_id == tenant.id, POSPayment.mode == PaymentMode.CREDIT)
    bills = POSBill.query.filter(
        POSBill.tenant_id == tenant.id, POSBill.customer_id == customer_id,
        POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]), POSBill.id.in_(credit_ids),
    ).order_by(POSBill.completed_at, POSBill.id).all()
    start = _start(tenant)
    return [b for b in bills if not start or (bill_date(b) and bill_date(b) >= start)]


def _bill_credit_back(bill: POSBill) -> list[tuple[date, str, Decimal, object]]:
    """Returns / refunds on a credit bill paid back by reducing what's owed."""
    out = []
    for ret in bill.returns:
        if ret.status == POSReturnStatus.COMPLETED and ret.refund_mode == PaymentMode.CREDIT:
            out.append((ret.returned_at.astimezone().date(), ret.return_number, Decimal(ret.grand_total), ret))
    if bill.status == POSBillStatus.REFUNDED and bill.refund_mode == PaymentMode.CREDIT:
        refunded_on = bill.refunded_at.astimezone().date() if bill.refunded_at else bill_date(bill)
        out.append((refunded_on, f"Refund of {bill.bill_number}", Decimal(bill.refunded_amount or 0), None))
    return out


def customer_account(tenant, customer: Customer, with_entries: bool = True) -> CustomerAccount:
    account = CustomerAccount(customer=customer)
    entries = account.entries
    items_by_key: dict[str, OpenItem] = {}

    opening = Decimal(customer.opening_balance or 0)
    if opening:
        odate = opening_date(tenant, customer)
        if opening > 0:
            item = OpenItem("opening", None, "Opening balance", odate, odate, opening)
            items_by_key[item.key] = item
        else:
            account.on_account += -opening
        entries.append(LedgerEntry(odate, "opening", "", "Opening balance",
                                   debit=opening if opening > 0 else ZERO, credit=-opening if opening < 0 else ZERO,
                                   order=0))

    for inv in customer_invoices(tenant, customer.id):
        amount = invoice_receivable(inv)
        entries.append(LedgerEntry(inv.invoice_date, "invoice", inv.invoice_number, "Invoice", debit=amount,
                                   link=("invoicing.view_invoice", {"invoice_id": inv.id}), order=1))
        item = OpenItem("invoice", inv.id, inv.invoice_number, inv.invoice_date, _due(inv.invoice_date, customer), amount)
        items_by_key[item.key] = item

    invoice_ids = [i.id for k, i in items_by_key.items() if i.kind == "invoice"]
    if invoice_ids:
        notes = CreditDebitNote.query.filter(
            CreditDebitNote.tenant_id == tenant.id, CreditDebitNote.original_invoice_id.in_(invoice_ids),
            CreditDebitNote.status == NoteStatus.ISSUED,
        ).order_by(CreditDebitNote.note_date, CreditDebitNote.id).all()
        for note in notes:
            item = items_by_key[f"invoice:{note.original_invoice_id}"]
            amount = Decimal(note.grand_total)
            if note.document_type == DocumentType.CREDIT_NOTE:
                item.amount -= amount
                entries.append(LedgerEntry(note.note_date, "credit_note", note.note_number, f"Credit note on {item.number}",
                                           credit=amount, link=("notes.view_note", {"note_id": note.id}), order=2))
            else:
                item.amount += amount
                entries.append(LedgerEntry(note.note_date, "debit_note", note.note_number, f"Debit note on {item.number}",
                                           debit=amount, link=("notes.view_note", {"note_id": note.id}), order=2))

    for bill in credit_bills(tenant, customer.id):
        amount = Decimal(bill.credit_amount)
        bdate = bill_date(bill)
        entries.append(LedgerEntry(bdate, "pos_bill", bill.bill_number, "Counter bill (credit part)", debit=amount,
                                   link=("pos.view_receipt", {"bill_id": bill.id}), order=1))
        item = OpenItem("pos_bill", bill.id, bill.bill_number, bdate, _due(bdate, customer), amount)
        for back_date, number, back, _ret in _bill_credit_back(bill):
            item.amount -= back
            entries.append(LedgerEntry(back_date, "pos_return", number, f"Goods returned on {bill.bill_number}",
                                       credit=back, order=2))
        items_by_key[item.key] = item

    receipts = CustomerReceipt.query.filter(
        CustomerReceipt.tenant_id == tenant.id, CustomerReceipt.customer_id == customer.id,
        CustomerReceipt.status == VoucherStatus.POSTED,
    ).order_by(CustomerReceipt.voucher_date, CustomerReceipt.id).all()
    refunds_by_receipt = defaultdict(lambda: ZERO)
    for refund in RefundVoucher.query.filter_by(tenant_id=tenant.id, customer_id=customer.id,
                                                status=VoucherStatus.POSTED).order_by(RefundVoucher.voucher_date).all():
        refunds_by_receipt[refund.receipt_id] += Decimal(refund.amount)
        entries.append(LedgerEntry(refund.voucher_date, "refund", refund.number, "Refund voucher", debit=Decimal(refund.amount),
                                   link=("accounts.view_refund", {"refund_id": refund.id}), order=4))

    for receipt in receipts:
        label = "Advance received" if receipt.is_advance else "Receipt"
        entries.append(LedgerEntry(receipt.voucher_date, "receipt", receipt.number, f"{label} ({receipt.mode.value})",
                                   credit=Decimal(receipt.amount),
                                   link=("accounts.view_receipt", {"receipt_id": receipt.id}), order=3))
        if not _receipt_settles(receipt):
            entries.append(LedgerEntry(receipt.cleared_on or receipt.voucher_date, "bounce", receipt.number,
                                       f"Cheque {receipt.cheque_no or ''} bounced".replace("  ", " "),
                                       debit=Decimal(receipt.amount),
                                       link=("accounts.view_receipt", {"receipt_id": receipt.id}), order=5))
            continue
        allocated = ZERO
        for alloc in receipt.allocations:
            key = "opening:0" if alloc.is_opening else (
                f"invoice:{alloc.invoice_id}" if alloc.invoice_id else f"pos_bill:{alloc.pos_bill_id}")
            if key in items_by_key:
                items_by_key[key].settled += Decimal(alloc.amount)
                allocated += Decimal(alloc.amount)
        account.on_account += Decimal(receipt.amount) - allocated - refunds_by_receipt[receipt.id]

    # A bill paid more than it now owes (a return after it was paid) has
    # its excess counted as on account rather than a negative item.
    for item in items_by_key.values():
        if item.outstanding < 0:
            account.on_account += -item.outstanding
            item.settled = item.amount
    account.items = sorted(items_by_key.values(), key=lambda i: (i.doc_date, i.number))

    if with_entries:
        entries.sort(key=lambda e: (e.entry_date, e.order, e.number))
        running = ZERO
        for e in entries:
            running += e.debit - e.credit
            e.balance = running
    return account


def item_outstanding(tenant, customer: Customer, kind: str, item_id: int | None) -> Decimal | None:
    account = customer_account(tenant, customer, with_entries=False)
    for item in account.items:
        if item.kind == kind and (item.id or None) == (item_id or None):
            return item.outstanding
    return None


def customer_balances(tenant, as_of: date | None = None) -> list[dict]:
    """Every customer with anything on their account, with ageing."""
    as_of = as_of or date.today()
    rows = []
    for customer in Customer.query.filter_by(tenant_id=tenant.id).order_by(Customer.name).all():
        account = customer_account(tenant, customer, with_entries=False)
        if not account.items and not account.on_account:
            continue
        ageing = account.ageing(as_of)
        if not any(ageing[name] for name, _, _ in AGE_BUCKETS) and not account.on_account:
            continue
        rows.append({"customer": customer, "ageing": ageing, "balance": account.balance,
                     "overdue": account.overdue(as_of),
                     "over_limit": bool(customer.credit_limit and account.balance > customer.credit_limit)})
    return rows


def balance_of(tenant, customer: Customer) -> Decimal:
    return customer_account(tenant, customer, with_entries=False).balance


def credit_warning(tenant, customer: Customer | None, adding: Decimal = ZERO) -> str | None:
    """A warning (never a block) when this sale takes the customer past
    their credit limit. None when accounts are off or no limit is set."""
    if customer is None or not tenant.has_feature("receivables") or not customer.credit_limit:
        return None
    balance = balance_of(tenant, customer)
    total = balance + Decimal(adding or 0)
    if total > Decimal(customer.credit_limit):
        return (f"{customer.name} now owes Rs. {total:,.2f} - over their credit limit of "
                f"Rs. {Decimal(customer.credit_limit):,.2f}.")
    return None


def statement(tenant, customer: Customer, start: date, end: date) -> dict:
    """Ledger for a period: balance brought forward, the entries, closing."""
    account = customer_account(tenant, customer)
    brought = ZERO
    rows = []
    for e in account.entries:
        if e.entry_date < start:
            brought = e.balance
        elif e.entry_date <= end:
            rows.append(e)
    closing = rows[-1].balance if rows else brought
    return {"customer": customer, "start": start, "end": end, "opening": brought, "rows": rows, "closing": closing,
            "debits": sum((r.debit for r in rows), ZERO), "credits": sum((r.credit for r in rows), ZERO),
            "account": account, "ageing": account.ageing(end), "tenant": tenant}
