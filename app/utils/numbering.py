from app.extensions import db
from app.models.invoice_series import DocumentType, InvoiceSeries
from app.utils.financial_year import current_financial_year

DEFAULT_PREFIXES = {
    DocumentType.TAX_INVOICE: "INV",
    DocumentType.BILL_OF_SUPPLY: "BOS",
    DocumentType.EXPORT_INVOICE_IGST: "EXP",
    DocumentType.EXPORT_INVOICE_LUT: "EXP-LUT",
    DocumentType.RCM_INVOICE: "RCM",
    DocumentType.DELIVERY_CHALLAN: "DC",
    DocumentType.RECEIPT_VOUCHER: "RV",
    DocumentType.REFUND_VOUCHER: "RFV",
    DocumentType.CREDIT_NOTE: "CN",
    DocumentType.DEBIT_NOTE: "DN",
    DocumentType.POS_BILL: "POS",
    DocumentType.POS_RETURN: "SR",
    DocumentType.PURCHASE_BILL: "PB",
    DocumentType.PURCHASE_RETURN: "PDN",
    DocumentType.PURCHASE_ORDER: "PO",
    DocumentType.STOCK_TRANSFER: "TDC",
    DocumentType.STOCK_ADJUSTMENT: "ADJ",
    # "EXP" is the export invoice's prefix; expense vouchers moved to "EV"
    # (Phase 6 migration renames existing expense series).
    DocumentType.EXPENSE: "EV",
    DocumentType.MARGIN_SCHEME_INVOICE: "MSI",
    DocumentType.PAYMENT_RECEIPT: "RCT",
    DocumentType.SUPPLIER_PAYMENT: "PAY",
    DocumentType.QUOTATION: "QTN",
    DocumentType.PROFORMA_INVOICE: "PI",
    DocumentType.SALES_ORDER: "SO",
    DocumentType.JOB_CARD: "JOB",
    DocumentType.OLD_GOLD_PURCHASE: "OG",
}

# The IRP accepts a document number of at most 16 characters, and
# "EXP-LUT/2026-27/0001" is 20 - so a client with e-invoicing on numbers
# LUT exports in an "EXL" series instead (a new series from the next
# LUT export; numbers already issued are untouched).
EINVOICE_PREFIXES = {DocumentType.EXPORT_INVOICE_LUT: "EXL"}


def _einvoice_prefix(tenant_id: int, document_type: DocumentType) -> str | None:
    if document_type not in EINVOICE_PREFIXES:
        return None
    from app.models.tenant import Tenant

    tenant = db.session.get(Tenant, tenant_id)
    return EINVOICE_PREFIXES[document_type] if tenant is not None and tenant.has_feature("einvoice") else None


def get_or_create_series(
    tenant_id: int, gstin_id: int, document_type: DocumentType, financial_year: str | None = None
) -> InvoiceSeries:
    financial_year = financial_year or current_financial_year()
    special = _einvoice_prefix(tenant_id, document_type)
    query = InvoiceSeries.query.filter_by(
        tenant_id=tenant_id,
        gstin_id=gstin_id,
        document_type=document_type,
        financial_year=financial_year,
    )
    series = (query.filter_by(prefix=special) if special else query).first()
    if series is None:
        series = InvoiceSeries(
            tenant_id=tenant_id,
            gstin_id=gstin_id,
            document_type=document_type,
            financial_year=financial_year,
            prefix=special or DEFAULT_PREFIXES.get(document_type, "DOC"),
            next_number=1,
        )
        db.session.add(series)
        db.session.flush()
    return series


def allocate_number(series: InvoiceSeries) -> str:
    """Locks the series row, reserves the next number, and returns the
    formatted invoice number. Caller must be inside a transaction; the
    row lock (SELECT ... FOR UPDATE) prevents two concurrent checkouts
    from getting the same number.
    """
    locked = (
        InvoiceSeries.query.filter_by(id=series.id)
        .with_for_update()
        .one()
    )
    number = locked.next_number
    locked.next_number = number + 1
    db.session.add(locked)
    return f"{locked.prefix}/{locked.financial_year}/{number:04d}"
