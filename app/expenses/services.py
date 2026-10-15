"""Expenses: entering, posting and voiding expense vouchers, the
categories they're filed under, recurring expenses that make a draft
each period, and petty cash paid out of the POS drawer.

An expense is posted (numbered EXP/<FY>/0001, from the same row-locked
allocator as every other document) or a draft. A posted expense is never
deleted - it's voided with a reason. A draft that isn't wanted (this
month's rent was paid some other way) is voided the same way, so the
recurring expense doesn't make it again. GST on an expense is computed by
app.utils.itc, like a purchase bill's."""

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models.expense import (
    DEFAULT_CATEGORIES,
    PETTY_CASH_CATEGORY,
    Expense,
    ExpenseCategory,
    ExpensePaymentMode,
    ExpenseStatus,
    Frequency,
    RecurringExpense,
)
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.purchase import ItcEligibility, Supplier
from app.models.tenant import Gstin
from app.models.user import User, UserRole
from app.utils import itc
from app.utils.financial_year import financial_year_for
from app.utils.gst import GSTIN_REGEX, normalize_gstin
from app.utils.gst_config import GST_RATE_SLABS
from app.utils.numbering import allocate_number, get_or_create_series

ZERO = Decimal("0")
MAX_DRAFTS_PER_RUN = 36  # a recurring expense left a year or more behind catches up in steps


class ExpenseError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- categories ---------------------------------------------------------------------

def ensure_categories(tenant_id: int) -> list[ExpenseCategory]:
    """The client's categories, made from the standard list the first time
    they're needed. The client can rename, add and switch them off."""
    rows = ExpenseCategory.query.filter_by(tenant_id=tenant_id).all()
    if not rows:
        for name in DEFAULT_CATEGORIES:
            db.session.add(ExpenseCategory(tenant_id=tenant_id, name=name))
        db.session.flush()
        rows = ExpenseCategory.query.filter_by(tenant_id=tenant_id).all()
    return sorted((c for c in rows if c.is_active), key=lambda c: c.name.lower())


def add_category(tenant_id: int, name: str) -> ExpenseCategory:
    name = (name or "").strip()[:100]
    if not name:
        raise ExpenseError("Give the category a name.")
    ensure_categories(tenant_id)
    existing = ExpenseCategory.query.filter(ExpenseCategory.tenant_id == tenant_id,
                                            db.func.lower(ExpenseCategory.name) == name.lower()).first()
    if existing is not None:
        if existing.is_active:
            raise ExpenseError(f"There's already a category called {existing.name}.")
        existing.is_active = True  # switched back on
        return existing
    category = ExpenseCategory(tenant_id=tenant_id, name=name)
    db.session.add(category)
    db.session.flush()
    return category


def rename_category(category: ExpenseCategory, name: str) -> None:
    name = (name or "").strip()[:100]
    if not name:
        raise ExpenseError("Give the category a name.")
    clash = ExpenseCategory.query.filter(ExpenseCategory.tenant_id == category.tenant_id,
                                         db.func.lower(ExpenseCategory.name) == name.lower(),
                                         ExpenseCategory.id != category.id).first()
    if clash is not None:
        raise ExpenseError(f"There's already a category called {clash.name}.")
    category.name = name


def _category(tenant_id: int, category_id) -> ExpenseCategory:
    category = ExpenseCategory.query.filter_by(id=category_id, tenant_id=tenant_id, is_active=True).first()
    if category is None:
        raise ExpenseError("Pick a category.")
    return category


# --- entering an expense -------------------------------------------------------------

@dataclass
class ExpenseInput:
    expense_date: date | None
    category_id: int | None
    amount: Decimal
    payment_mode: ExpensePaymentMode = ExpensePaymentMode.CASH
    payee_name: str | None = None
    supplier_id: int | None = None
    reference: str | None = None
    branch_user_id: int | None = None
    notes: str | None = None
    attachment_id: int | None = None
    # GST bill
    is_gst_bill: bool = False
    gstin_id: int | None = None
    supplier_gstin: str | None = None
    bill_number: str | None = None
    gst_rate: Decimal = ZERO
    amount_includes_gst: bool = True
    is_rcm: bool = False
    itc_eligibility: ItcEligibility = ItcEligibility.ELIGIBLE


