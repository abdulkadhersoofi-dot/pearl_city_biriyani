"""Sales: the one definition every screen uses.

"Sales" for a period is the documents below, each counted on its own date:

    + invoices issued            (invoice date; not delivery challans - a
                                  challan moves stock, it sells nothing)
    + counter (POS) bills        (the day the bill was completed)
    - counter refunds            (the day of the refund, for the amount
                                  refunded - even when the bill is older)
    - counter sales returns      (the day the goods came back)
    - credit notes issued        (note date)
    + debit notes issued         (note date)

The dashboard's tiles, the 14-day trend, the owner's day summary, the Sales
report and its exports, and Profit & loss's revenue all read `entries()` -
so they cannot disagree about which documents count or when. The Books check
(app/finance/checks.py) compares them with the GST returns, which work from
the same documents line by line.

A document is counted in the period its own date falls in. A refund never
changes the figures of the earlier day the bill was made on: yesterday's
report reads the same tomorrow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_series import DocumentType
from app.models.note import CreditDebitNote, NoteStatus
from app.models.pos_bill import POSBill, POSBillStatus
from app.models.pos_return import POSReturn, POSReturnStatus

ZERO = Decimal("0.00")
PAISA = Decimal("0.01")
# Documents kept in the invoices table that are not sales.
NOT_SALES = (DocumentType.DELIVERY_CHALLAN,)

INVOICE, POS_BILL, POS_REFUND, POS_RETURN, CREDIT_NOTE, DEBIT_NOTE = (
    "invoice", "pos_bill", "pos_refund", "pos_return", "credit_note", "debit_note")
# What each kind is to Profit & loss and the reports.
RETURNS = (POS_REFUND, POS_RETURN)


def money(value) -> Decimal:
    return Decimal(value or 0).quantize(PAISA, rounding=ROUND_HALF_UP)


def day_start(d: date) -> datetime:
    """Local midnight, as an aware datetime (POS times are stored aware)."""
    return datetime.combine(d, time.min).astimezone()


def local_day(when) -> date:
    return when.astimezone().date() if hasattr(when, "astimezone") else when


@dataclass
class Entry:
    """One document's effect on sales."""

    kind: str
    on: date
    sign: int              # +1 adds to sales, -1 takes away
    taxable: Decimal       # all amounts positive; `sign` says which way
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total: Decimal         # what the customer pays or gets back, tax included
    doc: object            # the Invoice / POSBill / POSReturn / CreditDebitNote
    # For a part refund: the share of the bill refunded (1 = all of it).
    ratio: Decimal = Decimal("1")

    @property
    def signed_total(self) -> Decimal:
        return self.sign * self.total

    @property
    def signed_taxable(self) -> Decimal:
        return self.sign * self.taxable

    @property
    def tax(self) -> Decimal:
        return self.cgst + self.sgst + self.igst


