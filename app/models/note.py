import enum

from app.extensions import db
from app.models.invoice_series import DocumentType
from app.models.mixins import TenantScopedMixin, TimestampMixin


class NoteStatus(str, enum.Enum):
    ISSUED = "issued"
    VOID = "void"


class CreditDebitNote(db.Model, TenantScopedMixin, TimestampMixin):
    """A Credit Note or Debit Note issued against an existing invoice - a
    sales return, a post-sale discount, a pricing correction, and so on.
    Never hard-deleted, same as Invoice: voiding sets status=void and
    keeps the record for audit.
    """

    __tablename__ = "credit_debit_notes"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "note_number", name="uq_note_tenant_number"),
    )

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id"), nullable=False, index=True)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id"), nullable=False)
    note_number = db.Column(db.String(64), nullable=False, index=True)
    document_type = db.Column(db.Enum(DocumentType, name="document_type"), nullable=False)

    original_invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False, index=True)

    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    # Frozen at issue time, same reasoning as Invoice.customer_snapshot - a
    # later edit to the customer master must never rewrite a past note.
    customer_snapshot = db.Column(db.JSON, nullable=False)

    place_of_supply_state_code = db.Column(db.String(2), nullable=False)
    note_date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(
        db.Enum(NoteStatus, name="note_status"), nullable=False, default=NoteStatus.ISSUED
    )

    subtotal = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_discount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_cgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_sgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_igst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    round_off = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    grand_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    voided_at = db.Column(db.DateTime(timezone=True))
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    void_reason = db.Column(db.Text)

    gstin = db.relationship("Gstin")
    series = db.relationship("InvoiceSeries")
    customer = db.relationship("Customer")
    original_invoice = db.relationship("Invoice", backref="credit_debit_notes")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    voided_by = db.relationship("User", foreign_keys=[voided_by_id])
    lines = db.relationship(
        "NoteLine",
        backref="note",
        cascade="all, delete-orphan",
        order_by="NoteLine.sort_order",
    )

    @property
    def is_intra_state(self) -> bool:
        return self.gstin is not None and self.place_of_supply_state_code == self.gstin.state_code

    def __repr__(self):
        return f"<CreditDebitNote {self.note_number}>"


class NoteLine(db.Model, TimestampMixin):
    __tablename__ = "note_lines"

    id = db.Column(db.Integer, primary_key=True)
    note_id = db.Column(db.Integer, db.ForeignKey("credit_debit_notes.id"), nullable=False, index=True)
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
        return f"<NoteLine {self.id} {self.description}>"
