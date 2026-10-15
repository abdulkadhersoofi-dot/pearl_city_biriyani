"""Sales documents that come before (or instead of) an invoice, and
invoices that repeat (Phase 6).

  - SalesDocument: a Quotation / Estimate (QTN), Proforma Invoice (PI),
    Sales Order (SO) or Delivery Challan (DC, Rule 55 - job work, sale on
    approval, goods sent elsewhere without a sale). None of them is a tax
    document: they don't count in GSTR-1's sales or move stock. Each turns
    into a real invoice in one click (converted_invoice_id); a challan can
    instead be marked returned (job work back, approval goods refused).
    Numbered from the row-locked allocator, never deleted - cancelled with
    a reason. Delivery challans share the DC series with challans already
    issued to owned branches, so a challan number is never used twice.
  - RecurringInvoice: a template (customer, lines, frequency) for monthly
    AMC / rent / catering contracts. Each due date becomes a
    RecurringInvoiceRun - a draft with no number until it is issued, so a
    skipped month leaves no gap in the invoice series.
"""

import enum

from app.extensions import db
from app.models.expense import Frequency
from app.models.invoice_series import DocumentType
from app.models.mixins import TenantScopedMixin, TimestampMixin


class SalesDocumentKind(str, enum.Enum):
    QUOTATION = "quotation"
    PROFORMA = "proforma"
    SALES_ORDER = "sales_order"
    DELIVERY_CHALLAN = "delivery_challan"


SALES_DOCUMENT_LABELS = {
    SalesDocumentKind.QUOTATION: "Quotation",
    SalesDocumentKind.PROFORMA: "Proforma Invoice",
    SalesDocumentKind.SALES_ORDER: "Sales Order",
    SalesDocumentKind.DELIVERY_CHALLAN: "Delivery Challan",
}

SALES_DOCUMENT_SERIES = {
    SalesDocumentKind.QUOTATION: DocumentType.QUOTATION,
    SalesDocumentKind.PROFORMA: DocumentType.PROFORMA_INVOICE,
    SalesDocumentKind.SALES_ORDER: DocumentType.SALES_ORDER,
    SalesDocumentKind.DELIVERY_CHALLAN: DocumentType.DELIVERY_CHALLAN,
}


class SalesDocumentStatus(str, enum.Enum):
    OPEN = "open"
    CONVERTED = "converted"  # invoiced
    RETURNED = "returned"  # challan goods came back
    CANCELLED = "cancelled"


class ChallanPurpose(str, enum.Enum):
    JOB_WORK = "job_work"
    SALE_ON_APPROVAL = "sale_on_approval"
    STOCK_TRANSFER = "stock_transfer"
    OTHER = "other"


CHALLAN_PURPOSE_LABELS = {
    ChallanPurpose.JOB_WORK: "Job work",
    ChallanPurpose.SALE_ON_APPROVAL: "Sale on approval",
    ChallanPurpose.STOCK_TRANSFER: "Stock transfer",
    ChallanPurpose.OTHER: "Other (exhibition, supply of unknown quantity...)",
}


