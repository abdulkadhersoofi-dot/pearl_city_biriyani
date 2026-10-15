"""GST return *working papers* - GSTR-1 and GSTR-3B, built from this
tenant's own invoices, POS bills and credit/debit notes for one GSTIN in
one calendar month.

These are deliberately not a filing tool. Two things a real GSTR-1/3B
needs that this app has no way to know:
  - Input Tax Credit (ITC): only clients with the Purchases module on
    record purchases. For them the 3B working paper adds table 3.1(d)
    and table 4 from their purchase bills (itc_summary); for everyone
    else it shows gross output tax only, with that limitation stated
    plainly next to it.
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
from app.models.pos_return import POSReturn, POSReturnStatus
from app.models.tenant import Gstin, RegistrationType, Tenant

ZERO = Decimal("0")


class ReportPeriodError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def normalized_customer_gstin(snapshot: dict | None) -> str | None:
    """Blank-or-whitespace means "no GSTIN" (B2C), not a truthy string -
    a frozen customer_snapshot can carry either depending on when it was
    captured, so this is the one place that decides what counts as unset."""
    return ((snapshot or {}).get("gstin") or "").strip() or None


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
    # Reverse-charge tax (RCM_INVOICE only) - this tenant's recipient pays
    # it, not this tenant, so it's tracked separately from cgst/sgst/igst
    # above: gstr1_data folds it back in for disclosure (a GSTR-1 B2B row
    # still reports an RCM supply's tax), gstr3b_data deliberately doesn't
    # (it isn't this tenant's own output tax payable).
    rcgst: Decimal = ZERO
    rsgst: Decimal = ZERO
    rigst: Decimal = ZERO
    sign: int = 1
    # Margin scheme (Rule 32(5)): taxable_value / tax above are the
    # margin's; this is the rest of the selling price, which the HSN
    # summary discloses on a 0% row of the same HSN.
    margin_remainder: Decimal = ZERO

    @property
    def is_b2b(self) -> bool:
        return bool(self.customer_gstin)

    @property
    def total(self) -> Decimal:
        return self.sign * (self.taxable_value + self.cgst + self.sgst + self.igst + self.rcgst + self.rsgst + self.rigst)


def _remainder(line) -> Decimal:
    """Selling price - margin, for a margin-scheme sale line (0 otherwise)."""
    if not getattr(line, "is_margin_scheme", False):
        return ZERO
    return line.line_total - line.taxable_value - line.cgst_amount - line.sgst_amount - line.igst_amount


@dataclass
class HsnBucket:
    hsn: str
    gst_rate: Decimal
    unit: str
    # The 0% row disclosing the non-margin part of margin-scheme sales.
    margin_remainder: bool = False
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
    voided_pos_bills = POSBill.query.filter(
        POSBill.tenant_id == tenant.id,
        POSBill.gstin_id == gstin.id,
        POSBill.status == POSBillStatus.VOIDED,
        POSBill.completed_at >= day_start,
        POSBill.completed_at <= day_end,
    ).all()
    # Item-wise POS sales returns, in the month the goods came back. A
    # voided one only shows in the document summary, as cancelled.
    pos_returns_all = (
        POSReturn.query.filter(
            POSReturn.tenant_id == tenant.id,
            POSReturn.gstin_id == gstin.id,
            POSReturn.returned_at >= day_start,
            POSReturn.returned_at <= day_end,
        )
        .order_by(POSReturn.returned_at)
        .all()
    )
    pos_returns = [r for r in pos_returns_all if r.status == POSReturnStatus.COMPLETED]
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
                    customer_gstin=normalized_customer_gstin(cust),
                    place_of_supply=inv.place_of_supply_state_code,
                    hsn=l.hsn_or_sac_code or "",
                    gst_rate=l.gst_rate,
                    unit=l.unit,
                    qty=l.qty,
                    taxable_value=l.taxable_value,
                    cgst=l.cgst_amount,
                    sgst=l.sgst_amount,
                    igst=l.igst_amount,
                    rcgst=l.rcgst_amount,
                    rsgst=l.rsgst_amount,
                    rigst=l.rigst_amount,
                    sign=1,
                    margin_remainder=_remainder(l),
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
                    customer_gstin=normalized_customer_gstin(cust),
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
                    margin_remainder=_remainder(l),
                )
            )
            if refund_ratio:
                lines.append(
                    SupplyLine(
                        doc_type=DocumentType.POS_BILL.value,
                        doc_number=f"{bill.bill_number or f'held-{bill.id}'} (refund)",
                        doc_date=bill.refunded_at.date() if bill.refunded_at else start,
                        customer_name=cust.get("name", "Walk-in"),
                        customer_gstin=normalized_customer_gstin(cust),
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
                        margin_remainder=_remainder(l) * refund_ratio,
                    )
                )

    for ret in pos_returns:
        cust = ret.customer_snapshot or {}
        margin_return = bool(ret.bill and ret.bill.is_margin_scheme)
        for l in ret.lines:
            lines.append(
                SupplyLine(
                    doc_type=DocumentType.POS_RETURN.value,
                    doc_number=ret.return_number,
                    margin_remainder=(l.line_total - l.taxable_value - l.cgst_amount - l.sgst_amount - l.igst_amount)
                    if margin_return else ZERO,
                    doc_date=ret.returned_at.date(),
                    customer_name=cust.get("name", "Walk-in"),
                    customer_gstin=normalized_customer_gstin(cust),
                    place_of_supply=ret.gstin.state_code if ret.gstin else gstin.state_code,
                    hsn=l.hsn_or_sac_code or "",
                    gst_rate=l.gst_rate,
                    unit=l.unit,
                    qty=l.qty,
                    taxable_value=l.taxable_value,
                    cgst=l.cgst_amount,
                    sgst=l.sgst_amount,
                    igst=l.igst_amount,
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
                    customer_gstin=normalized_customer_gstin(cust),
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
        "voided_pos_bills": voided_pos_bills,
        "pos_returns": pos_returns,
        "pos_returns_all": pos_returns_all,
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
    margin_label = DOCUMENT_TYPE_LABELS[DocumentType.MARGIN_SCHEME_INVOICE]
    for bill in raw["pos_bills"]:
        if bill.bill_number:
            prefix = bill.bill_number.rsplit("/", 1)[0]
            record(margin_label if bill.is_margin_scheme else "POS Bill", prefix, bill.bill_number)
    for bill in raw["voided_pos_bills"]:
        if bill.bill_number:
            record(margin_label if bill.is_margin_scheme else "POS Bill", bill.bill_number.rsplit("/", 1)[0],
                   bill.bill_number, cancelled=True)
    for ret in raw["pos_returns_all"]:
        record(
            DOCUMENT_TYPE_LABELS[DocumentType.POS_RETURN],
            ret.return_number.rsplit("/", 1)[0],
            ret.return_number,
            cancelled=ret.status != POSReturnStatus.COMPLETED,
        )
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
        # GSTR-1 still has to disclose an RCM supply's tax (a B2B invoice
        # row reports it, flagged reverse-charge, same as any other tax
        # amount) even though GSTR-3B must not count it as this tenant's
        # own payable - so here, unlike gstr3b_data below, rcgst/rsgst/
        # rigst are folded back into the ordinary cgst/sgst/igst figures.
        eff_cgst = l.cgst + l.rcgst
        eff_sgst = l.sgst + l.rsgst
        eff_igst = l.igst + l.rigst

        hkey = (l.hsn, l.gst_rate, l.unit)
        hb = hsn.setdefault(hkey, HsnBucket(hsn=l.hsn, gst_rate=l.gst_rate, unit=l.unit))
        hb.qty += l.sign * l.qty
        hb.taxable_value += l.sign * l.taxable_value
        hb.cgst += l.sign * eff_cgst
        hb.sgst += l.sign * eff_sgst
        hb.igst += l.sign * eff_igst
        if l.margin_remainder:
            # Margin scheme: the part of the price that isn't margin, on a
            # 0% row of the same HSN (no quantity - the units are on the
            # margin row above).
            mkey = (l.hsn, ZERO, l.unit, "margin")
            mb = hsn.setdefault(mkey, HsnBucket(hsn=l.hsn, gst_rate=ZERO, unit=l.unit, margin_remainder=True))
            mb.taxable_value += l.sign * l.margin_remainder

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
            doc["cgst"] += l.sign * eff_cgst
            doc["sgst"] += l.sign * eff_sgst
            doc["igst"] += l.sign * eff_igst
        else:
            bkey = (l.place_of_supply, l.gst_rate)
            bb = b2c.setdefault(bkey, B2CBucket(place_of_supply=l.place_of_supply, gst_rate=l.gst_rate))
            bb.taxable_value += l.sign * l.taxable_value
            bb.cgst += l.sign * eff_cgst
            bb.sgst += l.sign * eff_sgst
            bb.igst += l.sign * eff_igst

    b2b = []
    for entry in b2b_by_customer.values():
        docs = sorted(entry["documents"].values(), key=lambda d: (d["doc_date"], d["doc_number"]))
        b2b.append({"customer_gstin": entry["customer_gstin"], "customer_name": entry["customer_name"], "documents": docs})
    b2b.sort(key=lambda e: e["customer_gstin"])

    b2c_list = sorted(b2c.values(), key=lambda b: (b.place_of_supply, b.gst_rate))
    hsn_list = sorted(hsn.values(), key=lambda h: (h.hsn, h.margin_remainder, h.gst_rate))
    hsn_list = [h for h in hsn_list if h.qty or h.taxable_value]

    credit_debit_notes = [
        {
            "note_number": n.note_number,
            "note_date": n.note_date,
            "document_type": n.document_type.value,
            "against_invoice": n.original_invoice.invoice_number if n.original_invoice else "",
            "customer_gstin": normalized_customer_gstin(n.customer_snapshot),
            "customer_name": (n.customer_snapshot or {}).get("name", ""),
            "taxable_value": n.total_taxable_value,
            "cgst": n.total_cgst,
            "sgst": n.total_sgst,
            "igst": n.total_igst,
            "total": n.grand_total,
        }
        for n in raw["notes"]
    ]
    # A POS return to a registered customer is reported the way a credit
    # note is - it reduces that customer's B2B supplies.
    credit_debit_notes += [
        {
            "note_number": r.return_number,
            "note_date": r.returned_at.date(),
            "document_type": DocumentType.POS_RETURN.value,
            "against_invoice": r.bill.bill_number if r.bill else "",
            "customer_gstin": normalized_customer_gstin(r.customer_snapshot),
            "customer_name": (r.customer_snapshot or {}).get("name", ""),
            "taxable_value": r.total_taxable_value,
            "cgst": r.total_cgst,
            "sgst": r.total_sgst,
            "igst": r.total_igst,
            "total": r.grand_total,
        }
        for r in raw["pos_returns"]
        if normalized_customer_gstin(r.customer_snapshot)
    ]

    totals = {
        "taxable_value": sum((l.sign * l.taxable_value for l in lines), ZERO),
        "cgst": sum((l.sign * (l.cgst + l.rcgst) for l in lines), ZERO),
        "sgst": sum((l.sign * (l.sgst + l.rsgst) for l in lines), ZERO),
        "igst": sum((l.sign * (l.igst + l.rigst) for l in lines), ZERO),
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
_NOTE_DOC_TYPES = {DocumentType.CREDIT_NOTE, DocumentType.DEBIT_NOTE, DocumentType.POS_RETURN}


def gstr3b_data(tenant: Tenant, gstin: Gstin, period: str) -> dict:
    lines, raw = _collect_supply_lines(tenant, gstin, period)

    # Deliberately sums only l.cgst/sgst/igst below, never l.rcgst/rsgst/
    # rigst - an RCM_INVOICE's tax is the recipient's liability, not this
    # tenant's, so it must not inflate gross_tax_payable (this tenant's
    # own output tax). The taxable value still counts normally; only the
    # tax itself is excluded. Contrast gstr1_data, which does disclose it.
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
        # Only for clients with the Purchases module on - everyone else's
        # working paper (and its JSON) stays exactly as it was.
        **({"itc": itc_summary(tenant, gstin, period)}
           if tenant.has_feature("purchases") or tenant.has_feature("expenses") else {}),
    }


def _heads() -> dict:
    return {"taxable_value": ZERO, "cgst": ZERO, "sgst": ZERO, "igst": ZERO}


def itc_summary(tenant: Tenant, gstin: Gstin, period: str) -> dict:
    """GSTR-3B table 3.1(d) and table 4 from the client's purchase bills
    (by the date they were entered) and returns to suppliers in the month.

      3.1(d)  inward supplies liable to reverse charge - the RCM bills'
              value and the tax the client pays on them
      4A(3)   ITC on those reverse-charge purchases
      4A(5)   all other ITC - GST charged on the other purchase bills
      4B(1)   reversed: GST on lines not eligible (s.17(5) blocked, or
              otherwise ineligible) - availed in 4A, reversed here
      4B(2)   reversed: credit of goods sent back to suppliers
      4C      net ITC available = 4A - 4B

    Posted expenses with a GST bill (module "expenses") are counted the
    same way, by their expense date.

    As with the rest of this working paper, the claim must still be
    checked against GSTR-2B (Purchases -> GSTR-2B match) before filing."""
    from app.models.purchase import ItcEligibility, PurchaseBill, PurchaseBillLine, PurchaseReturn, PurchaseStatus

    start, end = month_bounds(period)
    bills = PurchaseBill.query.filter(
        PurchaseBill.tenant_id == tenant.id, PurchaseBill.gstin_id == gstin.id,
        PurchaseBill.status == PurchaseStatus.POSTED, PurchaseBill.bill_date >= start, PurchaseBill.bill_date <= end,
    ).all()
    rcm_inward, a3, a5, b1, b2 = _heads(), _heads(), _heads(), _heads(), _heads()
    for bill in bills:
        target = a3 if bill.is_rcm else a5
        if bill.is_rcm:
            rcm_inward["taxable_value"] += bill.total_taxable_value
        target["taxable_value"] += bill.total_taxable_value
        for head in ("cgst", "sgst", "igst"):
            amount = getattr(bill, f"total_{head}")
            target[head] += amount
            if bill.is_rcm:
                rcm_inward[head] += amount
    if bills:
        lost = (
            PurchaseBillLine.query.filter(PurchaseBillLine.bill_id.in_([b.id for b in bills]),
                                          PurchaseBillLine.itc_eligibility != ItcEligibility.ELIGIBLE).all()
        )
        for line in lost:
            b1["taxable_value"] += line.taxable_value
            b1["cgst"] += line.cgst_amount
            b1["sgst"] += line.sgst_amount
            b1["igst"] += line.igst_amount
    for ret in PurchaseReturn.query.filter(
        PurchaseReturn.tenant_id == tenant.id, PurchaseReturn.gstin_id == gstin.id,
        PurchaseReturn.status == PurchaseStatus.POSTED, PurchaseReturn.return_date >= start,
        PurchaseReturn.return_date <= end,
    ):
        b2["taxable_value"] += ret.total_taxable_value
        b2["cgst"] += ret.itc_cgst
        b2["sgst"] += ret.itc_sgst
        b2["igst"] += ret.itc_igst
    # Expenses with a GST bill count exactly like a purchase bill: their
    # tax is availed in 4A and whatever of it isn't credit (blocked, or
    # the client can't claim) is reversed in 4B(1).
    from app.models.expense import Expense, ExpenseStatus

    expenses = Expense.query.filter(
        Expense.tenant_id == tenant.id, Expense.gstin_id == gstin.id, Expense.status == ExpenseStatus.POSTED,
        Expense.is_gst_bill.is_(True), Expense.expense_date >= start, Expense.expense_date <= end,
    ).all()
    for e in expenses:
        target = a3 if e.is_rcm else a5
        target["taxable_value"] += e.taxable_value
        if e.is_rcm:
            rcm_inward["taxable_value"] += e.taxable_value
        for head in ("cgst", "sgst", "igst"):
            tax, credit = getattr(e, f"{head}_amount"), getattr(e, f"itc_{head}")
            target[head] += tax
            b1[head] += tax - credit
            if e.is_rcm:
                rcm_inward[head] += tax
        if e.itc_total != e.total_tax:
            b1["taxable_value"] += e.taxable_value
    net = {h: a3[h] + a5[h] - b1[h] - b2[h] for h in ("cgst", "sgst", "igst")}
    return {
        "rcm_inward": rcm_inward,
        "a3_rcm": a3,
        "a5_other": a5,
        "b1_ineligible": b1,
        "b2_returns": b2,
        "net": net,
        "net_total": net["cgst"] + net["sgst"] + net["igst"],
        "rcm_tax_payable": rcm_inward["cgst"] + rcm_inward["sgst"] + rcm_inward["igst"],
        "bill_count": len(bills),
        "expense_count": len(expenses),
    }
