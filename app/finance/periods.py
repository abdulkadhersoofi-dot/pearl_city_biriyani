"""Closed months: once a GST return is marked filed, the documents it was
filed from stay as they were.

    sales side      invoices, credit and debit notes       GSTR-1 filed
    buying side     purchase bills, purchase returns,      GSTR-3B filed
                    supplier credit / debit notes,
                    expenses

In a closed month (for that GSTIN) nothing can be added with a date in it,
and nothing dated in it can be voided - either would change figures that
have already gone to the government. A correction is a credit or debit note
dated in an open month, which is also how GST wants it reported.

The firm (Ultra Admin / Auditor) marks a return filed on the filing tracker,
and can unmark it to reopen the month; the audit log keeps both. Counter
(POS) bills are not checked here: they are always dated now, and can only be
voided the same day.

    ensure_open(tenant_id, gstin_id, on, SALES, SomeError, "add an invoice")
"""

from __future__ import annotations

from datetime import date

from app.models.filing import RETURN_LABELS, FilingStatus, ReturnType

SALES, BUYING = "sales", "buying"
_RETURN = {SALES: ReturnType.GSTR1, BUYING: ReturnType.GSTR3B}
_INSTEAD = {
    SALES: "Issue a credit or debit note dated in an open month instead",
    BUYING: "Enter it with a date in an open month, or record a supplier credit / debit note",
}


def period_of(on: date) -> str:
    return f"{on.year:04d}-{on.month:02d}"


def closed_by(tenant_id: int, gstin_id: int | None, on: date | None, side: str) -> FilingStatus | None:
    """The filed return that closes this date for this GSTIN, if any."""
    if not gstin_id or on is None:
        return None
    return FilingStatus.query.filter_by(tenant_id=tenant_id, gstin_id=gstin_id, period=period_of(on),
                                        return_type=_RETURN[side], filed=True).first()


def message(row: FilingStatus, on: date, doing: str, side: str) -> str:
    label = RETURN_LABELS[row.return_type]
    filed = f" on {row.filed_on:%d %b %Y}" if row.filed_on else ""
    return (f"{on:%B %Y} is closed: its {label} was marked filed{filed}, so you can't {doing} dated in it. "
            f"{_INSTEAD[side]} - or ask your accountant to reopen the month.")


def ensure_open(tenant_id: int, gstin_id: int | None, on: date | None, side: str, error, doing: str) -> None:
    """Raises error(message) when `on` falls in a month closed for this GSTIN."""
    row = closed_by(tenant_id, gstin_id, on, side)
    if row is not None:
        raise error(message(row, on, doing, side))