class SalesDocument(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "sales_documents"
    __table_args__ = (db.UniqueConstraint("tenant_id", "number", name="uq_sales_documents_tenant_number"),)

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.Enum(SalesDocumentKind, name="sales_document_kind"), nullable=False, index=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_sales_documents_gstin"), nullable=False)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_sales_documents_series"), nullable=False)
    number = db.Column(db.String(64), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", name="fk_sales_documents_customer"), nullable=False, index=True)
    customer_snapshot = db.Column(db.JSON, nullable=False)
    place_of_supply_state_code = db.Column(db.String(2), nullable=False)
    doc_date = db.Column(db.Date, nullable=False)
    valid_until = db.Column(db.Date)
    # What it becomes when converted (Tax Invoice, Bill of Supply, export...).
    invoice_document_type = db.Column(db.Enum(DocumentType, name="document_type"), nullable=False)
    challan_purpose = db.Column(db.Enum(ChallanPurpose, name="challan_purpose"))
    status = db.Column(db.Enum(SalesDocumentStatus, name="sales_document_status"), nullable=False,
                       default=SalesDocumentStatus.OPEN)

    subtotal = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_discount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_cgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_sgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_igst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    round_off = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    grand_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    notes = db.Column(db.Text)
    terms = db.Column(db.Text)
    converted_invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id", name="fk_sales_documents_invoice"), index=True)
    closed_at = db.Column(db.DateTime(timezone=True))  # converted / returned / cancelled
    cancel_reason = db.Column(db.Text)
    closed_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_sales_documents_closed_by"))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_sales_documents_created_by"), nullable=False)

    gstin = db.relationship("Gstin")
    customer = db.relationship("Customer")
    converted_invoice = db.relationship("Invoice")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    lines = db.relationship("SalesDocumentLine", backref="document", cascade="all, delete-orphan",
                            order_by="SalesDocumentLine.sort_order")

    @property
    def label(self) -> str:
        return SALES_DOCUMENT_LABELS[self.kind]

    @property
    def total_tax(self):
        return self.total_cgst + self.total_sgst + self.total_igst

    @property
    def is_intra_state(self) -> bool:
        return self.gstin is not None and self.place_of_supply_state_code == self.gstin.state_code

    def __repr__(self):
        return f"<SalesDocument {self.number}>"


class SalesDocumentLine(db.Model, TimestampMixin):
    __tablename__ = "sales_document_lines"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("sales_documents.id", name="fk_sales_document_lines_document"),
                            nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_sales_document_lines_product"))
    description = db.Column(db.String(255), nullable=False)
    hsn_or_sac_code = db.Column(db.String(8))
    qty = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit = db.Column(db.String(20), nullable=False, default="pcs")
    rate = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    discount_percent = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    discount_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    price_includes_tax = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    cgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    sgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    igst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    product = db.relationship("Product")


class RecurringInvoice(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "recurring_invoices"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_recurring_invoices_gstin"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", name="fk_recurring_invoices_customer"), nullable=False, index=True)
    document_type = db.Column(db.Enum(DocumentType, name="document_type"), nullable=False)
    place_of_supply_state_code = db.Column(db.String(2), nullable=False)
    frequency = db.Column(db.Enum(Frequency, name="expense_frequency"), nullable=False, default=Frequency.MONTHLY)
    anchor_day = db.Column(db.Integer, nullable=False, default=1)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)
    next_due_date = db.Column(db.Date, nullable=False)
    # [{description, hsn_or_sac_code, qty, unit, rate, discount_percent,
    #   gst_rate, product_id, price_includes_tax}] - money as strings.
    lines = db.Column(db.JSON, nullable=False, default=list)
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_recurring_invoices_created_by"), nullable=False)

    gstin = db.relationship("Gstin")
    customer = db.relationship("Customer")
    runs = db.relationship("RecurringInvoiceRun", backref="recurring", order_by="RecurringInvoiceRun.due_date.desc()",
                           lazy="dynamic")


class RecurringRunStatus(str, enum.Enum):
    PENDING = "pending"
    ISSUED = "issued"
    SKIPPED = "skipped"


class RecurringInvoiceRun(db.Model, TenantScopedMixin, TimestampMixin):
    """One due date of a recurring invoice: a draft until issued (or
    skipped). The unique (recurring, due date) makes generation safe to run
    any number of times."""

    __tablename__ = "recurring_invoice_runs"
    __table_args__ = (db.UniqueConstraint("recurring_id", "due_date", name="uq_recurring_invoice_runs_due"),)

    id = db.Column(db.Integer, primary_key=True)
    recurring_id = db.Column(db.Integer, db.ForeignKey("recurring_invoices.id", name="fk_recurring_invoice_runs_recurring"),
                             nullable=False, index=True)
    due_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.Enum(RecurringRunStatus, name="recurring_run_status"), nullable=False,
                       default=RecurringRunStatus.PENDING)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id", name="fk_recurring_invoice_runs_invoice"))
    skip_reason = db.Column(db.Text)
    closed_at = db.Column(db.DateTime(timezone=True))
    closed_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_recurring_invoice_runs_closed_by"))

    invoice = db.relationship("Invoice")
