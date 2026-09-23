from flask import current_app, render_template
from weasyprint import HTML

from app.models.pos_bill import POSBill

PAGE_SIZES = {
    "58mm": "58mm auto",
    "80mm": "80mm auto",
    "a4": "A4",
}


def render_receipt_pdf(bill: POSBill, page_format: str = "80mm") -> bytes:
    page_size = PAGE_SIZES.get(page_format, PAGE_SIZES["80mm"])
    html = render_template(
        "pos/receipt_print.html",
        bill=bill,
        firm_name=current_app.config["FIRM_NAME"],
        page_size=page_size,
        is_thermal=page_format != "a4",
    )
    return HTML(string=html, base_url=current_app.root_path).write_pdf()
