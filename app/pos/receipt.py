from flask import current_app, render_template
from weasyprint import HTML

from app.models.pos_bill import POSBill
from app.models.tenant import Tenant

# Keyed and labeled the way thermal paper is actually sold in a shop - by
# roll width in inches ("2 inch roll", "3 inch roll") - not by the mm
# figure printed in a spec sheet nobody buying paper looks at. The CSS
# page width is the same physical width as before (58mm/80mm), just
# expressed in inches.
THERMAL_WIDTHS_IN = {
    "2in": 2.28,
    "3in": 3.15,
}
DEFAULT_PAGE_FORMAT = "3in"

# CSS Paged Media's `size` property has no "fixed width, auto height"
# value - "<length> auto" is not valid syntax and WeasyPrint silently
# ignores it, falling back to its own default (A4-ish) page size. That
# was the actual, more serious bug behind receipts looking wrong on
# thermal paper: every "58mm"/"80mm" receipt was secretly being laid out
# on a full A4 page the whole time.
#
# The fix needs an explicit height too, and that height has to actually
# fit the content: too tall and a PDF viewer's "fit to page" view shrinks
# the real content to an invisible sliver; too short and WeasyPrint
# spills the rest onto a second page, which a real receipt obviously
# can't have. A row count times a guessed row height is too fragile to
# get right for every font/wrapping case (long item names wrap, HSN
# codes may or may not be set, etc.), so instead this seeds a height
# from the line count, then - if WeasyPrint still reports more than one
# page - doubles it and re-renders until it all fits on one page. A few
# extra render passes is cheap, and it's only ever paid once, at print
# time, not on every page view.
_FIXED_OVERHEAD_IN = 7.0  # firm name/GSTIN, receipt#/date, rules, totals, payment line, footer
_PER_LINE_IN = 1.0  # each cart line prints as 2 rows
_GROWTH_FACTOR = 1.8
_MAX_ATTEMPTS = 6


def render_receipt_pdf(bill: POSBill, page_format: str = DEFAULT_PAGE_FORMAT, copy_label: str | None = None) -> bytes:
    """copy_label: None for the original; "DUPLICATE COPY" for any reprint,
    "CUSTOMER COPY" for the shared link (app.pos.routes.receipt_pdf)."""
    tenant = Tenant.query.get(bill.tenant_id)
    context = {"bill": bill, "copy_label": copy_label, "upi_qr": _credit_upi_qr(tenant, bill)}
    extra_lines = len(bill.payments) + (4 if context["upi_qr"] else 0)
    return _render_pdf("pos/receipt_print.html", tenant, context, len(bill.lines) + extra_lines, page_format)


def render_return_pdf(ret, page_format: str = DEFAULT_PAGE_FORMAT) -> bytes:
    tenant = Tenant.query.get(ret.tenant_id)
    return _render_pdf("pos/return_print.html", tenant, {"ret": ret}, len(ret.lines), page_format)


def _credit_upi_qr(tenant, bill) -> str | None:
    """A pay-now QR is only printed for what's still owed (a credit / pay-
    later part of the bill) - on a paid bill it would invite paying twice."""
    from app.pos.share import qr_svg_data_uri, upi_uri

    owed = bill.credit_amount
    if not tenant.upi_vpa or not owed or not bill.bill_number:
        return None
    return qr_svg_data_uri(upi_uri(tenant.upi_vpa, tenant.upi_payee_name or tenant.display_name, owed, bill.bill_number))


def _render_pdf(template: str, tenant, context: dict, line_count: int, page_format: str) -> bytes:
    if page_format not in THERMAL_WIDTHS_IN and page_format != "a4":
        page_format = DEFAULT_PAGE_FORMAT
    if page_format == "a4":
        return _render(template, tenant, context, "A4")

    width_in = THERMAL_WIDTHS_IN[page_format]
    height_in = _FIXED_OVERHEAD_IN + _PER_LINE_IN * max(line_count, 1)
    document = None
    for _ in range(_MAX_ATTEMPTS):
        document = _render(template, tenant, context, f"{width_in}in {height_in:.2f}in", as_document=True)
        if len(document.pages) <= 1:
            break
        height_in *= _GROWTH_FACTOR
    return document.write_pdf()


def _render(template: str, tenant, context: dict, page_size: str, as_document: bool = False):
    is_thermal = page_size != "A4"
    # The 2-inch roll is narrow enough that the 3-inch roll's font size
    # wraps long values (the bill number especially) awkwardly - one size
    # doesn't fit both widths.
    is_narrow = is_thermal and page_size.startswith(f"{THERMAL_WIDTHS_IN['2in']}in")
    html = render_template(
        template,
        firm_name=tenant.display_name,
        page_size=page_size,
        is_thermal=is_thermal,
        is_narrow=is_narrow,
        **context,
    )
    doc = HTML(string=html, base_url=current_app.root_path).render()
    return doc if as_document else doc.write_pdf()
