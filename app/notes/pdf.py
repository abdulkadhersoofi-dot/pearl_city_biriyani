from flask import current_app, render_template
from weasyprint import HTML

from app.models.invoice_series import DOCUMENT_TYPE_LABELS
from app.models.note import CreditDebitNote
from app.models.tenant import Tenant
from app.utils.uploads import absolute_image_path


def render_note_pdf(note: CreditDebitNote) -> bytes:
    tenant = Tenant.query.get(note.tenant_id)
    html = render_template(
        "notes/pdf.html",
        note=note,
        document_label=DOCUMENT_TYPE_LABELS[note.document_type],
        firm_name=tenant.display_name,
        logo_path=absolute_image_path(tenant.logo_path),
    )
    return HTML(string=html, base_url=current_app.root_path).write_pdf()
