"""Closed months (app/finance/periods.py): once a GST return is marked filed,
nothing can be added with a date in that month or voided out of it - a
correction is a note dated in an open month. Unmarking the return reopens
the month."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from app.expenses import services as es
from app.finance import periods
from app.finance.filing import mark_filed
from app.inventory import ledger
from app.invoicing.services import (
    InvoiceInput, InvoiceValidationError, LineInput, create_invoice, void_invoice,
)
from app.models.customer import Customer
from app.models.expense import ExpenseCategory
from app.models.filing import ReturnType
from app.models.invoice_series import DocumentType
from app.models.purchase import Supplier
from app.models.tenant import Gstin
from app.notes.services import NoteInput, NoteValidationError, create_note, void_note
from app.purchases import services as ps

TODAY = date.today()
LAST_MONTH_END = TODAY.replace(day=1) - timedelta(days=1)
LAST_MONTH_DAY = LAST_MONTH_END.replace(day=10)
LAST_PERIOD = periods.period_of(LAST_MONTH_END)


def _line(rate="1000"):
    return LineInput(description="Catering", hsn_or_sac_code="996331", qty=D("1"), rate=D(rate),
                     discount_percent=D("0"), gst_rate=D("18"))


@pytest.fixture()
def buyer(db, tenant):
    c = Customer(tenant_id=tenant.id, name="Acme Pvt Ltd", gstin="29BBBBB0000B1Z5", state_code="29",
                 state_name="Karnataka")
    db.session.add(c)
    db.session.commit()
    return c


def _invoice(db, tenant, user, buyer, on, gstin=None):
    inv = create_invoice(tenant, user, InvoiceInput(
        gstin_id=(gstin or tenant.gstins.first()).id, document_type=DocumentType.TAX_INVOICE, customer_id=buyer.id,
        place_of_supply_state_code="29", invoice_date=on, notes=None, lines=[_line()]))
    db.session.commit()
    return inv


def _file(db, tenant, user, return_type, filed=True, gstin=None):
    mark_filed(tenant.id, (gstin or tenant.gstins.first()).id, LAST_PERIOD, return_type, user, filed, TODAY, "AA2701")
    db.session.commit()


# --- the sales side: GSTR-1 ---------------------------------------------------------------------------

def test_a_filed_month_takes_no_new_invoice_and_gives_none_up(db, tenant, client_admin, super_admin, buyer):
    old = _invoice(db, tenant, client_admin, buyer, LAST_MONTH_DAY)
    _file(db, tenant, super_admin, ReturnType.GSTR1)

    with pytest.raises(InvoiceValidationError) as back_dated:
        _invoice(db, tenant, client_admin, buyer, LAST_MONTH_DAY)
    db.session.rollback()
    text = back_dated.value.message
    assert f"{LAST_MONTH_END:%B %Y} is closed" in text and "GSTR-1 was marked filed" in text
    assert "credit or debit note" in text and "reopen the month" in text

    with pytest.raises(InvoiceValidationError, match="can't void an invoice"):
        void_invoice(old, client_admin, "Entered twice")
    db.session.rollback()

    # This month is open: a new invoice, and the correction - a credit note dated today.
    _invoice(db, tenant, client_admin, buyer, TODAY)
    note = create_note(tenant, client_admin, NoteInput(
        original_invoice_id=old.id, document_type=DocumentType.CREDIT_NOTE, note_date=TODAY,
        reason="Entered twice", lines=[_line()]))
    db.session.commit()
    assert note.note_date == TODAY


def test_notes_follow_the_same_rule(db, tenant, client_admin, super_admin, buyer):
    old = _invoice(db, tenant, client_admin, buyer, LAST_MONTH_DAY)
    note = create_note(tenant, client_admin, NoteInput(
        original_invoice_id=old.id, document_type=DocumentType.CREDIT_NOTE, note_date=LAST_MONTH_DAY,
        reason="Shortfall", lines=[_line("100")]))
    db.session.commit()
    _file(db, tenant, super_admin, ReturnType.GSTR1)
    with pytest.raises(NoteValidationError, match="is closed"):
        create_note(tenant, client_admin, NoteInput(
            original_invoice_id=old.id, document_type=DocumentType.DEBIT_NOTE, note_date=LAST_MONTH_DAY,
            reason="More", lines=[_line("50")]))
    db.session.rollback()
    with pytest.raises(NoteValidationError, match="can't void a note"):
        void_note(note, client_admin, "Mistake")


def test_unmarking_the_return_reopens_the_month(db, tenant, client_admin, super_admin, buyer):
    old = _invoice(db, tenant, client_admin, buyer, LAST_MONTH_DAY)
    _file(db, tenant, super_admin, ReturnType.GSTR1)
    assert periods.closed_by(tenant.id, tenant.gstins.first().id, LAST_MONTH_DAY, periods.SALES) is not None
    _file(db, tenant, super_admin, ReturnType.GSTR1, filed=False)
    assert periods.closed_by(tenant.id, tenant.gstins.first().id, LAST_MONTH_DAY, periods.SALES) is None
    void_invoice(old, client_admin, "Entered twice")
    _invoice(db, tenant, client_admin, buyer, LAST_MONTH_DAY)


def test_only_that_gstin_and_only_that_side_close(db, tenant, client_admin, super_admin, buyer):
    second = Gstin(tenant_id=tenant.id, gstin="29AAAAA0000A1Z5", state_code="29", state_name="Karnataka")
    db.session.add(second)
    db.session.commit()
    _file(db, tenant, super_admin, ReturnType.GSTR1)
    _invoice(db, tenant, client_admin, buyer, LAST_MONTH_DAY, gstin=second)      # the other registration is open
    # GSTR-1 closes sales only: a purchase dated last month still goes in.
    tenant.set_feature("purchases", True)
    supplier = Supplier(tenant_id=tenant.id, name="Sri Balaji", gstin="27BBBBB1111B1Z5", state_code="27",
                        state_name="Maharashtra")
    db.session.add(supplier)
    db.session.commit()
    _purchase(db, tenant, client_admin, supplier, LAST_MONTH_DAY)


# --- the buying side: GSTR-3B -------------------------------------------------------------------------

def _purchase(db, tenant, user, supplier, on, invoice_no="SB/101"):
    bill = ps.create_purchase_bill(tenant, user, ps.PurchaseInput(
        supplier_id=supplier.id, gstin_id=tenant.gstins.first().id, supplier_invoice_no=invoice_no,
        supplier_invoice_date=on, location_id=ledger.default_location(tenant.id).id, bill_date=on,
        lines=[ps.PurchaseLineInput(product_id=None, description="Packing", hsn_or_sac_code="4819", qty=D("10"),
                                    unit="PCS", rate=D("100"), gst_rate=D("18"))]))
    db.session.commit()
    return bill


def test_a_month_with_gstr3b_filed_takes_no_purchases_or_expenses(db, tenant, client_admin, super_admin):
    for flag in ("purchases", "expenses"):
        tenant.set_feature(flag, True)
    supplier = Supplier(tenant_id=tenant.id, name="Sri Balaji", gstin="27BBBBB1111B1Z5", state_code="27",
                        state_name="Maharashtra")
    db.session.add(supplier)
    db.session.commit()
    es.ensure_categories(tenant.id)
    rent = ExpenseCategory.query.filter_by(tenant_id=tenant.id, name="Rent").one()
    old_bill = _purchase(db, tenant, client_admin, supplier, LAST_MONTH_DAY)
    old_expense = es.create_expense(tenant, client_admin, es.ExpenseInput(
        expense_date=LAST_MONTH_DAY, category_id=rent.id, amount=D("1000"), payee_name="Landlord"))
    db.session.commit()

    _file(db, tenant, super_admin, ReturnType.GSTR3B)

    with pytest.raises(ps.PurchaseError) as refused:
        _purchase(db, tenant, client_admin, supplier, LAST_MONTH_DAY, invoice_no="SB/102")
    db.session.rollback()
    assert "GSTR-3B was marked filed" in refused.value.message and "open month" in refused.value.message
    with pytest.raises(ps.PurchaseError, match="can't void a purchase bill"):
        ps.void_purchase_bill(old_bill, client_admin, "Wrong supplier")
    db.session.rollback()
    with pytest.raises(es.ExpenseError, match="is closed"):
        es.create_expense(tenant, client_admin, es.ExpenseInput(
            expense_date=LAST_MONTH_DAY, category_id=rent.id, amount=D("500"), payee_name="Landlord"))
    db.session.rollback()
    if old_expense.gstin_id:
        with pytest.raises(es.ExpenseError, match="can't void an expense"):
            es.void_expense(old_expense, client_admin, "Twice")
        db.session.rollback()
    # Today's bill is fine.
    _purchase(db, tenant, client_admin, supplier, TODAY, invoice_no="SB/103")


# --- on the screen --------------------------------------------------------------------------------------

def test_the_invoice_form_says_why(client, db, tenant, client_admin, super_admin, buyer):
    old = _invoice(db, tenant, client_admin, buyer, LAST_MONTH_DAY)
    _file(db, tenant, super_admin, ReturnType.GSTR1)
    client.post("/auth/login", data={"email": "admin@acme.example.com", "password": "ClientSecret123"})
    resp = client.post(f"/invoices/{old.id}/void", data={"reason": "Entered twice"}, follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert "is closed" in page and "GSTR-1 was marked filed" in page
    db.session.refresh(old)
    assert old.status.value == "issued"
