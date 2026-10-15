"""Receivables and payables (Phase 6): money received from customers and
paid to suppliers, and which bills each payment settles.

  - CustomerReceipt: a receipt voucher (RCT/<FY>/0001) for money a
    customer paid - or, with is_advance, an advance receipt voucher
    (RV/<FY>/0001) for money received before the supply, with GST on it
    when it is for services. Allocations tie it to the invoices / credit
    POS bills / opening balance it settles; what isn't allocated stays
    "on account".
  - RefundVoucher (RFV/<FY>/0001): an advance given back.
  - SupplierPayment (PAY/<FY>/0001) and its allocations to purchase
    bills, unpaid expenses and the supplier's opening balance.

All are numbered from the row-locked allocator, never deleted, voided
with a reason and audit-logged. A cheque has its own status: a bounced
cheque settles nothing."""

import enum

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin


class MoneyMode(str, enum.Enum):
    CASH = "cash"
    UPI = "upi"
    CARD = "card"
    BANK = "bank"
    CHEQUE = "cheque"


MONEY_MODE_LABELS = {
    MoneyMode.CASH: "Cash",
    MoneyMode.UPI: "UPI",
    MoneyMode.CARD: "Card",
    MoneyMode.BANK: "Bank transfer",
    MoneyMode.CHEQUE: "Cheque",
}


class ChequeStatus(str, enum.Enum):
    PENDING = "pending"
    CLEARED = "cleared"
    BOUNCED = "bounced"


class VoucherStatus(str, enum.Enum):
    POSTED = "posted"
    VOIDED = "voided"


class _ChequeMixin:
    """Mode plus cheque details. A cheque counts only once it is not
    bounced; a pending cheque settles the bill but stays out of the bank
    book until it clears."""

    mode = db.Column(db.Enum(MoneyMode, name="money_mode"), nullable=False, default=MoneyMode.CASH)
    reference = db.Column(db.String(100))  # UTR / card slip / UPI ref
    cheque_no = db.Column(db.String(30))
    cheque_date = db.Column(db.Date)
    bank_name = db.Column(db.String(100))
    cheque_status = db.Column(db.Enum(ChequeStatus, name="cheque_status"))
    cleared_on = db.Column(db.Date)

    @property
    def settles(self) -> bool:
        return self.status == VoucherStatus.POSTED and self.cheque_status != ChequeStatus.BOUNCED

    @property
    def in_bank_on(self):
        """When the money is in the bank (None for cash, or a cheque not cleared)."""
        if self.mode == MoneyMode.CASH:
            return None
        if self.mode == MoneyMode.CHEQUE:
            return self.cleared_on if self.cheque_status == ChequeStatus.CLEARED else None
        return self.voucher_date


class CustomerReceipt(db.Model, TenantScopedMixin, TimestampMixin, _ChequeMixin):
    __tablename__ = "customer_receipts"
    __table_args__ = (db.UniqueConstraint("tenant_id", "number", name="uq_customer_receipts_tenant_number"),)

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_customer_receipts_gstin"), nullable=False)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_customer_receipts_series"), nullable=False)
    number = db.Column(db.String(64), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", name="fk_customer_receipts_customer"), nullable=False, index=True)
    voucher_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    notes = db.Column(db.Text)
    status = db.Column(db.Enum(VoucherStatus, name="voucher_status"), nullable=False, default=VoucherStatus.POSTED)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_customer_receipts_created_by"), nullable=False)
    voided_at = db.Column(db.DateTime(timezone=True))
    void_reason = db.Column(db.Text)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_customer_receipts_voided_by"))

    # Advance receipt voucher (s.31(3)(d), Rule 50): money before the supply.
    is_advance = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    # GST is due on an advance for services only (goods: Notification 66/2017).
    for_services = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    place_of_supply_state_code = db.Column(db.String(2))
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0, server_default="0")
    taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    cgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    sgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    igst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")

    customer = db.relationship("Customer")
    gstin = db.relationship("Gstin")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    allocations = db.relationship("ReceiptAllocation", backref="receipt", cascade="all, delete-orphan",
                                  order_by="ReceiptAllocation.id")
    refunds = db.relationship("RefundVoucher", backref="receipt", order_by="RefundVoucher.id")

    @property
    def total_tax(self):
        return self.cgst_amount + self.sgst_amount + self.igst_amount

    def __repr__(self):
        return f"<CustomerReceipt {self.number} {self.amount}>"