def money(value, label: str) -> Decimal:
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        raise ExpenseError(f"{label} must be a number.")
    if amount.is_nan() or amount.is_infinite():
        raise ExpenseError(f"{label} must be a number.")
    return amount.quantize(Decimal("0.01"))


def _gstin(tenant, gstin_id) -> Gstin:
    query = Gstin.query.filter_by(tenant_id=tenant.id, is_active=True)
    gstin = query.filter_by(id=gstin_id).first() if gstin_id else query.order_by(Gstin.is_primary.desc(), Gstin.id).first()
    if gstin is None:
        raise ExpenseError("Pick which of your registrations this expense is for.")
    return gstin


def _apply(expense: Expense, tenant, data: ExpenseInput) -> None:
    from app.purchases.services import can_claim_itc

    if data.expense_date is None:
        raise ExpenseError("Enter the date of the expense.")
    if data.expense_date > date.today():
        raise ExpenseError("An expense can't be dated in the future.")
    category = _category(tenant.id, data.category_id)
    supplier = None
    if data.supplier_id:
        supplier = Supplier.query.filter_by(id=data.supplier_id, tenant_id=tenant.id).first()
        if supplier is None:
            raise ExpenseError("Pick a valid supplier.")
    payee = (data.payee_name or "").strip()[:255] or None
    if supplier is None and payee is None:
        raise ExpenseError("Say who was paid.")
    branch = None
    if data.branch_user_id:
        branch = User.query.filter_by(id=data.branch_user_id, tenant_id=tenant.id, role=UserRole.STAFF).first()
        if branch is None:
            raise ExpenseError("Pick a valid branch.")
    amount = Decimal(data.amount)
    if amount <= 0:
        raise ExpenseError("The amount must be more than 0.")

    expense.expense_date = data.expense_date
    expense.category_id = category.id
    expense.supplier_id = supplier.id if supplier else None
    expense.payee_name = payee
    expense.payment_mode = data.payment_mode
    expense.reference = (data.reference or "").strip()[:100] or None
    expense.branch_user_id = branch.id if branch else None
    expense.notes = (data.notes or "").strip() or None
    if data.attachment_id:
        expense.attachment_id = data.attachment_id

    gstin = _gstin(tenant, data.gstin_id)
    expense.gstin_id = gstin.id
    expense.is_gst_bill = bool(data.is_gst_bill)
    if not expense.is_gst_bill:
        expense.amount = amount
        expense.supplier_gstin = expense.supplier_state_code = expense.bill_number = None
        expense.is_rcm = False
        expense.gst_rate = expense.taxable_value = ZERO
        expense.cgst_amount = expense.sgst_amount = expense.igst_amount = ZERO
        expense.itc_eligibility = ItcEligibility.INELIGIBLE
        expense.itc_cgst = expense.itc_sgst = expense.itc_igst = ZERO
        return

    rate = Decimal(data.gst_rate or 0)
    if rate not in {Decimal(str(s)) for s in GST_RATE_SLABS}:
        raise ExpenseError(f"{rate}% isn't a GST rate.")
    supplier_gstin = normalize_gstin(data.supplier_gstin or (supplier.gstin if supplier else "") or "") or None
    if supplier_gstin and not re.match(GSTIN_REGEX, supplier_gstin):
        raise ExpenseError(f"{supplier_gstin} isn't a valid 15-character GSTIN.")
    if not supplier_gstin and not data.is_rcm:
        # Credit needs the supplier's GSTIN on the bill (s.16(2)); an
        # unregistered supplier's bill carries no GST to begin with.
        raise ExpenseError("Enter the supplier's GSTIN from the bill (an unregistered supplier doesn't charge GST).")
    supplier_state = (supplier_gstin[:2] if supplier_gstin else None) or (supplier.state_code if supplier else None) \
        or gstin.state_code
    claim = can_claim_itc(tenant, gstin)
    eligibility = data.itc_eligibility if claim else ItcEligibility.INELIGIBLE
    gst = itc.compute_expense_gst(amount, rate, supplier_state == gstin.state_code, data.amount_includes_gst,
                                  data.is_rcm, eligibility, claim)
    expense.supplier_gstin = supplier_gstin
    expense.supplier_state_code = supplier_state
    expense.bill_number = (data.bill_number or "").strip()[:50] or None
    expense.is_rcm = bool(data.is_rcm)
    expense.gst_rate = rate
    expense.itc_eligibility = eligibility
    for key, value in gst.items():
        setattr(expense, key, value)


