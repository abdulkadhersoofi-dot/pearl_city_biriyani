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
}


def get_or_create_series(
    tenant_id: int, gstin_id: int, document_type: DocumentType, financial_year: str | None = None
) -> InvoiceSeries:
    financial_year = financial_year or current_financial_year()
    series = InvoiceSeries.query.filter_by(
        tenant_id=tenant_id,
        gstin_id=gstin_id,
        document_type=document_type,
        financial_year=financial_year,
    ).first()
    if series is None:
        series = InvoiceSeries(
            tenant_id=tenant_id,
            gstin_id=gstin_id,
            document_type=document_type,
            financial_year=financial_year,
            prefix=DEFAULT_PREFIXES.get(document_type, "DOC"),
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
