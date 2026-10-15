"""Profit & Loss, worked out from the documents (feature "business_reports").

There is no double-entry ledger yet. Instead every figure is an
*account line* - (account code, label, amount) - produced by small
functions, one per source (sales, returns, stock, purchases, expenses).
A ledger added later only has to supply the same account codes and the
report, comparison column and exports keep working.

  Revenue      sales excluding GST - invoices and counter bills, plus
               debit notes, less credit notes, returns and refunds.
               Margin-scheme sales count at their full selling price; the
               GST paid on the margin is shown as a deduction.
  COGS         opening stock + purchases (net of returns; the GST that
               can't be claimed is part of cost, claimable GST isn't)
               + opening stock entered during the period
               (+ transfers in - out, for one location) - closing stock,
               valued from the stock ledger at weighted-average cost.
               Damage, wastage and count differences fall into COGS
               through closing stock.
  Gross profit = Revenue - COGS.  Expenses by category (cost excluding
  claimable GST).  Net profit = Gross profit - Expenses.

Without the Inventory module there is no stock value to work from: the
fallback is Revenue - Purchases - Expenses, labelled a cash-basis
estimate.

One location: counter sales by the till's location (a branch login's own
location, everyone else the main store), invoices / notes at the main
store, purchases at their location, expenses by branch. Stock of a
third-party branch (an independent buyer) is never counted as the
client's own."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from app.extensions import db
from app.models.expense import Expense, ExpenseStatus
from app.models.inventory import MovementType, StockLocation, StockMovement
from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_series import DocumentType
from app.models.note import CreditDebitNote, NoteStatus
from app.models.pos_bill import POSBill, POSBillStatus
from app.models.pos_return import POSReturn, POSReturnStatus
from app.models.purchase import PurchaseBill, PurchaseReturn, PurchaseStatus
from app.models.user import BranchType, User

ZERO = Decimal("0.00")
PAISA = Decimal("0.01")
NOT_SALES = (DocumentType.DELIVERY_CHALLAN,)
ADDED_WITHOUT_PURCHASE = (MovementType.OPENING,)
TRANSFERS = (MovementType.TRANSFER_IN, MovementType.TRANSFER_OUT)
PURCHASE_MOVES = (MovementType.PURCHASE, MovementType.PURCHASE_RETURN)


def money(value) -> Decimal:
    return Decimal(value).quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass
class Line:
    code: str
    label: str
    amount: Decimal = ZERO


@dataclass
class Section:
    key: str
    title: str
    lines: list[Line] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((l.amount for l in self.lines), ZERO)

    def add(self, code, label, amount):
        for l in self.lines:
            if l.code == code:
                l.amount += amount
                return
        self.lines.append(Line(code, label, money(amount) if amount else ZERO))


def day_start(d: date) -> datetime:
    """Local midnight, as an aware datetime (POS times are stored aware)."""
    return datetime.combine(d, time.min).astimezone()


# --- where things happened ----------------------------------------------------------

@dataclass
class Places:
    default_id: int | None
    by_branch_user: dict  # branch login id -> location id
    excluded: set  # third-party branches' locations - not the client's stock

    def for_user(self, user_id):
        return self.by_branch_user.get(user_id, self.default_id)


def places(tenant) -> Places:
    locations = StockLocation.query.filter_by(tenant_id=tenant.id).all()
    default = next((l.id for l in locations if l.is_default), None)
    third_party = {u.id for u in User.query.filter_by(tenant_id=tenant.id, branch_type=BranchType.THIRD_PARTY)}
    return Places(default_id=default, by_branch_user={l.branch_user_id: l.id for l in locations if l.branch_user_id},
                  excluded={l.id for l in locations if l.branch_user_id in third_party})


def own_locations(tenant) -> list[StockLocation]:
    p = places(tenant)
    return [l for l in StockLocation.query.filter_by(tenant_id=tenant.id).order_by(StockLocation.is_default.desc(),
                                                                                   StockLocation.name)
            if l.id not in p.excluded]


# --- revenue ------------------------------------------------------------------------

def _margin_sale_value(lines) -> Decimal:
    return sum((Decimal(l.line_total) for l in lines), ZERO)


def revenue(tenant, start: date, end: date, location_id: int | None, p: Places) -> Section:
    s = Section("revenue", "Revenue")
    at_store = location_id is None or location_id == p.default_id
    margin_tax = ZERO

    if at_store:
        for inv in Invoice.query.filter(
            Invoice.tenant_id == tenant.id, Invoice.status == InvoiceStatus.ISSUED,
            Invoice.document_type.notin_(NOT_SALES), Invoice.invoice_date >= start, Invoice.invoice_date <= end,
        ):
            if inv.document_type == DocumentType.MARGIN_SCHEME_INVOICE:
                s.add("sales_invoices", "Sales - invoices", _margin_sale_value(inv.lines))
                margin_tax += inv.total_cgst + inv.total_sgst + inv.total_igst
            else:
                s.add("sales_invoices", "Sales - invoices", inv.total_taxable_value)
        for note in CreditDebitNote.query.filter(
            CreditDebitNote.tenant_id == tenant.id, CreditDebitNote.status == NoteStatus.ISSUED,
            CreditDebitNote.note_date >= start, CreditDebitNote.note_date <= end,
        ):
            if note.document_type == DocumentType.DEBIT_NOTE:
                s.add("debit_notes", "Debit notes", note.total_taxable_value)
            else:
                s.add("credit_notes", "Less: credit notes", -note.total_taxable_value)

    lo, hi = day_start(start), day_start(end + timedelta(days=1))
    bills = POSBill.query.filter(
        POSBill.tenant_id == tenant.id, POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
        POSBill.completed_at >= lo, POSBill.completed_at < hi,
    ).all()
    for bill in bills:
        if location_id is not None and p.for_user(bill.created_by_id) != location_id:
            continue
        if bill.is_margin_scheme:
            s.add("sales_counter", "Sales - counter (POS)", _margin_sale_value(bill.lines))
            margin_tax += bill.total_cgst + bill.total_sgst + bill.total_igst
        else:
            s.add("sales_counter", "Sales - counter (POS)", bill.total_taxable_value)
    refunded = POSBill.query.filter(
        POSBill.tenant_id == tenant.id, POSBill.status == POSBillStatus.REFUNDED,
        POSBill.refunded_at >= lo, POSBill.refunded_at < hi,
    ).all()
    for bill in refunded:
        if location_id is not None and p.for_user(bill.created_by_id) != location_id:
            continue
        if not bill.grand_total:
            continue
        ratio = min(Decimal(bill.refunded_amount or 0) / Decimal(bill.grand_total), Decimal("1"))
        base = _margin_sale_value(bill.lines) if bill.is_margin_scheme else Decimal(bill.total_taxable_value)
        s.add("returns", "Less: returns & refunds", -money(base * ratio))
        if bill.is_margin_scheme:
            margin_tax -= money((bill.total_cgst + bill.total_sgst + bill.total_igst) * ratio)
    for ret in POSReturn.query.filter(
        POSReturn.tenant_id == tenant.id, POSReturn.status == POSReturnStatus.COMPLETED,
        POSReturn.returned_at >= lo, POSReturn.returned_at < hi,
    ):
        if location_id is not None and p.for_user(ret.bill.created_by_id if ret.bill else None) != location_id:
            continue
        if ret.bill is not None and ret.bill.is_margin_scheme:
            s.add("returns", "Less: returns & refunds", -_margin_sale_value(ret.lines))
            margin_tax -= ret.total_cgst + ret.total_sgst + ret.total_igst
        else:
            s.add("returns", "Less: returns & refunds", -ret.total_taxable_value)
    if margin_tax:
        s.add("margin_tax", "Less: GST paid on margin-scheme sales", -margin_tax)
    return s


# --- stock & purchases --------------------------------------------------------------

def _movements(tenant, location_id, p: Places):
    q = StockMovement.query.filter(StockMovement.tenant_id == tenant.id)
    if location_id is not None:
        q = q.filter(StockMovement.location_id == location_id)
    elif p.excluded:
        q = q.filter(StockMovement.location_id.notin_(p.excluded))
    return q


def _value(query) -> Decimal:
    total = query.with_entities(db.func.coalesce(db.func.sum(StockMovement.qty * StockMovement.unit_cost), 0)).scalar()
    return money(total or 0)


def stock_value(tenant, before: date, location_id: int | None = None, p: Places | None = None) -> Decimal:
    """Value of stock on hand at the start of `before` (weighted average)."""
    p = p or places(tenant)
    return _value(_movements(tenant, location_id, p).filter(StockMovement.moved_at < day_start(before)))


def _period_value(tenant, start, end, location_id, p, types) -> Decimal:
    return _value(_movements(tenant, location_id, p).filter(
        StockMovement.moved_at >= day_start(start), StockMovement.moved_at < day_start(end + timedelta(days=1)),
        StockMovement.movement_type.in_(types)))


def _non_stock_purchases(tenant, start, end, location_id) -> tuple[Decimal, Decimal]:
    """Purchase bill lines that never reach stock (services, ad-hoc lines,
    non-stock items), at cost - and returns of them."""
    from app.inventory.ledger import sells_from_stock

    bought = returned = ZERO
    q = PurchaseBill.query.filter(PurchaseBill.tenant_id == tenant.id, PurchaseBill.status == PurchaseStatus.POSTED,
                                  PurchaseBill.bill_date >= start, PurchaseBill.bill_date <= end)
    if location_id is not None:
        q = q.filter(PurchaseBill.location_id == location_id)
    for bill in q:
        for line in bill.lines:
            if not sells_from_stock(line.product):
                bought += money(Decimal(line.qty) * Decimal(line.unit_cost))
    r = PurchaseReturn.query.filter(PurchaseReturn.tenant_id == tenant.id, PurchaseReturn.status == PurchaseStatus.POSTED,
                                    PurchaseReturn.return_date >= start, PurchaseReturn.return_date <= end)
    if location_id is not None:
        r = r.filter(PurchaseReturn.location_id == location_id)
    for ret in r:
        for line in ret.lines:
            if not sells_from_stock(line.product):
                returned += money(Decimal(line.qty) * Decimal(line.unit_cost))
    return bought, returned


def cost_of_goods(tenant, start, end, location_id, p: Places) -> Section:
    s = Section("cogs", "Cost of goods sold")
    opening = stock_value(tenant, start, location_id, p)
    closing = stock_value(tenant, end + timedelta(days=1), location_id, p)
    purchases_in = _period_value(tenant, start, end, location_id, p, (MovementType.PURCHASE,))
    returns_out = _period_value(tenant, start, end, location_id, p, (MovementType.PURCHASE_RETURN,))
    other_in, other_back = _non_stock_purchases(tenant, start, end, location_id)
    s.add("opening_stock", "Opening stock", opening)
    s.add("purchases", "Add: purchases", purchases_in + other_in)
    s.add("purchase_returns", "Less: purchase returns", returns_out - other_back)  # movements are negative
    entered = _period_value(tenant, start, end, location_id, p, ADDED_WITHOUT_PURCHASE)
    if entered:
        s.add("opening_entered", "Add: opening stock entered", entered)
    if location_id is not None:
        moved = _period_value(tenant, start, end, location_id, p, TRANSFERS)
        if moved:
            s.add("transfers", "Add: transfers in less out", moved)
    s.add("closing_stock", "Less: closing stock", -closing)
    return s


def cash_purchases(tenant, start, end, location_id) -> Section:
    """Cash-basis fallback: everything bought in the period, at cost."""
    s = Section("purchases", "Purchases")
    q = PurchaseBill.query.filter(PurchaseBill.tenant_id == tenant.id, PurchaseBill.status == PurchaseStatus.POSTED,
                                  PurchaseBill.bill_date >= start, PurchaseBill.bill_date <= end)
    r = PurchaseReturn.query.filter(PurchaseReturn.tenant_id == tenant.id, PurchaseReturn.status == PurchaseStatus.POSTED,
                                    PurchaseReturn.return_date >= start, PurchaseReturn.return_date <= end)
    if location_id is not None:
        q, r = q.filter(PurchaseBill.location_id == location_id), r.filter(PurchaseReturn.location_id == location_id)
    bought = sum((money(Decimal(l.qty) * Decimal(l.unit_cost)) for b in q for l in b.lines), ZERO)
    back = sum((money(Decimal(l.qty) * Decimal(l.unit_cost)) for x in r for l in x.lines), ZERO)
    s.add("purchases", "Purchases", bought)
    if back:
        s.add("purchase_returns", "Less: purchase returns", -back)
    return s


# --- expenses -----------------------------------------------------------------------

def expense_cost(e: Expense) -> Decimal:
    """What an expense really cost: claimable GST is not a cost."""
    if e.is_gst_bill:
        return Decimal(e.taxable_value) + Decimal(e.total_tax) - Decimal(e.itc_total)
    return Decimal(e.amount)


def expenses(tenant, start, end, location_id, p: Places) -> Section:
    s = Section("expenses", "Expenses")
    rows = Expense.query.filter(Expense.tenant_id == tenant.id, Expense.status == ExpenseStatus.POSTED,
                                Expense.expense_date >= start, Expense.expense_date <= end).all()
    by_cat = defaultdict(lambda: ZERO)
    names = {}
    for e in rows:
        if location_id is not None and p.for_user(e.branch_user_id) != location_id:
            continue
        by_cat[e.category_id] += expense_cost(e)
        names[e.category_id] = e.category.name
    for cat_id in sorted(by_cat, key=lambda c: names[c].lower()):
        s.add(f"expense:{cat_id}", names[cat_id], by_cat[cat_id])
    return s


# --- the statement ------------------------------------------------------------------

def profit_and_loss(tenant, start: date, end: date, location_id: int | None = None) -> dict:
    p = places(tenant)
    rev = revenue(tenant, start, end, location_id, p)
    exp = expenses(tenant, start, end, location_id, p)
    accrual = tenant.has_feature("inventory")
    if accrual:
        cost = cost_of_goods(tenant, start, end, location_id, p)
    else:
        cost = cash_purchases(tenant, start, end, location_id)
    net_revenue = rev.total
    gross = net_revenue - cost.total
    return {
        "start": start, "end": end, "location_id": location_id, "basis": "accrual" if accrual else "cash",
        "revenue": rev, "cost": cost, "expenses": exp,
        "net_revenue": net_revenue, "cost_total": cost.total, "gross_profit": gross,
        "gp_pct": (gross * 100 / net_revenue).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if net_revenue else None,
        "expense_total": exp.total, "net_profit": gross - exp.total,
    }


def comparison_period(start: date, end: date, mode: str) -> tuple[date, date] | None:
    if mode == "previous":
        days = (end - start).days + 1
        return start - timedelta(days=days), start - timedelta(days=1)
    if mode == "last_year":
        def back(d):
            try:
                return d.replace(year=d.year - 1)
            except ValueError:  # 29 Feb
                return d.replace(year=d.year - 1, day=28)
        return back(start), back(end)
    return None


def statement(tenant, start: date, end: date, location_id: int | None = None, compare: str = "previous") -> dict:
    """P&L with an optional comparison column, lines matched by code."""
    current = profit_and_loss(tenant, start, end, location_id)
    span = comparison_period(start, end, compare)
    prior = profit_and_loss(tenant, *span, location_id) if span else None
    rows = []
    for key in ("revenue", "cost", "expenses"):
        sec = current[key]
        other = prior[key] if prior else None
        codes = [l.code for l in sec.lines] + [l.code for l in (other.lines if other else []) if l.code not in
                                                 {x.code for x in sec.lines}]
        lines = []
        for code in codes:
            mine = next((l for l in sec.lines if l.code == code), None)
            theirs = next((l for l in other.lines if l.code == code), None) if other else None
            lines.append({"code": code, "label": (mine or theirs).label, "amount": mine.amount if mine else ZERO,
                          "prior": theirs.amount if theirs else (ZERO if other else None)})
        rows.append({"key": key, "title": sec.title, "lines": lines, "total": sec.total,
                     "prior_total": other.total if other else None})
    return {"current": current, "prior": prior, "prior_span": span, "compare": compare, "sections": rows,
            "tenant": tenant}