def create_expense(tenant, user, data: ExpenseInput, post: bool = True) -> Expense:
    expense = Expense(tenant_id=tenant.id, created_by_id=user.id, status=ExpenseStatus.DRAFT)
    _apply(expense, tenant, data)
    db.session.add(expense)
    db.session.flush()
    if post:
        post_expense(expense, user)
    return expense


def update_draft(expense: Expense, tenant, data: ExpenseInput) -> None:
    if expense.status != ExpenseStatus.DRAFT:
        raise ExpenseError("Only a draft can be changed - void a posted expense and enter it again.")
    _apply(expense, tenant, data)
    db.session.flush()


def post_expense(expense: Expense, user) -> None:
    """Gives the expense its voucher number. Numbers only ever go to posted
    expenses, so drafts never leave gaps in the series."""
    if expense.status != ExpenseStatus.DRAFT:
        raise ExpenseError("This expense is already posted or voided.")
    if expense.expense_date > date.today():
        raise ExpenseError("An expense can't be posted before its date.")
    series = get_or_create_series(expense.tenant_id, expense.gstin_id, DocumentType.EXPENSE,
                                  financial_year_for(expense.expense_date))
    expense.invoice_series_id = series.id
    expense.number = allocate_number(series)
    expense.status = ExpenseStatus.POSTED
    expense.posted_at = utcnow()


def void_expense(expense: Expense, user, reason: str) -> None:
    if expense.status == ExpenseStatus.VOIDED:
        raise ExpenseError("This expense is already voided.")
    reason = (reason or "").strip()
    if not reason:
        raise ExpenseError("A reason is required.")
    expense.status = ExpenseStatus.VOIDED
    expense.void_reason = reason
    expense.voided_at = utcnow()
    expense.voided_by_id = user.id


# --- recurring expenses ----------------------------------------------------------------

def _add_months(d: date, months: int, anchor_day: int) -> date:
    month_index = d.month - 1 + months
    year, month = d.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(anchor_day, calendar.monthrange(year, month)[1]))


def next_due(current: date, frequency: Frequency, anchor_day: int) -> date:
    """The due date after `current`. Monthly dates stay on the start day,
    or the month's last day when it's shorter (rent due on the 31st falls
    on 30 April, then back to 31 May)."""
    if frequency == Frequency.WEEKLY:
        return current + timedelta(days=7)
    if frequency == Frequency.MONTHLY:
        return _add_months(current, 1, anchor_day)
    if frequency == Frequency.QUARTERLY:
        return _add_months(current, 3, anchor_day)
    return _add_months(current, 12, anchor_day)


@dataclass
class RecurringInput:
    name: str
    category_id: int | None
    amount: Decimal
    frequency: Frequency
    start_date: date | None
    end_date: date | None = None
    payment_mode: ExpensePaymentMode = ExpensePaymentMode.BANK
    payee_name: str | None = None
    supplier_id: int | None = None
    branch_user_id: int | None = None
    notes: str | None = None


