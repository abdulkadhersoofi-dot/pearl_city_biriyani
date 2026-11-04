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
    DocumentType.MARGIN_PURCHASE: "SHP",
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
    # A counter device's own offline series (device_id set) is never a
    # normal series - see device_series below.
    query = InvoiceSeries.query.filter_by(
        tenant_id=tenant_id,
        gstin_id=gstin_id,
        document_type=document_type,
        financial_year=financial_year,
        device_id=None,
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


def _lock(series):
    # Any series table with prefix / financial_year / next_number - a
    # client's InvoiceSeries or the firm's own PlatformSeries.
    return type(series).query.filter_by(id=series.id).with_for_update().one()


def format_number(prefix: str, financial_year: str, number: int) -> str:
    return f"{prefix}/{financial_year}/{number:04d}"


def allocate_number(series) -> str:
    """Locks the series row, reserves the next number, and returns the
    formatted invoice number. Caller must be inside a transaction; the
    row lock (SELECT ... FOR UPDATE) prevents two concurrent checkouts
    from getting the same number.
    """
    locked = _lock(series)
    number = locked.next_number
    locked.next_number = number + 1
    db.session.add(locked)
    return format_number(locked.prefix, locked.financial_year, number)


def device_series(tenant_id: int, gstin_id: int, device, document_type: DocumentType,
                  financial_year: str) -> InvoiceSeries:
    """A counter device's own series (feature "offline_pos"): prefix = the
    device's code (O1, O2...), so its numbers never collide with the
    counter's normal series or another device's."""
    series = InvoiceSeries.query.filter_by(tenant_id=tenant_id, gstin_id=gstin_id, document_type=document_type,
                                           financial_year=financial_year, device_id=device.id).first()
    if series is None:
        series = InvoiceSeries(tenant_id=tenant_id, gstin_id=gstin_id, document_type=document_type,
                               financial_year=financial_year, prefix=device.code, next_number=1,
                               device_id=device.id)
        db.session.add(series)
        db.session.flush()
    return series


def claim_number(series: InvoiceSeries, number: int) -> str:
    """A number the device already gave out offline (it numbers its own
    series itself). Under the same row lock as allocate_number; the series
    moves past it, so the device's next number is never handed out twice."""
    locked = _lock(series)
    if number < 1:
        raise ValueError("A bill number starts at 1.")
    locked.next_number = max(locked.next_number, number + 1)
    db.session.add(locked)
    return format_number(locked.prefix, locked.financial_year, number)


# --- seeing and setting a series (the invoice form's number field) ------------------------------

import re  # noqa: E402

# Rule 46(b) CGST Rules: a consecutive serial number of at most 16
# characters - letters, digits, hyphen "-" and slash "/" - unique for the
# financial year.
GST_NUMBER = re.compile(r"^[A-Za-z0-9/-]{1,16}$")
PREFIX = re.compile(r"^[A-Za-z0-9-]{1,10}$")
MAX_LENGTH = 16


class NumberingError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _find_series(tenant_id: int, gstin_id: int, document_type: DocumentType, financial_year: str):
    special = _einvoice_prefix(tenant_id, document_type)
    query = InvoiceSeries.query.filter_by(tenant_id=tenant_id, gstin_id=gstin_id, document_type=document_type,
                                          financial_year=financial_year, device_id=None)
    return (query.filter_by(prefix=special) if special else query).first(), special


def peek(tenant_id: int, gstin_id: int, document_type: DocumentType, financial_year: str) -> dict:
    """The number the next document of this series will get - without
    taking it or making the series (a page showing it changes nothing)."""
    series, special = _find_series(tenant_id, gstin_id, document_type, financial_year)
    prefix = series.prefix if series else (special or DEFAULT_PREFIXES.get(document_type, "DOC"))
    next_number = series.next_number if series else 1
    return {"number": format_number(prefix, financial_year, next_number), "prefix": prefix,
            "next": next_number, "fy": financial_year, "prefix_locked": bool(special)}


def number_in(series, number: str) -> int | None:
    """The serial when `number` is written in this series' own format
    (INV/2026-27/0042 -> 42), else None."""
    head = f"{series.prefix}/{series.financial_year}/"
    tail = number[len(head):] if number.startswith(head) else ""
    return int(tail) if tail.isdigit() and len(tail) >= 4 and int(tail) > 0 else None


def check_typed_number(number: str) -> str:
    number = (number or "").strip()
    if not GST_NUMBER.match(number):
        raise NumberingError("An invoice number is at most 16 characters: letters, digits, \"-\" and \"/\" "
                             "(GST rule 46).")
    return number


def used_serials(model, tenant_id: int, column, prefix: str, financial_year: str) -> list[int]:
    head = f"{prefix}/{financial_year}/"
    rows = (db.session.query(column).filter(model.tenant_id == tenant_id, column.like(head.replace("%", r"\%") + "%"))
            .all())
    serials = []
    for (value,) in rows:
        tail = value[len(head):]
        if tail.isdigit():
            serials.append(int(tail))
    return serials


def set_series(series, prefix: str, next_number: int, used: list[int]) -> None:
    """The owner's numbering: a new prefix and/or where the series carries
    on from. Never so low that a number already given out comes round
    again, and never longer than GST's 16 characters."""
    prefix = (prefix or "").strip()
    if not PREFIX.match(prefix):
        raise NumberingError("The prefix is letters, digits and \"-\", up to 10 characters (no \"/\").")
    if not isinstance(next_number, int) or next_number < 1 or next_number > 99999999:
        raise NumberingError("The next number is a whole number from 1.")
    if len(format_number(prefix, series.financial_year, next_number)) > MAX_LENGTH:
        raise NumberingError(f"{format_number(prefix, series.financial_year, next_number)} is longer than GST's "
                             f"16 characters - use a shorter prefix.")
    if used and next_number <= max(used):
        raise NumberingError(f"{format_number(prefix, series.financial_year, max(used))} is already given out - "
                             f"the next number has to be at least {max(used) + 1}.")
    locked = _lock(series)
    locked.prefix, locked.next_number = prefix, next_number
    db.session.add(locked)
