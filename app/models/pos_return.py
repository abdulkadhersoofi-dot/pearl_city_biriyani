import enum

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin
from app.models.pos_bill import PaymentMode


class POSReturnStatus(str, enum.Enum):
    COMPLETED = "completed"
    VOIDED = "voided"


class POSReturn(db.Model, TenantScopedMixin, TimestampMixin):
    """Goods brought back against a POS bill - which lines, how many.

    Its own numbered document (series POS_RETURN, "SR/2026-27/0001"):
    it reduces sales and output tax in the period it happens (a B2B
    return is reported like a credit note), puts the goods back into the
    branch's stock count, and is paid out in cash/UPI/... or kept as
    exchange credit towards a new bill. The original bill is never
    altered. Never deleted - voided with a reason instead.
    """

    __tablename__ = "pos_returns"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "return_number", name="uq_pos_returns_tenant_number"),
    )

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_pos_returns_gstin"), nullable=False, index=True)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_pos_returns_series"), nullable=False)
    return_number = db.Column(db.String(64), nullable=False)   # looked up within a business: uq_*_tenant_number covers it
    bill_id = db.Column(db.Integer, db.ForeignKey("pos_bills.id", name="fk_pos_returns_bill"), nullable=False, index=True)
    customer_snapshot = db.Column(db.JSON)

    status = db.Column(
        db.Enum(POSReturnStatus, name="pos_return_status"), nullable=False, default=POSReturnStatus.COMPLETED
    )
    reason = db.Column(db.Text, nullable=False)
    # How the value went back to the customer. EXCHANGE = not paid out,
    # kept as credit for a new bill (POSPayment.pos_return_id).
    refund_mode = db.Column(db.Enum(PaymentMode, name="payment_mode"), nullable=False)
    exchange_used = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")

    subtotal = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_discount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_cgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_sgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_igst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    round_off = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    grand_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    cash_session_id = db.Column(db.Integer, db.ForeignKey("cash_sessions.id", name="fk_pos_returns_cash_session"), index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_pos_returns_created_by"), nullable=False)
    returned_at = db.Column(db.DateTime(timezone=True), nullable=False)

    voided_at = db.Column(db.DateTime(timezone=True))
    void_reason = db.Column(db.Text)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_pos_returns_voided_by"))

    gstin = db.relationship("Gstin")
    bill = db.relationship("POSBill", backref=db.backref("returns", order_by="POSReturn.id"))
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    voided_by = db.relationship("User", foreign_keys=[voided_by_id])
    lines = db.relationship(
        "POSReturnLine", backref="pos_return", cascade="all, delete-orphan", order_by="POSReturnLine.sort_order"
    )

    @property
    def exchange_balance(self):
        if self.refund_mode != PaymentMode.EXCHANGE or self.status != POSReturnStatus.COMPLETED:
            return 0
        return self.grand_total - (self.exchange_used or 0)

    def __repr__(self):
        return f"<POSReturn {self.return_number}>"


class POSReturnLine(db.Model, TimestampMixin):
    __tablename__ = "pos_return_lines"

    id = db.Column(db.Integer, primary_key=True)
    return_id = db.Column(
        db.Integer, db.ForeignKey("pos_returns.id", name="fk_pos_return_lines_return"), nullable=False, index=True
    )
    bill_line_id = db.Column(
        db.Integer, db.ForeignKey("pos_bill_lines.id", name="fk_pos_return_lines_bill_line"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_pos_return_lines_product"))

    description = db.Column(db.String(255), nullable=False)
    hsn_or_sac_code = db.Column(db.String(8))
    qty = db.Column(db.Numeric(12, 3), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default="PCS")
    rate = db.Column(db.Numeric(12, 2), nullable=False)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    price_includes_tax = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    discount_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    cgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    sgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    igst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    bill_line = db.relationship("POSBillLine")
    product = db.relationship("Product")
