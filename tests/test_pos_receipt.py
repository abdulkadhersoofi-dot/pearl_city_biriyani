from decimal import Decimal
from io import BytesIO

from pypdf import PdfReader

from app.pos.receipt import render_receipt_pdf
from app.pos.services import CartInput, CartLineInput, checkout


def _cart(n=1):
    return CartInput(
        lines=[
            CartLineInput(
                description=f"Item {i}",
                hsn_or_sac_code="996331",
                qty=Decimal("2"),
                rate=Decimal("150"),
                gst_rate=Decimal("5"),
                unit="plate",
            )
            for i in range(n)
        ]
    )


def _page_width_pt(pdf_bytes: bytes) -> float:
    reader = PdfReader(BytesIO(pdf_bytes))
    box = reader.pages[0].mediabox
    return float(box.width)


def test_thermal_receipt_pdf_uses_the_real_paper_width_not_a4(db, tenant, client_admin):
    """CSS Paged Media's `size` property has no "<length> auto" value -
    an earlier version of this code used it, WeasyPrint silently ignored
    it, and every thermal receipt ended up laid out on a full A4 page
    (595pt wide) instead of its actual ~58mm/80mm paper width (under
    240pt). Guard against that regression on the real generated PDF's
    page geometry, not just that generation didn't crash."""
    from app.models.pos_bill import PaymentMode

    bill = checkout(tenant, client_admin, _cart(), PaymentMode.CASH)
    db.session.commit()

    for fmt in ("2in", "3in"):
        width_pt = _page_width_pt(render_receipt_pdf(bill, fmt))
        assert width_pt < 240, f"{fmt} receipt is {width_pt}pt wide - looks like the A4 fallback, not thermal paper"


def test_a4_copy_is_the_full_a4_width(db, tenant, client_admin):
    from app.models.pos_bill import PaymentMode

    bill = checkout(tenant, client_admin, _cart(), PaymentMode.CASH)
    db.session.commit()

    width_pt = _page_width_pt(render_receipt_pdf(bill, "a4"))
    assert 590 < width_pt < 600  # A4 = 595.28pt wide


def test_large_cart_still_fits_on_a_single_page(db, tenant, client_admin):
    """A fixed page height is as wrong as the invalid 'auto' it replaces
    - too short and the receipt spills onto a second page (impossible on
    a real roll printer); too tall and a PDF viewer's fit-to-page view
    shrinks the real content to an invisible sliver. Growing the page
    only as far as the content actually needs avoids both."""
    from app.models.pos_bill import PaymentMode

    bill = checkout(tenant, client_admin, _cart(30), PaymentMode.CASH)
    db.session.commit()

    reader = PdfReader(BytesIO(render_receipt_pdf(bill, "2in")))
    assert len(reader.pages) == 1


def test_receipt_pdf_defaults_to_a_thermal_size_when_format_is_unknown(db, tenant, client_admin):
    from app.models.pos_bill import PaymentMode

    bill = checkout(tenant, client_admin, _cart(), PaymentMode.CASH)
    db.session.commit()

    width_pt = _page_width_pt(render_receipt_pdf(bill, "nonsense"))
    assert width_pt < 240
