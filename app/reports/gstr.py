"""GST return *working papers* - GSTR-1 and GSTR-3B, built from this
tenant's own invoices, POS bills and credit/debit notes for one GSTIN in
one calendar month.

These are deliberately not a filing tool. Two things a real GSTR-1/3B
needs that this app has no way to know:
  - Input Tax Credit (ITC): this app only records sales, never purchases,
    so GSTR-3B's net-payable figure can't be computed here. The 3B
    working paper shows gross output tax only, with that limitation
    stated plainly next to it.
  - The B2C Large / B2C Small split (GSTR-1 treats a B2C invoice over
    Rs. 2.5 lakh to another state differently): out of scope here, B2C
    is aggregated as a single "B2CS-style" bucket. Flagged in the UI.

Everything else - taxable value, CGST/SGST/IGST, B2B vs B2C split by
whether the customer has a GSTIN on file, HSN-wise summary, and the
document-number-series summary - is computed directly from this
tenant's own records, so those figures are exact.
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_series import DocumentType, DOCUMENT_TYPE_LABELS
from app.models.note import CreditDebitNote, NoteStatus
from app.models.pos_bill import POSBill, POSBillStatus
from app.models.tenant import Gstin, RegistrationType, Tenant

ZERO = Decimal("0")


class ReportPeriodError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def month_bounds(period: str) -> tuple[date, date]:
    """period is 'YYYY-MM'. Returns (first day, last day) of that month."""
    try:
        year_str, month_str = period.split("-")
        year, month = int(year_str), int(month_str)
        first = date(year, month, 1)
    except (ValueError, TypeError):
        raise ReportPeriodError(f"'{period}' is not a valid period - use YYYY-MM.")
    last_day = calendar.monthrange(year, month)[1]
    return first, date(year, month, last_day)


@dataclass
class SupplyLine:
    """One line of one document, already carrying everything GSTR
    aggregation needs - the document's own customer/place-of-supply
    context, and a sign so a credit note subtracts and a debit note
    adds."""

    doc_type: str
    doc_number: str
    doc_date: date
    customer_name: str
    customer_gstin: str | None
    place_of_supply: str
    hsn: str
    gst_rate: Decimal
    unit: str
    qty: Decimal
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    sign: int = 1

    @property
    def is_b2b(self) -> bool:
        return bool(self.customer_gstin)

    @property
    def total(self) -> Decimal:
        return self.sign * (self.taxable_value + self.cgst + self.sgst + self.igst)


@dataclass
class HsnBucket:
    hsn: str
    gst_rate: Decimal
    unit: str
    qty: Decimal = ZERO
    taxable_value: Decimal = ZERO
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    igst: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.taxable_value + self.cgst + self.sgst + self.igst


@dataclass
class B2CBucket:
    place_of_supply: str
    gst_rate: Decimal
    taxable_value: Decimal = ZERO
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    igst: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.taxable_value + self.cgst + self.sgst + self.igst


@dataclass
class DocSeriesSummary:
    label: str
    prefix: str
    from_number: str | None = None
    to_number: str | None = None
    total_count: int = 0
    cancelled_count: int = 0


def _month_datetime_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return datetime.combine(start, time.min), datetime.combine(end, time.max)


def _collect_supply_lines(tenant: Tenant, gstin: Gstin, period: str) -> tuple[list[SupplyLine], dict]:
    """Builds the unified line list plus the raw documents (for the
    document-summary and B2B-invoice-list sections, which need header
    level info the flattened lines don't carry)."""
    start, end = month_bounds(period)
    day_start, day_end = _month_datetime_bounds(start, end)

    invoices = (
        Invoice.query.filter(
            Invoice.tenant_id == tenant.id,
            Invoice.gstin_id == gstin.id,
            Invoice.invoice_date >= start,
            Invoice.invoice_date <= end,
            Invoice.status != InvoiceStatus.VOID,
            Invoice.document_type != DocumentType.BILL_OF_SUPPLY,
        )
        .order_by(Invoice.invoice_date, Invoice.invoice_number)
        .all()
    )
    voided_invoices = (
        Invoice.query.filter(
            Invoice.tenant_id == tenant.id,
            Invoice.gstin_id == gstin.id,
            Invoice.invoice_date >= start,
            Invoice.invoice_date <= end,
            Invoice.status == InvoiceStatus.VOID,
        )
        .all()
    )
    pos_bills = (
        POSBill.query.filter(
            POSBill.tenant_id == tenant.id,
            POSBill.gstin_id == gstin.id,
            POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
            POSBill.completed_at >= day_start,
            POSBill.completed_at <= day_end,
        )
        .order_by(POSBill.completed_at)
        .all()
    )
    notes = (
        CreditDebitNote.query.filter(
            CreditDebitNote.tenant_id == tenant.id,
            CreditDebitNote.gstin_id == gstin.id,
            CreditDebitNote.note_date >= start,
            CreditDebitNote.note_date <= end,
            CreditDebitNote.status != NoteStatus.VOID,
        )
        .order_by(CreditDebitNote.note_date)
        .all()
    )

    lines: list[SupplyLine] = []

    for inv in invoices:
        cust = inv.customer_snapshot or {}
        for l in inv.lines:
            lines.append(
                SupplyLine(
                    doc_type=inv.document_type.value,
                    doc_number=inv.invoice_number,
                    doc_date=inv.invoice_date,
                    customer_name=cust.get("name", ""),
                    customer_gstin=cust.get("gstin") or None,
                    place_of_supply=inv.place_of_supply_state_code,
                    hsn=l.hsn_or_sac_code or "",
                    gst_rate=l.gst_rate,
                    unit=l.unit,
                    qty=l.qty,
                    taxable_value=l.taxable_value,
                    cgst=l.cgst_amount,
                    sgst=l.sgst_amount,
                    igst=l.igst_amount,
                    sign=1,
                )
            )

    for bill in pos_bills:
        cust = bill.customer_snapshot or {}
        # Refunds share the same two-entry philosophy as the Day-book: the
        # original sale's lines count in full, and a proportional negative
        # adjustment covers however much of it was refunded (partial
        # refunds are common - a full line-by-line record of exactly which
        # items were refunded isn't kept, so this is an even split across
        # the bill's own lines, scaled by the refunded share of the total).
        refund_ratio = ZERO
        if bill.status == POSBillStatus.REFUNDED and bill.grand_total:
            refund_ratio = min(Decimal(bill.refunded_amount or 0) / Decimal(bill.grand_total), Decimal("1"))
        for l in bill.lines:
            lines.append(
                SupplyLine(
                    doc_type=DocumentType.POS_BILL.value,
                    doc_number=bill.bill_number or f"held-{bill.id}",
                    doc_date=bill.completed_at.date() if bill.completed_at else start,
                    customer_name=cust.get("name", "Walk-in"),
                    customer_gstin=cust.get("gstin") or None,
                    place_of_supply=bill.gstin.state_code if bill.gstin else gstin.state_code,
                    hsn=l.hsn_or_sac_code or "",
                    gst_rate=l.gst_rate,
                    unit=l.unit,
                    qty=l.qty,
                    taxable_value=l.taxable_value,
                    cgst=l.cgst_amount,
                    sgst=l.sgst_amount,
                    igst=l.igst_amount,
                    sign=1,
                )
            )
            if refund_ratio:
                lines.append(
                    SupplyLine(
                        doc_type=DocumentType.POS_BILL.value,
                        doc_number=f"{bill.bill_number or f'held-{bill.id}'} (refund)",
                        doc_date=bill.refunded_at.date() if bill.refunded_at else start,
                        customer_name=cust.get("name", "Walk-in"),
                        customer_gstin=cust.get("gstin") or None,
                        place_of_supply=bill.gstin.state_code if bill.gstin else gstin.state_code,
                        hsn=l.hsn_or_sac_code or "",
                        gst_rate=l.gst_rate,
                        unit=l.unit,
                        qty=l.qty * refund_ratio,
                        taxable_value=l.taxable_value * refund_ratio,
                        cgst=l.cgst_amount * refund_ratio,
                        sgst=l.sgst_amount * refund_ratio,
                        igst=l.igst_amount * refund_ratio,
                        sign=-1,
                    )
                )

    for note in notes:
        cust = note.customer_snapshot or {}
        sign = -1 if note.document_type == DocumentType.CREDIT_NOTE else 1
        for l in note.lines:
            lines.append(
                SupplyLine(
                    doc_type=note.document_type.value,
                    doc_number=note.note_number,
                    doc_date=note.note_date,
                    customer_name=cust.get("name", ""),
                    customer_gstin=cust.get("gstin") or None,
                    place_of_supply=note.place_of_supply_state_code,
                    hsn=l.hsn_or_sac_code or "",
                    gst_rate=l.gst_rate,
                    unit=l.unit,
                    qty=l.qty,
                    taxable_value=l.taxable_value,
                    cgst=l.cgst_amount,
                    sgst=l.sgst_amount,
                    igst=l.igst_amount,
                    sign=sign,
                )
            )

    raw = {
        "invoices": invoices,
        "voided_invoices": voided_invoices,
        "pos_bills": pos_bills,
        "notes": notes,
        "period_start": start,
        "period_end": end,
    }
    return lines, raw


def _document_summaries(tenant: Tenant, gstin: Gstin, raw: dict) -> list[DocSeriesSummary]:
    summaries: dict[str, DocSeriesSummary] = {}

    def record(label, prefix, number, cancelled=False):
        key = f"{label}:{prefix}"
        s = summaries.setdefault(key, DocSeriesSummary(label=label, prefix=prefix))
        s.total_count += 1
        if cancelled:
            s.cancelled_count += 1
        if s.from_number is None or number < s.from_number:
            s.from_number = number
        if s.to_number is None or number > s.to_number:
            s.to_number = number

    for inv in raw["invoices"]:
        prefix = inv.invoice_number.rsplit("/", 1)[0] if inv.invoice_number else ""
        record(DOCUMENT_TYPE_LABELS.get(inv.document_type, inv.document_type.value), prefix, inv.invoice_number)
    for inv in raw["voided_invoices"]:
        prefix = inv.invoice_number.rsplit("/", 1)[0] if inv.invoice_number else ""
        record(
            DOCUMENT_TYPE_LABELS.get(inv.document_type, inv.document_type.value),
            prefix,
            inv.invoice_number,
            cancelled=True,
        )
    for bill in raw["pos_bills"]:
        if bill.bill_number:
            prefix = bill.bill_number.rsplit("/", 1)[0]
            record("POS Bill", prefix, bill.bill_number)
    for note in raw["notes"]:
        prefix = note.note_number.rsplit("/", 1)[0] if note.note_number else ""
        record(DOCUMENT_TYPE_LABELS.get(note.document_type, note.document_type.value), prefix, note.note_number)

    return sorted(summaries.values(), key=lambda s: (s.label, s.prefix))


def gstr1_data(tenant: Tenant, gstin: Gstin, period: str) -> dict:
    lines, raw = _collect_supply_lines(tenant, gstin, period)

    b2b_by_customer: dict[str, dict] = {}
    b2c: dict[tuple, B2CBucket] = {}
    hsn: dict[tuple, HsnBucket] = {}

    for l in lines:
        hkey = (l.hsn, l.gst_rate, l.unit)
        hb = hsn.setdefault(hkey, HsnBucket(hsn=l.hsn, gst_rate=l.gst_rate, unit=l.unit))
        hb.qty += l.sign * l.qty
        hb.taxable_value += l.sign * l.taxable_value
        hb.cgst += l.sign * l.cgst
        hb.sgst += l.sign * l.sgst
        hb.igst += l.sign * l.igst

        if l.is_b2b:
            entry = b2b_by_customer.setdefault(
                l.customer_gstin, {"customer_gstin": l.customer_gstin, "customer_name": l.customer_name, "documents": {}}
            )
            doc = entry["documents"].setdefault(
                (l.doc_type, l.doc_number),
                {
                    "doc_type": l.doc_type,
                    "doc_number": l.doc_number,
                    "doc_date": l.doc_date,
                    "place_of_supply": l.place_of_supply,
                    "taxable_value": ZERO,
                    "cgst": ZERO,
                    "sgst": ZERO,
                    "igst": ZERO,
                },
            )
            doc["taxable_value"] += l.sign * l.taxable_value
            doc["cgst"] += l.sign * l.cgst
            doc["sgst"] += l.sign * l.sgst
            doc["igst"] += l.sign * l.igst
        else:
            bkey = (l.place_of_supply, l.gst_rate)
            bb = b2c.setdefault(bkey, B2CBucket(place_of_supply=l.place_of_supply, gst_rate=l.gst_rate))
            bb.taxable_value += l.sign * l.taxable_value
            bb.cgst += l.sign * l.cgst
            bb.sgst += l.sign * l.sgst
            bb.igst += l.sign * l.igst

    b2b = []
    for entry in b2b_by_customer.values():
        docs = sorted(entry["documents"].values(), key=lambda d: (d["doc_date"], d["doc_number"]))
        b2b.append({"customer_gstin": entry["customer_gstin"], "customer_name": entry["customer_name"], "documents": docs})
    b2b.sort(key=lambda e: e["customer_gstin"])

    b2c_list = sorted(b2c.values(), key=lambda b: (b.place_of_supply, b.gst_rate))
    hsn_list = sorted(hsn.values(), key=lambda h: (h.hsn, h.gst_rate))
    hsn_list = [h for h in hsn_list if h.qty or h.taxable_value]

    credit_debit_notes = [
        {
            "note_number": n.note_number,
            "note_date": n.note_date,
            "document_type": n.document_type.value,
            "against_invoice": n.original_invoice.invoice_number if n.original_invoice else "",
            "customer_gstin": (n.customer_snapshot or {}).get("gstin"),
            "customer_name": (n.customer_snapshot or {}).get("name", ""),
            "taxable_value": n.total_taxable_value,
            "cgst": n.total_cgst,
            "sgst": n.total_sgst,
            "igst": n.total_igst,
            "total": n.grand_total,
        }
        for n in raw["notes"]
    ]

    totals = {
        "taxable_value": sum((l.sign * l.taxable_value for l in lines), ZERO),
        "cgst": sum((l.sign * l.cgst for l in lines), ZERO),
        "sgst": sum((l.sign * l.sgst for l in lines), ZERO),
        "igst": sum((l.sign * l.igst for l in lines), ZERO),
    }
    totals["total"] = totals["taxable_value"] + totals["cgst"] + totals["sgst"] + totals["igst"]

    return {
        "tenant": tenant,
        "gstin": gstin,
        "period": period,
        "period_start": raw["period_start"],
        "period_end": raw["period_end"],
        "b2b": b2b,
        "b2c": b2c_list,
        "credit_debit_notes": credit_debit_notes,
        "hsn_summary": hsn_list,
        "document_summary": _document_summaries(tenant, gstin, raw),
        "totals": totals,
    }


# GSTR-3B's outward-supplies table (3.1) buckets by nature of supply, not
# by B2B/B2C - a tax invoice is "(a) taxable" whether its customer has a
# GSTIN or not. Composition dealers file GSTR-4, not this.
_TAXABLE_DOC_TYPES = {DocumentType.TAX_INVOICE, DocumentType.POS_BILL, DocumentType.RCM_INVOICE}
_ZERO_RATED_DOC_TYPES = {DocumentType.EXPORT_INVOICE_IGST, DocumentType.EXPORT_INVOICE_LUT}
_NIL_RATED_DOC_TYPES = {DocumentType.BILL_OF_SUPPLY}
_NOTE_DOC_TYPES = {DocumentType.CREDIT_NOTE, DocumentType.DEBIT_NOTE}


def gstr3b_data(tenant: Tenant, gstin: Gstin, period: str) -> dict:
    lines, raw = _collect_supply_lines(tenant, gstin, period)

    buckets = {
        "taxable": {"taxable_value": ZERO, "cgst": ZERO, "sgst": ZERO, "igst": ZERO},
        "zero_rated": {"taxable_value": ZERO, "cgst": ZERO, "sgst": ZERO, "igst": ZERO},
        "nil_rated": {"taxable_value": ZERO, "cgst": ZERO, "sgst": ZERO, "igst": ZERO},
    }

    for l in lines:
        try:
            doc_type = DocumentType(l.doc_type)
        except ValueError:
            doc_type = None
        if doc_type in _NOTE_DOC_TYPES:
            bucket = buckets["taxable"]
        elif doc_type in _ZERO_RATED_DOC_TYPES:
            bucket = buckets["zero_rated"]
        elif doc_type in _NIL_RATED_DOC_TYPES:
            bucket = buckets["nil_rated"]
        else:
            bucket = buckets["taxable"]
        bucket["taxable_value"] += l.sign * l.taxable_value
        bucket["cgst"] += l.sign * l.cgst
        bucket["sgst"] += l.sign * l.sgst
        bucket["igst"] += l.sign * l.igst

    gross_tax_payable = (
        buckets["taxable"]["cgst"]
        + buckets["taxable"]["sgst"]
        + buckets["taxable"]["igst"]
    )

    return {
        "tenant": tenant,
        "gstin": gstin,
        "period": period,
        "period_start": raw["period_start"],
        "period_end": raw["period_end"],
        "outward_taxable": buckets["taxable"],
        "outward_zero_rated": buckets["zero_rated"],
        "outward_nil_rated": buckets["nil_rated"],
        "gross_tax_payable": gross_tax_payable,
    }
