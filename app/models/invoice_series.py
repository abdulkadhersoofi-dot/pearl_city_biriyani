import enum

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin


class DocumentType(str, enum.Enum):
    TAX_INVOICE = "tax_invoice"
    BILL_OF_SUPPLY = "bill_of_supply"
    EXPORT_INVOICE_IGST = "export_invoice_igst"
    EXPORT_INVOICE_LUT = "export_invoice_lut"
    RCM_INVOICE = "rcm_invoice"
    DELIVERY_CHALLAN = "delivery_challan"
    RECEIPT_VOUCHER = "receipt_voucher"
    REFUND_VOUCHER = "refund_voucher"
    CREDIT_NOTE = "credit_note"
    DEBIT_NOTE = "debit_note"
    POS_BILL = "pos_bill"
    # Goods returned against a POS bill (app.models.pos_return).
    POS_RETURN = "pos_return"
    # Buying side and stock documents (Phase 3).
    PURCHASE_BILL = "purchase_bill"
    PURCHASE_RETURN = "purchase_return"
    PURCHASE_ORDER = "purchase_order"
    STOCK_TRANSFER = "stock_transfer"
    STOCK_ADJUSTMENT = "stock_adjustment"
    EXPENSE = "expense"
    # Second-hand goods under Rule 32(5) - its own series (Phase 5).
    MARGIN_SCHEME_INVOICE = "margin_scheme_invoice"
    # Accounts and pre-sale documents (Phase 6). RECEIPT_VOUCHER above is
    # the Rule 50 voucher for an advance; PAYMENT_RECEIPT is an ordinary
    # receipt for money received against bills.
    PAYMENT_RECEIPT = "payment_receipt"
    SUPPLIER_PAYMENT = "supplier_payment"
    QUOTATION = "quotation"
    PROFORMA_INVOICE = "proforma_invoice"
    SALES_ORDER = "sales_order"
    # Business-type modules (Phase 9): repair job cards and the voucher for
    # old gold bought from a customer.
    JOB_CARD = "job_card"
    OLD_GOLD_PURCHASE = "old_gold_purchase"


# Document types a tenant may issue, gated by their GST registration type.
# Populated in app.utils.gst; kept here so both series allocation and the
# invoicing UI agree on what is allowed.
DOCUMENT_TYPE_LABELS = {
    DocumentType.TAX_INVOICE: "Tax Invoice",
    DocumentType.BILL_OF_SUPPLY: "Bill of Supply",
    DocumentType.EXPORT_INVOICE_IGST: "Export Invoice (with IGST)",
    DocumentType.EXPORT_INVOICE_LUT: "Export Invoice (under LUT)",
    DocumentType.RCM_INVOICE: "Reverse Charge Invoice",
    DocumentType.DELIVERY_CHALLAN: "Delivery Challan",
    DocumentType.RECEIPT_VOUCHER: "Receipt Voucher",
    DocumentType.REFUND_VOUCHER: "Refund Voucher",
    DocumentType.CREDIT_NOTE: "Credit Note",
    DocumentType.DEBIT_NOTE: "Debit Note",
    DocumentType.POS_BILL: "POS Bill",
    DocumentType.POS_RETURN: "POS Sales Return",
    DocumentType.PURCHASE_BILL: "Purchase Bill",
    DocumentType.PURCHASE_RETURN: "Debit Note (purchase return)",
    DocumentType.PURCHASE_ORDER: "Purchase Order",
    DocumentType.STOCK_TRANSFER: "Delivery Challan (stock transfer)",
    DocumentType.STOCK_ADJUSTMENT: "Stock Adjustment",
    DocumentType.EXPENSE: "Expense Voucher",
    DocumentType.MARGIN_SCHEME_INVOICE: "Invoice - Margin Scheme",
    DocumentType.PAYMENT_RECEIPT: "Receipt",
    DocumentType.SUPPLIER_PAYMENT: "Payment Voucher",
    DocumentType.QUOTATION: "Quotation",
    DocumentType.PROFORMA_INVOICE: "Proforma Invoice",
    DocumentType.SALES_ORDER: "Sales Order",
    DocumentType.JOB_CARD: "Job Card",
    DocumentType.OLD_GOLD_PURCHASE: "Old Gold Purchase Voucher",
}


class InvoiceSeries(db.Model, TenantScopedMixin, TimestampMixin):
    """A per-financial-year, per-GSTIN, per-document-type numbering counter.

    Credit/debit notes and every other document type each get their own
    series so numbering never collides across document types.
    """

    __tablename__ = "invoice_series"
    __table_args__ = (
        db.UniqueConstraint(
            "tenant_id", "gstin_id", "financial_year", "document_type", "prefix",
            name="uq_series_tenant_gstin_fy_doctype_prefix",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id"), nullable=False, index=True)
    financial_year = db.Column(db.String(7), nullable=False)  # e.g. "2025-26"
    document_type = db.Column(db.Enum(DocumentType, name="document_type"), nullable=False)
    prefix = db.Column(db.String(20), nullable=False, default="INV")
    next_number = db.Column(db.Integer, nullable=False, default=1)

    gstin = db.relationship("Gstin")

    def __repr__(self):
        return f"<InvoiceSeries {self.prefix}/{self.financial_year} {self.document_type}>"
