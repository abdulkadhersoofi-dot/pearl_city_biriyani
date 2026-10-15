"""Expenses (module "expenses"): what the business spends that isn't stock
- rent, salaries, electricity, fuel, a courier, tea from the drawer.

An Expense is a numbered voucher (EV/<FY>/0001) once posted, never
deleted, voided with a reason. A draft (made by a recurring expense, or
waiting to be checked) has no number yet. When it was a GST bill, its tax
is stored the same way a purchase bill's is, so its eligible input tax
credit reaches the GSTR-3B working paper."""

import enum

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin
from app.models.purchase import ItcEligibility

DEFAULT_CATEGORIES = (
    "Rent",
    "Salary & Wages",
    "Electricity",
    "Fuel",
    "Transport",
    "Repairs",
    "Telephone/Internet",
    "Professional Fees",
    "Bank Charges",
    "Packing Material",
    "Advertisement",
    "Office Expenses",
    "Miscellaneous",
)
# Where petty cash from the POS drawer lands unless the cashier picks another.
PETTY_CASH_CATEGORY = "Miscellaneous"


class ExpenseCategory(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "expense_categories"
    __table_args__ = (db.UniqueConstraint("tenant_id", "name", name="uq_expense_categories_tenant_name"),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())

    def __repr__(self):
        return f"<ExpenseCategory {self.id} {self.name}>"


class ExpenseStatus(str, enum.Enum):
    DRAFT = "draft"
    POSTED = "posted"
    VOIDED = "voided"


class ExpensePaymentMode(str, enum.Enum):
    CASH = "cash"
    UPI = "upi"
    CARD = "card"
    BANK = "bank"
    CHEQUE = "cheque"
    UNPAID = "unpaid"


PAYMENT_MODE_LABELS = {
    ExpensePaymentMode.CASH: "Cash",
    ExpensePaymentMode.UPI: "UPI",
    ExpensePaymentMode.CARD: "Card",
    ExpensePaymentMode.BANK: "Bank transfer",
    ExpensePaymentMode.CHEQUE: "Cheque",
    ExpensePaymentMode.UNPAID: "Not paid yet",
}


class Expense(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "expenses"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "number", name="uq_expenses_tenant_number"),
        # One draft per recurring expense per due date, however often
        # the generator runs.
        db.UniqueConstraint("recurring_id", "due_date", name="uq_expenses_recurring_due"),
        db.UniqueConstraint("cash_movement_id", name="uq_expenses_cash_movement"),
    )

    id = db.Column(db.Integer, primary_key=True)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_expenses_series"))
    number = db.Column(db.String(64))  # set when posted
    status = db.Column(db.Enum(ExpenseStatus, name="expense_status"), nullable=False, default=ExpenseStatus.DRAFT)
    expense_date = db.Column(db.Date, nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("expense_categories.id", name="fk_expenses_category"), nullable=False)
    payee_name = db.Column(db.String(255))
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id", name="fk_expenses_supplier"))
    payment_mode = db.Column(db.Enum(ExpensePaymentMode, name="expense_payment_mode"), nullable=False,
                             default=ExpensePaymentMode.CASH)
    reference = db.Column(db.String(100))  # cheque / UTR / bill number
    # Head office when empty, else the branch login the expense belongs to.
    branch_user_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_expenses_branch_user"), index=True)
    # What was paid (or owed) to the payee. For a GST bill: taxable + GST,
    # except under reverse charge, where the payee gets the taxable value only.
    amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    # GST bill details - all zero / empty for an expense with no GST bill.
    is_gst_bill = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_expenses_gstin"))
    supplier_gstin = db.Column(db.String(15))
    supplier_state_code = db.Column(db.String(2))
    bill_number = db.Column(db.String(50))
    is_rcm = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0, server_default="0")
    taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    cgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    sgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    igst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    itc_eligibility = db.Column(db.Enum(ItcEligibility, name="itc_eligibility"), nullable=False,
                                default=ItcEligibility.ELIGIBLE)
    itc_cgst = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    itc_sgst = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    itc_igst = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")

    notes = db.Column(db.Text)
    attachment_id = db.Column(db.Integer, db.ForeignKey("uploaded_images.id", name="fk_expenses_attachment"))
    recurring_id = db.Column(db.Integer, db.ForeignKey("recurring_expenses.id", name="fk_expenses_recurring"), index=True)
    due_date = db.Column(db.Date)  # the period a recurring draft is for
    cash_movement_id = db.Column(db.Integer, db.ForeignKey("cash_movements.id", name="fk_expenses_cash_movement"))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_expenses_created_by"), nullable=False)
    posted_at = db.Column(db.DateTime(timezone=True))
    voided_at = db.Column(db.DateTime(timezone=True))
    void_reason = db.Column(db.Text)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_expenses_voided_by"))

    category = db.relationship("ExpenseCategory")
    supplier = db.relationship("Supplier")
    branch_user = db.relationship("User", foreign_keys=[branch_user_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    gstin = db.relationship("Gstin")
    recurring = db.relationship("RecurringExpense", back_populates="expenses")
    cash_movement = db.relationship("CashMovement")

    @property
    def total_tax(self):
        return self.cgst_amount + self.sgst_amount + self.igst_amount

    @property
    def itc_total(self):
        return self.itc_cgst + self.itc_sgst + self.itc_igst

    @property
    def payee(self) -> str:
        return (self.supplier.name if self.supplier else None) or self.payee_name or "-"

    def __repr__(self):
        return f"<Expense {self.id} {self.number or 'draft'} {self.amount}>"


class Frequency(str, enum.Enum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


FREQUENCY_LABELS = {
    Frequency.WEEKLY: "Every week",
    Frequency.MONTHLY: "Every month",
    Frequency.QUARTERLY: "Every 3 months",
    Frequency.YEARLY: "Every year",
}


class RecurringExpense(db.Model, TenantScopedMixin, TimestampMixin):
    """A template - rent, a salary - that makes a draft expense each
    period. A draft is checked (and its GST bill details added, if any)
    before it's posted."""

    __tablename__ = "recurring_expenses"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("expense_categories.id", name="fk_recurring_expenses_category"),
                            nullable=False)
    payee_name = db.Column(db.String(255))
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id", name="fk_recurring_expenses_supplier"))
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    payment_mode = db.Column(db.Enum(ExpensePaymentMode, name="expense_payment_mode"), nullable=False,
                             default=ExpensePaymentMode.BANK)
    branch_user_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_recurring_expenses_branch_user"))
    frequency = db.Column(db.Enum(Frequency, name="expense_frequency"), nullable=False, default=Frequency.MONTHLY)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)
    next_due_date = db.Column(db.Date, nullable=False)
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_recurring_expenses_created_by"), nullable=False)

    category = db.relationship("ExpenseCategory")
    supplier = db.relationship("Supplier")
    branch_user = db.relationship("User", foreign_keys=[branch_user_id])
    expenses = db.relationship("Expense", back_populates="recurring", order_by="Expense.due_date")

    def __repr__(self):
        return f"<RecurringExpense {self.id} {self.name}>"