class ReceiptAllocation(db.Model, TimestampMixin):
    """Part of a receipt settling one document (or the opening balance)."""

    __tablename__ = "receipt_allocations"

    id = db.Column(db.Integer, primary_key=True)
    receipt_id = db.Column(db.Integer, db.ForeignKey("customer_receipts.id", name="fk_receipt_allocations_receipt"),
                           nullable=False, index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id", name="fk_receipt_allocations_invoice"), index=True)
    pos_bill_id = db.Column(db.Integer, db.ForeignKey("pos_bills.id", name="fk_receipt_allocations_pos_bill"), index=True)
    is_opening = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    # When it was set against the document - for an advance, the month its
    # GST is adjusted in GSTR-1 (table 11B).
    allocated_on = db.Column(db.Date, nullable=False)

    invoice = db.relationship("Invoice")
    pos_bill = db.relationship("POSBill")


class RefundVoucher(db.Model, TenantScopedMixin, TimestampMixin):
    """An advance given back because the supply didn't happen (Rule 51)."""

    __tablename__ = "refund_vouchers"
    __table_args__ = (db.UniqueConstraint("tenant_id", "number", name="uq_refund_vouchers_tenant_number"),)

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_refund_vouchers_gstin"), nullable=False)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_refund_vouchers_series"), nullable=False)
    number = db.Column(db.String(64), nullable=False)
    receipt_id = db.Column(db.Integer, db.ForeignKey("customer_receipts.id", name="fk_refund_vouchers_receipt"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", name="fk_refund_vouchers_customer"), nullable=False)
    voucher_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    mode = db.Column(db.Enum(MoneyMode, name="money_mode"), nullable=False, default=MoneyMode.CASH)
    reference = db.Column(db.String(100))
    taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    cgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    sgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    igst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.Enum(VoucherStatus, name="voucher_status"), nullable=False, default=VoucherStatus.POSTED)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_refund_vouchers_created_by"), nullable=False)
    voided_at = db.Column(db.DateTime(timezone=True))
    void_reason = db.Column(db.Text)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_refund_vouchers_voided_by"))

    customer = db.relationship("Customer")
    gstin = db.relationship("Gstin")

    @property
    def total_tax(self):
        return self.cgst_amount + self.sgst_amount + self.igst_amount


class SupplierPayment(db.Model, TenantScopedMixin, TimestampMixin, _ChequeMixin):
    __tablename__ = "supplier_payments"
    __table_args__ = (db.UniqueConstraint("tenant_id", "number", name="uq_supplier_payments_tenant_number"),)

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_supplier_payments_gstin"), nullable=False)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_supplier_payments_series"), nullable=False)
    number = db.Column(db.String(64), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id", name="fk_supplier_payments_supplier"), nullable=False, index=True)
    voucher_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    notes = db.Column(db.Text)
    status = db.Column(db.Enum(VoucherStatus, name="voucher_status"), nullable=False, default=VoucherStatus.POSTED)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_supplier_payments_created_by"), nullable=False)
    voided_at = db.Column(db.DateTime(timezone=True))
    void_reason = db.Column(db.Text)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_supplier_payments_voided_by"))

    supplier = db.relationship("Supplier")
    gstin = db.relationship("Gstin")
    allocations = db.relationship("PaymentAllocation", backref="payment", cascade="all, delete-orphan",
                                  order_by="PaymentAllocation.id")


class PaymentAllocation(db.Model, TimestampMixin):
    __tablename__ = "payment_allocations"

    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("supplier_payments.id", name="fk_payment_allocations_payment"),
                           nullable=False, index=True)
    purchase_bill_id = db.Column(db.Integer, db.ForeignKey("purchase_bills.id", name="fk_payment_allocations_bill"), index=True)
    expense_id = db.Column(db.Integer, db.ForeignKey("expenses.id", name="fk_payment_allocations_expense"), index=True)
    is_opening = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    amount = db.Column(db.Numeric(12, 2), nullable=False)

    purchase_bill = db.relationship("PurchaseBill")
    expense = db.relationship("Expense")
