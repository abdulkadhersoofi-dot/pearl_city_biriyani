import enum

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin


class POSBillStatus(str, enum.Enum):
    HELD = "held"
    COMPLETED = "completed"
    REFUNDED = "refunded"


class PaymentMode(str, enum.Enum):
    CASH = "cash"
    UPI = "upi"
    CARD = "card"


class POSBill(db.Model, TenantScopedMixin, TimestampMixin):
    """A POS cart. A HELD bill is just a parked draft - no bill number is
    allocated for it and it is freely resumed or discarded. A number is
    only allocated at checkout (COMPLETED), because that's the point a
    fiscal receipt actually exists and must never be reused or skipped.
    """

    __tablename__ = "pos_bills"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "bill_number", name="uq_pos_bill_tenant_number"),
    )

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id"), nullable=False, index=True)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id"))
    bill_number = db.Column(db.String(64), index=True)

    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    customer_snapshot = db.Column(db.JSON)

    status = db.Column(db.Enum(POSBillStatus, name="pos_bill_status"), nullable=False, default=POSBillStatus.HELD)
    payment_mode = db.Column(db.Enum(PaymentMode, name="payment_mode"))
    hold_label = db.Column(db.String(100))

    subtotal = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_discount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_cgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_sgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_igst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    round_off = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    grand_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    completed_at = db.Column(db.DateTime(timezone=True))

    refunded_amount = db.Column(db.Numeric(12, 2))
    refund_reason = db.Column(db.Text)
    refunded_at = db.Column(db.DateTime(timezone=True))
    refunded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    gstin = db.relationship("Gstin")
    customer = db.relationship("Customer")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    refunded_by = db.relationship("User", foreign_keys=[refunded_by_id])
    lines = db.relationship(
        "POSBillLine",
        backref="bill",
        cascade="all, delete-orphan",
        order_by="POSBillLine.sort_order",
    )

    def __repr__(self):
        return f"<POSBill {self.bill_number or f'held-{self.id}'}>"


class POSBillLine(db.Model, TimestampMixin):
    __tablename__ = "pos_bill_lines"

    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("pos_bills.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"))

    description = db.Column(db.String(255), nullable=False)
    hsn_or_sac_code = db.Column(db.String(8))
    qty = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit = db.Column(db.String(20), nullable=False, default="pcs")
    rate = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    discount_percent = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    discount_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    cgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    sgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    igst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    sort_order = db.Column(db.Integer, nullable=False, default=0)

    product = db.relationship("Product")

    def __repr__(self):
        return f"<POSBillLine {self.id} {self.description}>"
