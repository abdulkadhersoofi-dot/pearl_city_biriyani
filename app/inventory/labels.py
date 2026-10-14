"""Barcode labels for shelf and product stickers - Code 128 for any code,
EAN-13 when the item already carries a valid 13-digit EAN. Rendered as
SVG (python-barcode) inside a WeasyPrint page: an A4 sheet of 3 x 8
labels, or a 50 x 25 mm roll for label printers."""

from base64 import b64encode
from io import BytesIO

import barcode
from barcode.writer import SVGWriter
from flask import current_app, render_template
from weasyprint import HTML

LAYOUTS = {
    "a4": "A4 sheet (24 per page, 70 x 37 mm)",
    "roll": "Label roll (50 x 25 mm)",
}
MAX_LABELS = 1000


def _ean13_ok(code: str) -> bool:
    if len(code) != 13 or not code.isdigit():
        return False
    digits = [int(c) for c in code]
    check = (10 - sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits[:12])) % 10) % 10
    return check == digits[12]


def label_code(product) -> str:
    """What goes in the bars: the item's barcode, else its SKU, else an
    internal code the POS barcode search also recognises (its id)."""
    return (product.barcode or product.sku or f"ARFA{product.id:06d}").strip()


def barcode_svg(code: str) -> str:
    """A data: URI of the code's barcode as SVG."""
    kind = "ean13" if _ean13_ok(code) else "code128"
    value = code[:12] if kind == "ean13" else code
    out = BytesIO()
    barcode.get(kind, value, writer=SVGWriter()).write(
        out, options={"write_text": False, "module_height": 10.0, "quiet_zone": 2.0}
    )
    return "data:image/svg+xml;base64," + b64encode(out.getvalue()).decode()


def build_labels(items: list[tuple[object, int]], show_price: bool = True) -> list[dict]:
    labels = []
    for product, copies in items:
        code = label_code(product)
        svg = barcode_svg(code)
        for _ in range(copies):
            labels.append({
                "name": product.name,
                "code": code,
                "svg": svg,
                "price": product.sale_price if show_price else None,
                "mrp": product.mrp if show_price else None,
            })
            if len(labels) >= MAX_LABELS:
                return labels
    return labels


def render_labels_pdf(labels: list[dict], layout: str, firm_name: str) -> bytes:
    html = render_template("inventory/labels_pdf.html", labels=labels, layout=layout, firm_name=firm_name)
    return HTML(string=html, base_url=current_app.root_path).write_pdf()
