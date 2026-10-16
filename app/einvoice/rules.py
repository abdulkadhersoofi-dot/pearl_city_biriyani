"""When e-invoicing / e-way bills apply - every threshold from
app.utils.gst_config, nothing hardcoded here.

e-Invoice (IRN): the client has the einvoice module on, is a regular GST
registration with a GSTIN, and its AATO is above EINVOICE_AATO_THRESHOLD.
Then these documents need an IRN:
  - tax invoices / reverse-charge invoices to a buyer with a GSTIN (B2B)
  - export invoices, with IGST (EXPWP) or under LUT (EXPWOP)
  - credit / debit notes against any of those
Not: B2C sales, bills of supply, delivery challans, branch transfers,
margin-scheme invoices (sold to consumers at the counter as a rule).

IRP time limit: at AATO >= EINVOICE_REPORTING_LIMIT_AATO a document can't
be reported more than EINVOICE_REPORTING_DAYS after its date."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.einvoice.providers.common import IST
from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_series import DocumentType
from app.models.note import CreditDebitNote, NoteStatus
from app.models.tenant import RegistrationType
from app.utils import gst_config

B2B_TYPES = {DocumentType.TAX_INVOICE: "B2B", DocumentType.RCM_INVOICE: "B2B",
             DocumentType.EXPORT_INVOICE_IGST: "EXPWP", DocumentType.EXPORT_INVOICE_LUT: "EXPWOP"}


def tenant_in_scope(tenant) -> bool:
    return (tenant.has_feature("einvoice") and tenant.registration_type == RegistrationType.REGULAR
            and tenant.aato is not None and Decimal(tenant.aato) > gst_config.EINVOICE_AATO_THRESHOLD)


def supply_type(invoice: Invoice) -> str | None:
    """INV-01 SupTyp for an invoice, or None if it isn't e-invoiced."""
    if invoice.branch_user_id or invoice.document_type not in B2B_TYPES:
        return None
    kind = B2B_TYPES[invoice.document_type]
    if kind == "B2B" and not ((invoice.customer_snapshot or {}).get("gstin") or "").strip():
        return None
    return kind


def needs_irn(tenant, doc) -> bool:
    """An IRN-able document without one yet (and not void)."""
    if not tenant_in_scope(tenant) or doc.gstin is None or not doc.gstin.gstin:
        return False
    if isinstance(doc, Invoice):
        return doc.status == InvoiceStatus.ISSUED and not doc.irn and supply_type(doc) is not None
    if isinstance(doc, CreditDebitNote):
        return (doc.status == NoteStatus.ISSUED and not doc.irn and doc.original_invoice is not None
                and supply_type(doc.original_invoice) is not None)
    return False


def doc_date(doc) -> date:
    return doc.invoice_date if isinstance(doc, Invoice) else doc.note_date


def reporting_deadline(tenant, doc) -> date | None:
    if tenant.aato is None or Decimal(tenant.aato) < gst_config.EINVOICE_REPORTING_LIMIT_AATO:
        return None
    return doc_date(doc) + timedelta(days=gst_config.EINVOICE_REPORTING_DAYS)


def deadline_warning(tenant, doc, today: date | None = None) -> str | None:
    deadline = reporting_deadline(tenant, doc)
    if deadline is None:
        return None
    today = today or date.today()
    if today > deadline:
        return (f"The IRP accepts this only within {gst_config.EINVOICE_REPORTING_DAYS} days of its date "
                f"(last day was {deadline.strftime('%d %b %Y')}) - it will be refused.")
    if (deadline - today).days <= gst_config.EINVOICE_WARN_DAYS_BEFORE_LIMIT:
        return f"Report this to the IRP by {deadline.strftime('%d %b %Y')} - after that the IRP refuses it."
    return None


def can_cancel_irn(doc, now: datetime | None = None) -> bool:
    if not doc.irn or doc.irn_cancelled_at or not doc.irn_ack_date:
        return False
    now = now or datetime.now(timezone.utc)
    return now - doc.irn_ack_date <= timedelta(hours=gst_config.IRN_CANCEL_HOURS)


# --- e-way bills ---------------------------------------------------------------------

def eway_bill_needed(value) -> bool:
    return Decimal(value or 0) > gst_config.EWAY_BILL_VALUE_THRESHOLD


def eway_validity_days(distance_km: int, odc: bool = False) -> int:
    """Rule 138(10): one day for the first 200 km (20 km for ODC) and one
    more for every further 200 km (20 km) or part of it."""
    per_day = gst_config.EWAY_BILL_KM_PER_DAY_ODC if odc else gst_config.EWAY_BILL_KM_PER_DAY
    distance_km = max(int(distance_km or 0), 1)
    return -(-distance_km // per_day)


def eway_valid_upto(generated_at: datetime, distance_km: int, odc: bool = False) -> datetime:
    """A 'day' runs to midnight of the day after generation, so N days end
    at the end of (generation date + N) - Indian time."""
    local = generated_at.astimezone(IST)
    last = local.date() + timedelta(days=eway_validity_days(distance_km, odc))
    return datetime.combine(last, datetime.max.time().replace(microsecond=0), tzinfo=IST)


def can_cancel_ewb(ewb, now: datetime | None = None) -> bool:
    if ewb.ewb_date is None or ewb.cancelled_at is not None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - ewb.ewb_date <= timedelta(hours=gst_config.EWAY_BILL_CANCEL_HOURS)