def entries(tenant, start: date, end: date, branch_ids: list[int] | None = None) -> list[Entry]:
    """Every document that counts towards sales from start to end (both
    days included), in date order.

    branch_ids: only what is attributable to these branch logins - a counter
    bill rung up by the branch, an invoice billed to it (and the notes
    against that invoice)."""
    lo, hi = day_start(start), day_start(end + timedelta(days=1))
    out: list[Entry] = []

    invoices = Invoice.query.filter(
        Invoice.tenant_id == tenant.id, Invoice.status == InvoiceStatus.ISSUED,
        Invoice.document_type.notin_(NOT_SALES), Invoice.invoice_date >= start, Invoice.invoice_date <= end)
    if branch_ids:
        invoices = invoices.filter(Invoice.branch_user_id.in_(branch_ids))
    for inv in invoices:
        # A reverse-charge invoice carries its tax in the rc* columns: what
        # was sold is the same either way.
        out.append(Entry(INVOICE, inv.invoice_date, 1, Decimal(inv.total_taxable_value),
                         Decimal(inv.total_cgst) + Decimal(inv.total_rcgst or 0),
                         Decimal(inv.total_sgst) + Decimal(inv.total_rsgst or 0),
                         Decimal(inv.total_igst) + Decimal(inv.total_rigst or 0),
                         Decimal(inv.grand_total), inv))

    notes = CreditDebitNote.query.filter(
        CreditDebitNote.tenant_id == tenant.id, CreditDebitNote.status == NoteStatus.ISSUED,
        CreditDebitNote.note_date >= start, CreditDebitNote.note_date <= end)
    if branch_ids:
        notes = notes.join(Invoice, CreditDebitNote.original_invoice_id == Invoice.id) \
            .filter(Invoice.branch_user_id.in_(branch_ids))
    for note in notes:
        debit = note.document_type == DocumentType.DEBIT_NOTE
        out.append(Entry(DEBIT_NOTE if debit else CREDIT_NOTE, note.note_date, 1 if debit else -1,
                         Decimal(note.total_taxable_value), Decimal(note.total_cgst), Decimal(note.total_sgst),
                         Decimal(note.total_igst), Decimal(note.grand_total), note))

    counted = [POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]
    bills = POSBill.query.filter(POSBill.tenant_id == tenant.id, POSBill.status.in_(counted),
                                 POSBill.completed_at >= lo, POSBill.completed_at < hi)
    if branch_ids:
        bills = bills.filter(POSBill.created_by_id.in_(branch_ids))
    for bill in bills:
        out.append(Entry(POS_BILL, local_day(bill.completed_at), 1, Decimal(bill.total_taxable_value),
                         Decimal(bill.total_cgst), Decimal(bill.total_sgst), Decimal(bill.total_igst),
                         Decimal(bill.grand_total), bill))

    refunded = POSBill.query.filter(POSBill.tenant_id == tenant.id, POSBill.status == POSBillStatus.REFUNDED,
                                    POSBill.refunded_at >= lo, POSBill.refunded_at < hi)
    if branch_ids:
        refunded = refunded.filter(POSBill.created_by_id.in_(branch_ids))
    for bill in refunded:
        if not bill.grand_total:
            continue
        ratio = min(Decimal(bill.refunded_amount or 0) / Decimal(bill.grand_total), Decimal("1"))
        if not ratio:
            continue
        out.append(Entry(POS_REFUND, local_day(bill.refunded_at), -1, money(bill.total_taxable_value * ratio),
                         money(bill.total_cgst * ratio), money(bill.total_sgst * ratio),
                         money(bill.total_igst * ratio), money(Decimal(bill.grand_total) * ratio), bill, ratio))

    returns = POSReturn.query.filter(POSReturn.tenant_id == tenant.id, POSReturn.status == POSReturnStatus.COMPLETED,
                                     POSReturn.returned_at >= lo, POSReturn.returned_at < hi)
    if branch_ids:
        returns = returns.filter(POSReturn.created_by_id.in_(branch_ids))
    for ret in returns:
        out.append(Entry(POS_RETURN, local_day(ret.returned_at), -1, Decimal(ret.total_taxable_value),
                         Decimal(ret.total_cgst), Decimal(ret.total_sgst), Decimal(ret.total_igst),
                         Decimal(ret.grand_total), ret))

    out.sort(key=lambda e: e.on)
    return out


def total(tenant, start: date, end: date, branch_ids: list[int] | None = None) -> Decimal:
    """Sales for the period, tax included."""
    return sum((e.signed_total for e in entries(tenant, start, end, branch_ids)), ZERO)


def by_day(tenant, start: date, end: date) -> list[tuple[date, Decimal]]:
    """Sales for each day from start to end. The days add up to total()
    over the same range."""
    days = {start + timedelta(days=i): ZERO for i in range((end - start).days + 1)}
    for e in entries(tenant, start, end):
        if e.on in days:
            days[e.on] += e.signed_total
    return sorted(days.items())


def totals(rows: list[Entry]) -> dict:
    return {
        "taxable_value": sum((e.sign * e.taxable for e in rows), ZERO),
        "cgst": sum((e.sign * e.cgst for e in rows), ZERO),
        "sgst": sum((e.sign * e.sgst for e in rows), ZERO),
        "igst": sum((e.sign * e.igst for e in rows), ZERO),
        "total": sum((e.signed_total for e in rows), ZERO),
    }
