from io import BytesIO

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.invoicing.services import parse_line_arrays
from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_series import DOCUMENT_TYPE_LABELS, DocumentType
from app.models.note import CreditDebitNote, NoteStatus
from app.models.user import UserRole
from app.notes import notes_bp
from app.notes.forms import NoteForm
from app.notes.pdf import render_note_pdf
from app.notes.services import NoteInput, NoteValidationError, create_note, void_note
from app.utils.audit import record_audit
from app.utils.tenant_scope import assert_owns, tenant_query


@notes_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
def list_notes():
    status = request.args.get("status", "")
    query = tenant_query(CreditDebitNote)
    if status:
        query = query.filter(CreditDebitNote.status == NoteStatus(status))
    notes = query.order_by(CreditDebitNote.note_date.desc(), CreditDebitNote.id.desc()).limit(200).all()
    return render_template(
        "notes/list.html",
        notes=notes,
        status=status,
        NoteStatus=NoteStatus,
        DOCUMENT_TYPE_LABELS=DOCUMENT_TYPE_LABELS,
    )


@notes_bp.route("/new", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def new_note():
    invoice_id = request.values.get("invoice_id", type=int)
    note_type = request.values.get("type", "")
    document_type = {"credit": DocumentType.CREDIT_NOTE, "debit": DocumentType.DEBIT_NOTE}.get(note_type)

    invoice = tenant_query(Invoice).filter_by(id=invoice_id).first() if invoice_id else None
    if not invoice or not document_type:
        abort(404)
    assert_owns(invoice)
    if invoice.status == InvoiceStatus.VOID:
        flash("Cannot issue a note against a voided invoice.", "error")
        return redirect(url_for("invoicing.view_invoice", invoice_id=invoice.id))

    form = NoteForm()

    if form.validate_on_submit():
        try:
            lines = parse_line_arrays(
                request.form.getlist("line_description[]"),
                request.form.getlist("line_hsn[]"),
                request.form.getlist("line_qty[]"),
                request.form.getlist("line_rate[]"),
                request.form.getlist("line_discount[]"),
                request.form.getlist("line_gst_rate[]"),
                request.form.getlist("line_product_id[]"),
                request.form.getlist("line_unit[]"),
            )
            note = create_note(
                current_user.tenant,
                current_user,
                NoteInput(
                    original_invoice_id=invoice.id,
                    document_type=document_type,
                    note_date=form.note_date.data,
                    reason=form.reason.data,
                    lines=lines,
                ),
            )
            record_audit(
                current_user,
                "note_created",
                tenant_id=current_user.tenant_id,
                entity_type="note",
                entity_id=note.id,
                details={"note_number": note.note_number, "against_invoice": invoice.invoice_number},
            )
            db.session.commit()
            flash(f"{DOCUMENT_TYPE_LABELS[document_type]} {note.note_number} created.", "success")
            return redirect(url_for("notes.view_note", note_id=note.id))
        except NoteValidationError as exc:
            db.session.rollback()
            flash(exc.message, "error")

    return render_template(
        "notes/form.html",
        form=form,
        invoice=invoice,
        document_type=document_type,
        document_label=DOCUMENT_TYPE_LABELS[document_type],
    )


@notes_bp.route("/<int:note_id>")
@roles_required(UserRole.CLIENT_ADMIN)
def view_note(note_id):
    note = tenant_query(CreditDebitNote).filter_by(id=note_id).first_or_404()
    assert_owns(note)
    return render_template(
        "notes/view.html", note=note, document_label=DOCUMENT_TYPE_LABELS[note.document_type]
    )


@notes_bp.route("/<int:note_id>/pdf")
@roles_required(UserRole.CLIENT_ADMIN)
def note_pdf(note_id):
    note = tenant_query(CreditDebitNote).filter_by(id=note_id).first_or_404()
    assert_owns(note)
    pdf_bytes = render_note_pdf(note)
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"{note.note_number.replace('/', '-')}.pdf",
    )


@notes_bp.route("/<int:note_id>/void", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def void_note_route(note_id):
    note = tenant_query(CreditDebitNote).filter_by(id=note_id).first_or_404()
    assert_owns(note)
    try:
        void_note(note, current_user, request.form.get("reason", ""))
        record_audit(
            current_user,
            "note_voided",
            tenant_id=note.tenant_id,
            entity_type="note",
            entity_id=note.id,
            details={"reason": note.void_reason},
        )
        db.session.commit()
        flash(f"{DOCUMENT_TYPE_LABELS[note.document_type]} {note.note_number} voided.", "success")
    except NoteValidationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("notes.view_note", note_id=note.id))
