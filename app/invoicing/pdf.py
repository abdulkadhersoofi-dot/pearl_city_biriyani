from flask import current_app, render_template
from weasyprint import HTML

from app.models.invoice import Invoice
from app.models.invoice_series import DOCUMENT_TYPE_LABELS


def render_invoice_pdf(invoice: Invoice) -> bytes:
    html = render_template(
        "invoicing/pdf.html",
        invoice=invoice,
        document_label=DOCUMENT_TYPE_LABELS[invoice.document_type],
        firm_name=current_app.config["FIRM_NAME"],
    )
    return HTML(string=html, base_url=current_app.root_path).write_pdf()