def save_recurring(tenant, user, data: RecurringInput, recurring: RecurringExpense | None = None) -> RecurringExpense:
    name = (data.name or "").strip()[:100]
    if not name:
        raise ExpenseError("Give it a name, e.g. Shop rent.")
    category = _category(tenant.id, data.category_id)
    if Decimal(data.amount) <= 0:
        raise ExpenseError("The amount must be more than 0.")
    if data.start_date is None:
        raise ExpenseError("Enter when it starts.")
    if data.end_date and data.end_date < data.start_date:
        raise ExpenseError("It can't end before it starts.")
    supplier_id = None
    if data.supplier_id:
        if Supplier.query.filter_by(id=data.supplier_id, tenant_id=tenant.id).first() is None:
            raise ExpenseError("Pick a valid supplier.")
        supplier_id = data.supplier_id
    if not supplier_id and not (data.payee_name or "").strip():
        raise ExpenseError("Say who is paid.")
    branch_id = None
    if data.branch_user_id:
        if User.query.filter_by(id=data.branch_user_id, tenant_id=tenant.id, role=UserRole.STAFF).first() is None:
            raise ExpenseError("Pick a valid branch.")
        branch_id = data.branch_user_id
    if recurring is None:
        recurring = RecurringExpense(tenant_id=tenant.id, created_by_id=user.id, next_due_date=data.start_date)
        db.session.add(recurring)
    elif recurring.start_date != data.start_date or recurring.frequency != data.frequency:
        # A new schedule starts from its own start date, skipping any
        # periods that already have a draft or voucher.
        recurring.next_due_date = data.start_date
    recurring.name = name
    recurring.category_id = category.id
    recurring.amount = Decimal(data.amount).quantize(Decimal("0.01"))
    recurring.frequency = data.frequency
    recurring.start_date = data.start_date
    recurring.end_date = data.end_date
    recurring.payment_mode = data.payment_mode
    recurring.payee_name = (data.payee_name or "").strip()[:255] or None
    recurring.supplier_id = supplier_id
    recurring.branch_user_id = branch_id
    recurring.notes = (data.notes or "").strip() or None
    db.session.flush()
    return recurring


def generate_due_drafts(tenant, today: date | None = None) -> list[Expense]:
    """A draft expense for every period that has come due on every active
    recurring expense. Safe to run any number of times: each period gets
    exactly one draft (unique recurring_id + due_date), and the schedule
    moves on as drafts are made."""
    today = today or date.today()
    made: list[Expense] = []
    recurring_rows = RecurringExpense.query.filter(
        RecurringExpense.tenant_id == tenant.id, RecurringExpense.is_active.is_(True),
        RecurringExpense.next_due_date <= today,
    ).with_for_update().all()
    if not recurring_rows:
        return made
    gstin = _gstin(tenant, None)
    for rec in recurring_rows:
        anchor = rec.start_date.day
        due = rec.next_due_date
        steps = 0
        while due <= today and (rec.end_date is None or due <= rec.end_date) and steps < MAX_DRAFTS_PER_RUN:
            exists = Expense.query.filter_by(recurring_id=rec.id, due_date=due).first()
            if exists is None:
                draft = Expense(
                    tenant_id=tenant.id, status=ExpenseStatus.DRAFT, expense_date=due, due_date=due,
                    recurring_id=rec.id, category_id=rec.category_id, payee_name=rec.payee_name,
                    supplier_id=rec.supplier_id, payment_mode=rec.payment_mode, branch_user_id=rec.branch_user_id,
                    amount=rec.amount, notes=rec.notes, gstin_id=gstin.id, created_by_id=rec.created_by_id,
                    itc_eligibility=ItcEligibility.INELIGIBLE,
                )
                db.session.add(draft)
                made.append(draft)
            due = next_due(due, rec.frequency, anchor)
            steps += 1
        rec.next_due_date = due
        if rec.end_date is not None and due > rec.end_date:
            rec.is_active = False
    db.session.flush()
    return made


# --- petty cash from the POS drawer -----------------------------------------------------

def petty_cash_expense(tenant, user, movement, category_id: int | None) -> Expense:
    """Cash paid out of the POS drawer (a CashMovement "out") booked as a
    posted cash expense of the counter that paid it."""
    categories = ensure_categories(tenant.id)
    category = next((c for c in categories if c.id == category_id), None) if category_id else None
    if category is None:
        category = next((c for c in categories if c.name == PETTY_CASH_CATEGORY), None) or categories[0]
    expense = create_expense(tenant, user, ExpenseInput(
        expense_date=date.today(),
        category_id=category.id, amount=Decimal(movement.amount), payment_mode=ExpensePaymentMode.CASH,
        payee_name=movement.reason, branch_user_id=user.id if getattr(user, "is_staff", False) else None,
        notes="Petty cash paid from the counter drawer",
    ))
    expense.cash_movement_id = movement.id
    db.session.flush()
    return expense
