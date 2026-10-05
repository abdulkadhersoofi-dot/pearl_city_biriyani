from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.invoicing.services import LineInput
from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.note import CreditDebitNote, NoteLine, NoteStatus
from app.models.tenant import RegistrationType, Tenant
from app.utils.financial_year import financial_year_for
from app.utils.gst import compute_invoice_totals, compute_line
from app.utils.numbering import allocate_number, get_or_create_series

NOTE_DOCUMENT_TYPES = {DocumentType.CREDIT_NOTE, DocumentType.DEBIT_NOTE}


class NoteValidationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class NoteInput:
    original_invoice_id: int
    document_type: DocumentType
    note_date: date
    reason: str
    lines: list[LineInput] = field(default_factory=list)


def create_note(tenant: Tenant, created_by, data: NoteInput) -> CreditDebitNote:
    if data.document_type not in NOTE_DOCUMENT_TYPES:
        raise NoteValidationError("Invalid note type.")
    if not data.reason or not data.reason.strip():
        raise NoteValidationError("A reason is required to issue a note.")
    if not data.lines:
        raise NoteValidationError("Add at least one item to the note.")

    original = Invoice.query.filter_by(id=data.original_invoice_id, tenant_id=tenant.id).first()
    if not original:
        raise NoteValidationError("Select a valid invoice to adjust.")
    if original.status == InvoiceStatus.VOID:
        raise NoteValidationError("Cannot issue a note against a voided invoice.")

    gstin = original.gstin
    # Same rule as a Bill of Supply / POS sale: unregistered and
    # composition clients never charge GST, on any document.
    charge_gst = tenant.registration_type == RegistrationType.REGULAR
    is_intra_state = original.is_intra_state

    computed_lines = []
    for line in data.lines:
        computed = compute_line(
            qty=line.qty,
            rate=line.rate,
            discount_percent=line.discount_percent,
            gst_rate=line.gst_rate,
            is_intra_state=is_intra_state,
            charge_gst=charge_gst,
        )
        computed_lines.append({**computed, "input": line})

    totals = compute_invoice_totals(computed_lines)

    series = get_or_create_series(
        tenant.id, gstin.id, data.document_type, financial_year_for(data.note_date)
    )
    note_number = allocate_number(series)

    note = CreditDebitNote(
        tenant_id=tenant.id,
        gstin_id=gstin.id,
        invoice_series_id=series.id,
        note_number=note_number,
        document_type=data.document_type,
        original_invoice_id=original.id,
        customer_id=original.customer_id,
        customer_snapshot=original.customer_snapshot,
        place_of_supply_state_code=original.place_of_supply_state_code,
        note_date=data.note_date,
        reason=data.reason.strip(),
        status=NoteStatus.ISSUED,
        created_by_id=created_by.id,
        **totals,
    )
    db.session.add(note)
    db.session.flush()

    for order, item in enumerate(computed_lines):
        line_in = item["input"]
        db.session.add(
            NoteLine(
                note_id=note.id,
                product_id=line_in.product_id,
                description=line_in.description,
                hsn_or_sac_code=line_in.hsn_or_sac_code,
                qty=line_in.qty,
                unit=line_in.unit,
                rate=line_in.rate,
                discount_percent=line_in.discount_percent,
                discount_amount=item["discount_amount"],
                taxable_value=item["taxable_value"],
                gst_rate=line_in.gst_rate if charge_gst else Decimal("0"),
                cgst_amount=item["cgst_amount"],
                sgst_amount=item["sgst_amount"],
                igst_amount=item["igst_amount"],
                line_total=item["line_total"],
                sort_order=order,
            )
        )

    return note


def void_note(note: CreditDebitNote, voided_by, reason: str) -> None:
    if note.status == NoteStatus.VOID:
        raise NoteValidationError("Note is already void.")
    if not reason or not reason.strip():
        raise NoteValidationError("A reason is required to void a note.")
    note.status = NoteStatus.VOID
    note.voided_by_id = voided_by.id
    note.void_reason = reason.strip()
    note.voided_at = utcnow()
