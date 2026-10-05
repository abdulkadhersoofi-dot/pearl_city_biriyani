from flask import current_app, render_template
from weasyprint import HTML

from app.models.invoice_series import DOCUMENT_TYPE_LABELS
from app.models.note import CreditDebitNote


def render_note_pdf(note: CreditDebitNote) -> bytes:
    html = render_template(
        "notes/pdf.html",
        note=note,
        document_label=DOCUMENT_TYPE_LABELS[note.document_type],
        firm_name=current_app.config["FIRM_NAME"],
    )
    return HTML(string=html, base_url=current_app.root_path).write_pdf()
