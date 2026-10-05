from flask import current_app, render_template
from weasyprint import HTML

from app.models.invoice_series import DOCUMENT_TYPE_LABELS
from app.models.note import CreditDebitNote
from app.models.tenant import Tenant
from app.utils.uploads import image_data_uri


def render_note_pdf(note: CreditDebitNote) -> bytes:
    tenant = Tenant.query.get(note.tenant_id)
    html = render_template(
        "notes/pdf.html",
        note=note,
        document_label=DOCUMENT_TYPE_LABELS[note.document_type],
        firm_name=tenant.display_name,
        logo_data_uri=image_data_uri(tenant.logo_image_id),
    )
    return HTML(string=html, base_url=current_app.root_path).write_pdf()
