"""Tax-inclusive lines through every document type - invoice (via the real
form route), credit note, POS bill - and into GSTR-1, which must report
exactly the taxable value / tax the documents stored."""

from datetime import date
from decimal import Decimal

from app.invoicing.services import InvoiceInput, LineInput, create_invoice
from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.invoice_series import DocumentType
from app.models.pos_bill import PaymentMode
from app.models.product import Product
from app.notes.services import NoteInput, create_note
from app.pos.services import CartInput, CartLineInput, checkout
from app.reports.gstr import gstr1_data


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _customer(db, tenant):
    c = Customer(tenant_id=tenant.id, name="Ravi", state_code="27", state_name="Maharashtra")
    db.session.add(c)
    db.session.commit()
    return c


def test_invoice_form_posts_an_inclusive_line(client, db, tenant, client_admin):
    customer = _customer(db, tenant)
    gstin = tenant.gstins.first()
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        "/invoices/new",
        data={
            "gstin_id": str(gstin.id),
            "document_type": "tax_invoice",
            "customer_id": str(customer.id),
            "place_of_supply_state_code": gstin.state_code,
            "invoice_date": "2026-10-05",
            "notes": "",
            "line_description[]": ["Thali", "Lassi"],
            "line_hsn[]": ["996331", "0403"],
            "line_qty[]": ["2", "1"],
            "line_rate[]": ["236", "100"],
            "line_discount[]": ["0", "0"],
            "line_gst_rate[]": ["18", "18"],
            "line_product_id[]": ["", ""],
            "line_unit[]": ["NOS", "NOS"],
            "line_incl[]": ["1", "0"],
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    inv = Invoice.query.filter_by(tenant_id=tenant.id).one()
    thali, lassi = inv.lines
    assert thali.price_includes_tax is True and lassi.price_includes_tax is False
    assert thali.taxable_value == Decimal("400.00") and thali.line_total == Decimal("472.00")
    assert lassi.taxable_value == Decimal("100.00") and lassi.line_total == Decimal("118.00")
    assert inv.grand_total == Decimal("590.00")
    # The detail page marks the inclusive rate so it isn't misread.
    assert b"(incl. GST)" in client.get(f"/invoices/{inv.id}").data


def test_old_forms_without_the_flag_stay_exclusive(client, db, tenant, client_admin):
    customer = _customer(db, tenant)
    gstin = tenant.gstins.first()
    _login(client, client_admin.email, "ClientSecret123")
    client.post(
        "/invoices/new",
        data={
            "gstin_id": str(gstin.id), "document_type": "tax_invoice", "customer_id": str(customer.id),
            "place_of_supply_state_code": gstin.state_code, "invoice_date": "2026-10-05", "notes": "",
            "line_description[]": "Thali", "line_hsn[]": "996331", "line_qty[]": "1", "line_rate[]": "100",
            "line_discount[]": "0", "line_gst_rate[]": "18", "line_product_id[]": "", "line_unit[]": "NOS",
        },
    )
    inv = Invoice.query.filter_by(tenant_id=tenant.id).one()
    assert inv.lines[0].price_includes_tax is False
    assert inv.grand_total == Decimal("118.00")


def test_credit_note_on_an_inclusive_line_credits_the_same_amounts(db, tenant, client_admin):
    customer = _customer(db, tenant)
    line = LineInput(description="Thali", hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("100"), discount_percent=Decimal("0"), gst_rate=Decimal("18"), unit="NOS", price_includes_tax=True)
    inv = create_invoice(tenant, client_admin, InvoiceInput(
        gstin_id=tenant.gstins.first().id, document_type=DocumentType.TAX_INVOICE, customer_id=customer.id,
        place_of_supply_state_code="27", invoice_date=date(2026, 10, 5), notes=None, lines=[line],
    ))
    db.session.flush()
    note = create_note(tenant, client_admin, NoteInput(
        original_invoice_id=inv.id, document_type=DocumentType.CREDIT_NOTE, note_date=date(2026, 10, 6),
        reason="Returned", lines=[line],
    ))
    db.session.commit()
    nl, il = note.lines[0], inv.lines[0]
    assert nl.price_includes_tax is True
    assert (nl.taxable_value, nl.cgst_amount, nl.sgst_amount, nl.line_total) == (
        il.taxable_value, il.cgst_amount, il.sgst_amount, il.line_total
    )
    assert note.grand_total == inv.grand_total == Decimal("100.00")


def test_gstr1_reports_exactly_what_the_documents_stored(db, tenant, client_admin, staff):
    product = Product(tenant_id=tenant.id, name="Biriyani", hsn_or_sac_code="996331", gst_rate=Decimal("5"), unit="NOS", sale_price=Decimal("150"), price_includes_tax=True)
    db.session.add(product)
    db.session.commit()
    bill = checkout(tenant, staff, CartInput(lines=[CartLineInput(
        description="Biriyani", hsn_or_sac_code="996331", qty=Decimal("3"), rate=Decimal("150"),
        gst_rate=Decimal("5"), product_id=product.id, unit="NOS",
    )]), PaymentMode.CASH)
    db.session.commit()
    assert bill.grand_total == Decimal("450.00")

    report = gstr1_data(tenant, tenant.gstins.first(), date.today().strftime("%Y-%m"))
    bucket = next(h for h in report["hsn_summary"] if h.hsn == "996331")
    line = bill.lines[0]
    assert bucket.taxable_value == line.taxable_value == Decimal("428.57")
    assert bucket.cgst + bucket.sgst == line.cgst_amount + line.sgst_amount == Decimal("21.43")
    assert bucket.total == Decimal("450.00")
