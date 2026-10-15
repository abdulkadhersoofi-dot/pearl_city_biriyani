"""Documents as double-entry vouchers - the bridge to an accounting ledger.

Each sales / purchase / money document of a period becomes a balanced
Voucher (debits = credits, to the paisa) posting to named ledgers in
Tally's standard groups. Today it feeds the Tally export
(app.finance.tally); a ledger table added later can store exactly these
entries. Any paisa left by rounding goes to "Round Off", so every voucher
balances by construction - and a test asserts it.

Ledgers used:
  Sundry Debtors     each customer; "Counter Sales" for walk-in counter bills
  Sundry Creditors   each supplier
  Sales Accounts     "Sales @ <rate>%" (and "Sales - Margin Scheme")
  Purchase Accounts  "Purchases @ <rate>%"
  Duties & Taxes     Output / Input CGST, SGST, IGST; "RCM Tax Payable"
  Cash-in-Hand       "Cash"
  Bank Accounts      "Bank" (UPI, card, transfer, cheque, wallet)
  Indirect Expenses  each expense category; "Round Off"
  Direct Expenses    "Freight & Other Charges on Purchases"
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from app.finance.pnl import NOT_SALES, ZERO, day_start, money
from app.models.accounts import (
    ChequeStatus,
    CustomerReceipt,
    MoneyMode,
    RefundVoucher,
    SupplierPayment,
    VoucherStatus,
)
from app.models.expense import Expense, ExpensePaymentMode, ExpenseStatus
from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_series import DocumentType
from app.models.note import CreditDebitNote, NoteStatus
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus
from app.models.pos_return import POSReturn, POSReturnStatus
from app.models.purchase import PurchaseBill, PurchaseReturn, PurchaseStatus

GROUPS = {
    "debtor": "Sundry Debtors", "creditor": "Sundry Creditors", "sales": "Sales Accounts",
    "purchase": "Purchase Accounts", "tax": "Duties & Taxes", "cash": "Cash-in-Hand", "bank": "Bank Accounts",
    "expense": "Indirect Expenses", "direct": "Direct Expenses",
}
CASH, BANK, ROUND_OFF, COUNTER = "Cash", "Bank", "Round Off", "Counter Sales"
FREIGHT = "Freight & Other Charges on Purchases"
RCM_PAYABLE = "RCM Tax Payable"
MARGIN_SALES = "Sales - Margin Scheme"


@dataclass
class Ledger:
    name: str
    group: str
    gstin: str | None = None
    state: str | None = None
    tax_head: str | None = None  # "Central Tax" / "State Tax" / "Integrated Tax"


@dataclass
class Entry:
    ledger: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO


@dataclass
class Voucher:
    vch_type: str  # Sales / Credit Note / Debit Note / Purchase / Receipt / Payment
    on: date
    number: str
    party: str | None
    narration: str
    entries: list[Entry] = field(default_factory=list)

    def dr(self, ledger, amount):
        if amount:
            self.entries.append(Entry(ledger, debit=money(amount)))

    def cr(self, ledger, amount):
        if amount:
            self.entries.append(Entry(ledger, credit=money(amount)))

    @property
    def debits(self) -> Decimal:
        return sum((e.debit for e in self.entries), ZERO)

    @property
    def credits(self) -> Decimal:
        return sum((e.credit for e in self.entries), ZERO)

    def balance(self):
        """Puts any rounding difference on Round Off."""
        diff = self.debits - self.credits
        if diff > 0:
            self.cr(ROUND_OFF, diff)
        elif diff < 0:
            self.dr(ROUND_OFF, -diff)
        return self


class Book:
    """Ledger masters met while building vouchers - one name per party."""

    def __init__(self):
        self.ledgers: dict[str, Ledger] = {}
        for name, group in ((CASH, "cash"), (BANK, "bank"), (ROUND_OFF, "expense"), (COUNTER, "debtor")):
            self.add(name, group)
        for prefix in ("Output", "Input"):
            for head, duty in (("CGST", "Central Tax"), ("SGST", "State Tax"), ("IGST", "Integrated Tax")):
                self.add(f"{prefix} {head}", "tax", tax_head=duty)

    def add(self, name, group, gstin=None, state=None, tax_head=None) -> str:
        if name not in self.ledgers:
            self.ledgers[name] = Ledger(name, GROUPS[group], gstin, state, tax_head)
        return name

    def party(self, kind: str, obj_id: int | None, snapshot: dict | None) -> str:
        snap = snapshot or {}
        name = (snap.get("name") or "").strip() or COUNTER
        group = "debtor" if kind == "customer" else "creditor"
        existing = self.ledgers.get(name)
        if existing is not None and existing.group != GROUPS[group]:
            name = f"{name} ({'customer' if kind == 'customer' else 'supplier'})"
        return self.add(name, group, gstin=snap.get("gstin"), state=snap.get("state_name"))


def _money_ledger(mode) -> str:
    return CASH if mode in (MoneyMode.CASH, PaymentMode.CASH, ExpensePaymentMode.CASH) else BANK


def _sales_lines(v: Voucher, book: Book, lines, margin: bool, side: str, ratio=Decimal("1")):
    post = v.cr if side == "cr" else v.dr
    by_rate = defaultdict(lambda: ZERO)
    for line in lines:
        if margin:
            by_rate[None] += (Decimal(line.line_total) - Decimal(line.cgst_amount) - Decimal(line.sgst_amount)
                              - Decimal(line.igst_amount)) * ratio
        else:
            by_rate[Decimal(line.gst_rate)] += Decimal(line.taxable_value) * ratio
    for rate, amount in sorted(by_rate.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
        name = MARGIN_SALES if rate is None else f"Sales @ {rate.normalize():f}%"
        post(book.add(name, "sales"), amount)


def _taxes(v: Voucher, book: Book, doc, prefix: str, side: str, ratio=Decimal("1")):
    post = v.cr if side == "cr" else v.dr
    for head in ("cgst", "sgst", "igst"):
        post(f"{prefix} {head.upper()}", Decimal(getattr(doc, f"total_{head}")) * ratio)


def _sales_vouchers(tenant, start, end, book) -> list[Voucher]:
    out = []
    for inv in Invoice.query.filter(Invoice.tenant_id == tenant.id, Invoice.status == InvoiceStatus.ISSUED,
                                    Invoice.document_type.notin_(NOT_SALES), Invoice.invoice_date >= start,
                                    Invoice.invoice_date <= end).order_by(Invoice.invoice_date, Invoice.invoice_number):
        party = book.party("customer", inv.customer_id, inv.customer_snapshot)
        v = Voucher("Sales", inv.invoice_date, inv.invoice_number, party, f"Invoice {inv.invoice_number}")
        rcm_tax = Decimal(inv.total_rcgst) + Decimal(inv.total_rsgst) + Decimal(inv.total_rigst)
        v.dr(party, Decimal(inv.grand_total) - rcm_tax)
        _sales_lines(v, book, inv.lines, inv.document_type == DocumentType.MARGIN_SCHEME_INVOICE, "cr")
        _taxes(v, book, inv, "Output", "cr")
        out.append(v.balance())
    for note in CreditDebitNote.query.filter(CreditDebitNote.tenant_id == tenant.id,
                                             CreditDebitNote.status == NoteStatus.ISSUED,
                                             CreditDebitNote.note_date >= start, CreditDebitNote.note_date <= end):
        party = book.party("customer", note.customer_id, note.customer_snapshot)
        credit = note.document_type == DocumentType.CREDIT_NOTE
        v = Voucher("Credit Note" if credit else "Debit Note", note.note_date, note.note_number, party,
                    f"{'Credit' if credit else 'Debit'} note on {note.original_invoice.invoice_number if note.original_invoice else ''}: {note.reason}")
        if credit:
            _sales_lines(v, book, note.lines, False, "dr")
            _taxes(v, book, note, "Output", "dr")
            v.cr(party, note.grand_total)
        else:
            v.dr(party, note.grand_total)
            _sales_lines(v, book, note.lines, False, "cr")
            _taxes(v, book, note, "Output", "cr")
        out.append(v.balance())

    lo, hi = day_start(start), day_start(end + timedelta(days=1))
    for bill in POSBill.query.filter(POSBill.tenant_id == tenant.id,
                                     POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
                                     POSBill.completed_at >= lo, POSBill.completed_at < hi).order_by(POSBill.completed_at):
        party = book.party("customer", bill.customer_id, bill.customer_snapshot) if bill.customer_id else COUNTER
        v = Voucher("Sales", bill.completed_at.astimezone().date(), bill.bill_number, party, f"Counter bill {bill.bill_number}")
        payments = [(p.mode, Decimal(p.amount)) for p in bill.payments] or [(bill.payment_mode, Decimal(bill.grand_total))]
        for mode, amount in payments:
            if mode == PaymentMode.CREDIT:
                v.dr(party, amount)
            elif mode == PaymentMode.EXCHANGE:
                v.dr(COUNTER, amount)  # settled by goods returned - see the matching return voucher
            else:
                v.dr(_money_ledger(mode), amount)
        _sales_lines(v, book, bill.lines, bill.is_margin_scheme, "cr")
        _taxes(v, book, bill, "Output", "cr")
        out.append(v.balance())
    for bill in POSBill.query.filter(POSBill.tenant_id == tenant.id, POSBill.status == POSBillStatus.REFUNDED,
                                     POSBill.refunded_at >= lo, POSBill.refunded_at < hi):
        if not bill.grand_total:
            continue
        ratio = min(Decimal(bill.refunded_amount or 0) / Decimal(bill.grand_total), Decimal("1"))
        party = book.party("customer", bill.customer_id, bill.customer_snapshot) if bill.customer_id else COUNTER
        v = Voucher("Credit Note", bill.refunded_at.astimezone().date(), f"{bill.bill_number}-REFUND", party,
                    f"Refund of {bill.bill_number}: {bill.refund_reason or ''}")
        _sales_lines(v, book, bill.lines, bill.is_margin_scheme, "dr", ratio)
        _taxes(v, book, bill, "Output", "dr", ratio)
        mode = bill.refund_mode or PaymentMode.CASH
        v.cr(party if mode == PaymentMode.CREDIT else _money_ledger(mode), bill.refunded_amount)
        out.append(v.balance())
    for ret in POSReturn.query.filter(POSReturn.tenant_id == tenant.id, POSReturn.status == POSReturnStatus.COMPLETED,
                                      POSReturn.returned_at >= lo, POSReturn.returned_at < hi):
        bill = ret.bill
        party = book.party("customer", bill.customer_id, ret.customer_snapshot) if bill and bill.customer_id else COUNTER
        v = Voucher("Credit Note", ret.returned_at.astimezone().date(), ret.return_number, party,
                    f"Goods returned on {bill.bill_number if bill else ''}: {ret.reason}")
        _sales_lines(v, book, ret.lines, bool(bill and bill.is_margin_scheme), "dr")
        _taxes(v, book, ret, "Output", "dr")
        if ret.refund_mode == PaymentMode.CREDIT:
            v.cr(party, ret.grand_total)
        elif ret.refund_mode == PaymentMode.EXCHANGE:
            v.cr(COUNTER, ret.grand_total)
        else:
            v.cr(_money_ledger(ret.refund_mode), ret.grand_total)
        out.append(v.balance())
    return out


def _purchase_vouchers(tenant, start, end, book) -> list[Voucher]:
    out = []
    for bill in PurchaseBill.query.filter(PurchaseBill.tenant_id == tenant.id, PurchaseBill.status == PurchaseStatus.POSTED,
                                          PurchaseBill.bill_date >= start, PurchaseBill.bill_date <= end
                                          ).order_by(PurchaseBill.bill_date, PurchaseBill.number):
        party = book.party("supplier", bill.supplier_id, bill.supplier_snapshot)
        v = Voucher("Purchase", bill.bill_date, bill.number, party,
                    f"Supplier invoice {bill.supplier_invoice_no} dated {bill.supplier_invoice_date.isoformat()}")
        by_rate = defaultdict(lambda: ZERO)
        for line in bill.lines:
            tax = Decimal(line.cgst_amount) + Decimal(line.sgst_amount) + Decimal(line.igst_amount)
            credit_taken = tax if (line.itc_eligibility.value == "eligible" and bill.itc_total > 0) else ZERO
            # GST that can't be claimed is part of what the goods cost.
            by_rate[Decimal(line.gst_rate)] += Decimal(line.taxable_value) + (tax - credit_taken)
        for rate, amount in sorted(by_rate.items()):
            v.dr(book.add(f"Purchases @ {rate.normalize():f}%", "purchase"), amount)
        if bill.other_charges:
            v.dr(book.add(FREIGHT, "direct"), bill.other_charges)
        for head in ("cgst", "sgst", "igst"):
            v.dr(f"Input {head.upper()}", getattr(bill, f"itc_{head}"))
        if bill.is_rcm:
            v.cr(book.add(RCM_PAYABLE, "tax"), bill.total_tax)
            # non-claimable RCM tax already went into cost above
        v.cr(party, bill.grand_total)
        out.append(v.balance())
    for ret in PurchaseReturn.query.filter(PurchaseReturn.tenant_id == tenant.id,
                                           PurchaseReturn.status == PurchaseStatus.POSTED,
                                           PurchaseReturn.return_date >= start, PurchaseReturn.return_date <= end):
        party = book.party("supplier", ret.supplier_id, ret.supplier_snapshot)
        v = Voucher("Debit Note", ret.return_date, ret.number, party, f"Goods returned: {ret.reason}")
        v.dr(party, ret.grand_total)
        by_rate = defaultdict(lambda: ZERO)
        for line in ret.lines:
            tax = Decimal(line.cgst_amount) + Decimal(line.sgst_amount) + Decimal(line.igst_amount)
            credit = tax if line.itc_eligibility.value == "eligible" and (ret.itc_cgst + ret.itc_sgst + ret.itc_igst) > 0 else ZERO
            by_rate[Decimal(line.gst_rate)] += Decimal(line.taxable_value) + tax - credit
        for rate, amount in sorted(by_rate.items()):
            v.cr(book.add(f"Purchases @ {rate.normalize():f}%", "purchase"), amount)
        for head in ("cgst", "sgst", "igst"):
            v.cr(f"Input {head.upper()}", getattr(ret, f"itc_{head}"))
        out.append(v.balance())
    return out


def _money_vouchers(tenant, start, end, book) -> list[Voucher]:
    out = []
    for r in CustomerReceipt.query.filter(CustomerReceipt.tenant_id == tenant.id, CustomerReceipt.status == VoucherStatus.POSTED,
                                          CustomerReceipt.voucher_date >= start, CustomerReceipt.voucher_date <= end):
        if r.cheque_status == ChequeStatus.BOUNCED:
            continue
        party = book.party("customer", r.customer_id, {"name": r.customer.name, "gstin": r.customer.gstin,
                                                       "state_name": r.customer.state_name})
        v = Voucher("Receipt", r.voucher_date, r.number, party,
                    ("Advance received" if r.is_advance else "Received") + (f" - cheque {r.cheque_no}" if r.cheque_no else ""))
        v.dr(_money_ledger(r.mode), r.amount)
        v.cr(party, r.amount)
        out.append(v)
    for rf in RefundVoucher.query.filter(RefundVoucher.tenant_id == tenant.id, RefundVoucher.status == VoucherStatus.POSTED,
                                         RefundVoucher.voucher_date >= start, RefundVoucher.voucher_date <= end):
        party = book.party("customer", rf.customer_id, {"name": rf.customer.name, "gstin": rf.customer.gstin,
                                                        "state_name": rf.customer.state_name})
        v = Voucher("Payment", rf.voucher_date, rf.number, party, f"Refund of advance: {rf.reason}")
        v.dr(party, rf.amount)
        v.cr(_money_ledger(rf.mode), rf.amount)
        out.append(v)
    for p in SupplierPayment.query.filter(SupplierPayment.tenant_id == tenant.id, SupplierPayment.status == VoucherStatus.POSTED,
                                          SupplierPayment.voucher_date >= start, SupplierPayment.voucher_date <= end):
        if p.cheque_status == ChequeStatus.BOUNCED:
            continue
        party = book.party("supplier", p.supplier_id, p.supplier.snapshot())
        v = Voucher("Payment", p.voucher_date, p.number, party, "Paid" + (f" - cheque {p.cheque_no}" if p.cheque_no else ""))
        v.dr(party, p.amount)
        v.cr(_money_ledger(p.mode), p.amount)
        out.append(v)
    for e in Expense.query.filter(Expense.tenant_id == tenant.id, Expense.status == ExpenseStatus.POSTED,
                                  Expense.expense_date >= start, Expense.expense_date <= end):
        v = Voucher("Journal" if e.payment_mode == ExpensePaymentMode.UNPAID else "Payment", e.expense_date, e.number,
                    None, f"{e.category.name} - {e.payee}")
        if e.is_gst_bill:
            v.dr(book.add(e.category.name, "expense"),
                 Decimal(e.taxable_value) + Decimal(e.total_tax) - Decimal(e.itc_total))
            for head in ("cgst", "sgst", "igst"):
                v.dr(f"Input {head.upper()}", getattr(e, f"itc_{head}"))
            if e.is_rcm:
                v.cr(book.add(RCM_PAYABLE, "tax"), e.total_tax)
        else:
            v.dr(book.add(e.category.name, "expense"), e.amount)
        if e.payment_mode == ExpensePaymentMode.UNPAID and e.supplier is not None:
            v.party = book.party("supplier", e.supplier_id, e.supplier.snapshot())
            v.cr(v.party, e.amount)
        elif e.payment_mode == ExpensePaymentMode.UNPAID:
            v.party = book.add(f"{e.payee} (payable)", "creditor")
            v.cr(v.party, e.amount)
        else:
            v.cr(_money_ledger(e.payment_mode), e.amount)
        out.append(v.balance())
    return out


def build(tenant, start: date, end: date) -> tuple[list[Ledger], list[Voucher]]:
    book = Book()
    vouchers = _sales_vouchers(tenant, start, end, book) + _purchase_vouchers(tenant, start, end, book) \
        + _money_vouchers(tenant, start, end, book)
    vouchers.sort(key=lambda v: (v.on, v.vch_type, v.number))
    used = {e.ledger for v in vouchers for e in v.entries} | {v.party for v in vouchers if v.party}
    return [l for l in book.ledgers.values() if l.name in used], vouchers
