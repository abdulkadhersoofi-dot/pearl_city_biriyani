from flask import current_app, render_template
from weasyprint import HTML

from app.models.invoice import Invoice
from app.models.invoice_series import DOCUMENT_TYPE_LABELS
from app.models.tenant import Tenant
from app.utils.uploads import absolute_image_path


def render_invoice_pdf(invoice: Invoice) -> bytes:
    tenant = Tenant.query.get(invoice.tenant_id)
    html = render_template(
        "invoicing/pdf.html",
        invoice=invoice,
        document_label=DOCUMENT_TYPE_LABELS[invoice.document_type],
        firm_name=tenant.display_name,
        logo_path=absolute_image_path(tenant.logo_path),
    )
    return HTML(string=html, base_url=current_app.root_path).write_pdf()
