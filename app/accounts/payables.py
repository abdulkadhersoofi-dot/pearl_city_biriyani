"""What the client owes each supplier (feature "payables"), worked out from
the documents - same shape as app.accounts.receivables:

  + opening balance (Supplier.opening_balance)
  + posted purchase bills (grand total = what's payable; under reverse
    charge that's without the tax, which goes to the government)
  - posted purchase returns (debit notes) against those bills
  + posted expenses booked to this supplier as "unpaid"
  - payment vouchers (a bounced cheque pays nothing)

Payments are set against bills / unpaid expenses / the opening balance;
the rest is an advance paid ("on account")."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from app.accounts.receivables import AGE_BUCKETS, LedgerEntry, OpenItem
from app.models.accounts import ChequeStatus, SupplierPayment, VoucherStatus
from app.models.expense import Expense, ExpensePaymentMode, ExpenseStatus
from app.models.purchase import PurchaseBill, PurchaseReturn, PurchaseStatus, Supplier

ZERO = Decimal("0.00")


@dataclass
class SupplierAccount:
    supplier: Supplier
    items: list[OpenItem] = field(default_factory=list)
    on_account: Decimal = ZERO
    entries: list[LedgerEntry] = field(default_factory=list)

    @property
    def open_items(self):
        return [i for i in self.items if i.outstanding > 0]

    @property
    def balance(self) -> Decimal:
        return sum((i.outstanding for i in self.items), ZERO) - self.on_account

    def ageing(self, as_of: date) -> dict:
        buckets = {name: ZERO for name, _, _ in AGE_BUCKETS}
        for item in self.open_items:
            days = max((as_of - item.doc_date).days, 0)
            for name, low, high in AGE_BUCKETS:
                if days >= low and (high is None or days <= high):
                    buckets[name] += item.outstanding
                    break
        buckets["on_account"] = self.on_account
        buckets["balance"] = self.balance
        return buckets

    def overdue(self, as_of: date) -> Decimal:
        return sum((i.outstanding for i in self.open_items if i.due_date < as_of), ZERO)


def _due(doc_date: date, supplier: Supplier) -> date:
    return doc_date + timedelta(days=supplier.payment_terms_days or 0)


def supplier_account(tenant, supplier: Supplier, with_entries: bool = True) -> SupplierAccount:
    account = SupplierAccount(supplier=supplier)
    entries = account.entries
    items: dict[str, OpenItem] = {}
    start = tenant.books_opening_date

    opening = Decimal(supplier.opening_balance or 0)
    if opening:
        odate = supplier.opening_balance_date or start or (supplier.created_at.date() if supplier.created_at else date.today())
        if opening > 0:
            item = OpenItem("opening", None, "Opening balance", odate, odate, opening)
            items[item.key] = item
        else:
            account.on_account += -opening
        entries.append(LedgerEntry(odate, "opening", "", "Opening balance", credit=opening if opening > 0 else ZERO,
                                   debit=-opening if opening < 0 else ZERO))

    bills = PurchaseBill.query.filter_by(tenant_id=tenant.id, supplier_id=supplier.id, status=PurchaseStatus.POSTED)
    if start:
        bills = bills.filter(PurchaseBill.bill_date >= start)
    for bill in bills.order_by(PurchaseBill.bill_date, PurchaseBill.id).all():
        amount = Decimal(bill.grand_total)
        item = OpenItem("purchase_bill", bill.id, f"{bill.number} ({bill.supplier_invoice_no})", bill.bill_date,
                        _due(bill.bill_date, supplier), amount)
        items[item.key] = item
        entries.append(LedgerEntry(bill.bill_date, "purchase_bill", bill.number, f"Purchase bill {bill.supplier_invoice_no}",
                                   credit=amount, link=("purchases.view_bill", {"bill_id": bill.id}), order=1))
    bill_ids = [i.id for i in items.values() if i.kind == "purchase_bill"]
    if bill_ids:
        for ret in PurchaseReturn.query.filter(PurchaseReturn.tenant_id == tenant.id,
                                               PurchaseReturn.purchase_bill_id.in_(bill_ids),
                                               PurchaseReturn.status == PurchaseStatus.POSTED).all():
            amount = Decimal(ret.grand_total)
            items[f"purchase_bill:{ret.purchase_bill_id}"].amount -= amount
            entries.append(LedgerEntry(ret.return_date, "purchase_return", ret.number, "Goods returned (debit note)",
                                       debit=amount, link=("purchases.view_return", {"return_id": ret.id}), order=2))

    expenses = Expense.query.filter_by(tenant_id=tenant.id, supplier_id=supplier.id, status=ExpenseStatus.POSTED,
                                       payment_mode=ExpensePaymentMode.UNPAID)
    if start:
        expenses = expenses.filter(Expense.expense_date >= start)
    for exp in expenses.order_by(Expense.expense_date, Expense.id).all():
        amount = Decimal(exp.amount)
        item = OpenItem("expense", exp.id, exp.number, exp.expense_date, _due(exp.expense_date, supplier), amount)
        items[item.key] = item
        entries.append(LedgerEntry(exp.expense_date, "expense", exp.number, "Expense (unpaid)", credit=amount,
                                   link=("expenses.view_expense", {"expense_id": exp.id}), order=1))

    payments = SupplierPayment.query.filter_by(tenant_id=tenant.id, supplier_id=supplier.id, status=VoucherStatus.POSTED)
    for pay in payments.order_by(SupplierPayment.voucher_date, SupplierPayment.id).all():
        entries.append(LedgerEntry(pay.voucher_date, "payment", pay.number, f"Payment ({pay.mode.value})",
                                   debit=Decimal(pay.amount), link=("accounts.view_payment", {"payment_id": pay.id}),
                                   order=3))
        if pay.cheque_status == ChequeStatus.BOUNCED:
            entries.append(LedgerEntry(pay.cleared_on or pay.voucher_date, "bounce", pay.number, "Cheque bounced",
                                       credit=Decimal(pay.amount),
                                       link=("accounts.view_payment", {"payment_id": pay.id}), order=5))
            continue
        allocated = ZERO
        for alloc in pay.allocations:
            key = "opening:0" if alloc.is_opening else (
                f"purchase_bill:{alloc.purchase_bill_id}" if alloc.purchase_bill_id else f"expense:{alloc.expense_id}")
            if key in items:
                items[key].settled += Decimal(alloc.amount)
                allocated += Decimal(alloc.amount)
        account.on_account += Decimal(pay.amount) - allocated

    for item in items.values():
        if item.outstanding < 0:
            account.on_account += -item.outstanding
            item.settled = item.amount
    account.items = sorted(items.values(), key=lambda i: (i.doc_date, i.number))
    if with_entries:
        entries.sort(key=lambda e: (e.entry_date, e.order, e.number))
        running = ZERO
        for e in entries:
            running += e.credit - e.debit  # what is owed to them
            e.balance = running
    return account


def supplier_balances(tenant, as_of: date | None = None) -> list[dict]:
    as_of = as_of or date.today()
    rows = []
    for supplier in Supplier.query.filter_by(tenant_id=tenant.id).order_by(Supplier.name).all():
        account = supplier_account(tenant, supplier, with_entries=False)
        if not account.open_items and not account.on_account:
            continue
        rows.append({"supplier": supplier, "ageing": account.ageing(as_of), "balance": account.balance,
                     "overdue": account.overdue(as_of)})
    return rows
