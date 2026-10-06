import enum

from app.extensions import db
from app.models.invoice_series import DocumentType
from app.models.mixins import TenantScopedMixin, TimestampMixin


class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"
    ISSUED = "issued"
    VOID = "void"


class Invoice(db.Model, TenantScopedMixin, TimestampMixin):
    """An issued document (Tax Invoice, Bill of Supply, ...). Never
    hard-deleted - voiding sets status=void and keeps the full audit trail.
    """

    __tablename__ = "invoices"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "invoice_number", name="uq_invoice_tenant_number"),
    )

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id"), nullable=False, index=True)
    invoice_series_id = db.Column(
        db.Integer, db.ForeignKey("invoice_series.id"), nullable=False
    )
    invoice_number = db.Column(db.String(64), nullable=False, index=True)
    document_type = db.Column(db.Enum(DocumentType, name="document_type"), nullable=False)

    # Exactly one of customer_id/branch_user_id is set - an invoice bills
    # either an ordinary Customer or a branch (see app/invoicing/services.py
    # create_invoice, which enforces this and resolves the branch case
    # automatically to the right document type and GST treatment).
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    branch_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    # Frozen copy of the customer's (or branch's) name/GSTIN/address at
    # issue time, so a later edit to the customer master, or renaming a
    # branch, never rewrites history on a past invoice.
    customer_snapshot = db.Column(db.JSON, nullable=False)

    place_of_supply_state_code = db.Column(db.String(2), nullable=False)
    invoice_date = db.Column(db.Date, nullable=False)
    status = db.Column(
        db.Enum(InvoiceStatus, name="invoice_status"), nullable=False, default=InvoiceStatus.ISSUED
    )

    subtotal = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_discount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_cgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_sgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_igst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    # A Reverse Charge Invoice's tax is never charged by/payable to this
    # tenant - the recipient self-assesses and pays it directly to the
    # government - so it's never mixed into total_cgst/sgst/igst above
    # (which GSTR-3B sums as this tenant's own output tax payable). It's
    # disclosed here instead, exclusively for RCM_INVOICE documents - see
    # app.invoicing.services.create_invoice.
    total_rcgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_rsgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_rigst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    round_off = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    grand_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    notes = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    voided_at = db.Column(db.DateTime(timezone=True))
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    void_reason = db.Column(db.Text)

    # e-Invoice (IRN/QR) hook - an extension point, not a working NIC IRP
    # integration: this app has no e-invoice portal credentials to call
    # that government API with. Nothing in this codebase writes these
    # columns; they exist so a future integration has somewhere to put the
    # IRN/ack/QR it gets back, and the invoice view/PDF already know how
    # to show them once populated.
    irn = db.Column(db.String(64))
    irn_ack_number = db.Column(db.String(32))
    irn_ack_date = db.Column(db.DateTime(timezone=True))
    qr_code_data = db.Column(db.Text)

    gstin = db.relationship("Gstin")
    series = db.relationship("InvoiceSeries")
    customer = db.relationship("Customer")
    branch_user = db.relationship("User", foreign_keys=[branch_user_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    voided_by = db.relationship("User", foreign_keys=[voided_by_id])
    lines = db.relationship(
        "InvoiceLine",
        backref="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceLine.sort_order",
    )

    @property
    def is_intra_state(self) -> bool:
        return self.gstin is not None and self.place_of_supply_state_code == self.gstin.state_code

    def __repr__(self):
        return f"<Invoice {self.invoice_number}>"


class InvoiceLine(db.Model, TimestampMixin):
    __tablename__ = "invoice_lines"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False, index=True)
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
    # Reverse-charge counterpart of the three columns above - see
    # Invoice.total_rcgst.
    rcgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    rsgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    rigst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    sort_order = db.Column(db.Integer, nullable=False, default=0)

    product = db.relationship("Product")

    def __repr__(self):
        return f"<InvoiceLine {self.id} {self.description}>"
