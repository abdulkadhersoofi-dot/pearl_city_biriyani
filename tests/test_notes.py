from datetime import date
from decimal import Decimal

import pytest

from app.invoicing.services import InvoiceInput, LineInput, create_invoice
from app.models.customer import Customer
from app.models.invoice import InvoiceStatus
from app.models.invoice_series import DocumentType
from app.models.note import NoteStatus
from app.models.tenant import RegistrationType
from app.notes.services import NoteInput, NoteValidationError, create_note, void_note


def _make_invoice(db, tenant, client_admin, qty="4", rate="150", gst_rate="5"):
    customer = Customer(tenant_id=tenant.id, name="Walk-in", state_code="27", state_name="Maharashtra")
    db.session.add(customer)
    db.session.flush()

    invoice = create_invoice(
        tenant,
        client_admin,
        InvoiceInput(
            gstin_id=tenant.gstins.first().id,
            document_type=DocumentType.TAX_INVOICE,
            customer_id=customer.id,
            place_of_supply_state_code="27",
            invoice_date=date(2026, 10, 5),
            notes=None,
            lines=[
                LineInput(
                    description="Chicken Biriyani",
                    hsn_or_sac_code="996331",
                    qty=Decimal(qty),
                    rate=Decimal(rate),
                    discount_percent=Decimal("0"),
                    gst_rate=Decimal(gst_rate),
                    unit="plate",
                )
            ],
        ),
    )
    db.session.commit()
    return invoice


def _note_line(qty="4", rate="150", gst_rate="5"):
    return LineInput(
        description="Chicken Biriyani",
        hsn_or_sac_code="996331",
        qty=Decimal(qty),
        rate=Decimal(rate),
        discount_percent=Decimal("0"),
        gst_rate=Decimal(gst_rate),
        unit="plate",
    )


def test_full_credit_note_matches_the_original_invoice(db, tenant, client_admin):
    invoice = _make_invoice(db, tenant, client_admin)

    note = create_note(
        tenant,
        client_admin,
        NoteInput(
            original_invoice_id=invoice.id,
            document_type=DocumentType.CREDIT_NOTE,
            note_date=date(2026, 10, 6),
            reason="Full order returned - wrong items delivered",
            lines=[_note_line()],
        ),
    )
    db.session.commit()

    assert note.note_number.startswith("CN/")
    assert note.grand_total == invoice.grand_total
    assert note.total_cgst == invoice.total_cgst
    assert note.total_sgst == invoice.total_sgst
    assert note.original_invoice_id == invoice.id
    assert note.customer_snapshot == invoice.customer_snapshot


def test_partial_credit_note_only_covers_the_returned_quantity(db, tenant, client_admin):
    invoice = _make_invoice(db, tenant, client_admin, qty="4")

    note = create_note(
        tenant,
        client_admin,
        NoteInput(
            original_invoice_id=invoice.id,
            document_type=DocumentType.CREDIT_NOTE,
            note_date=date(2026, 10, 6),
            reason="1 of 4 plates returned - overcooked",
            lines=[_note_line(qty="1")],
        ),
    )
    db.session.commit()

    # 1 plate @ Rs.150, 5% GST => taxable 150, tax 7.50, split 3.75/3.75
    assert note.total_taxable_value == Decimal("150.00")
    assert note.total_cgst == Decimal("3.75")
    assert note.total_sgst == Decimal("3.75")
    assert note.grand_total == Decimal("158.00")
    assert note.grand_total < invoice.grand_total


def test_debit_note_gets_its_own_numbering_series(db, tenant, client_admin):
    invoice = _make_invoice(db, tenant, client_admin)

    note = create_note(
        tenant,
        client_admin,
        NoteInput(
            original_invoice_id=invoice.id,
            document_type=DocumentType.DEBIT_NOTE,
            note_date=date(2026, 10, 6),
            reason="Price under-billed - correcting rate",
            lines=[_note_line(qty="1", rate="20")],
        ),
    )
    db.session.commit()

    assert note.note_number.startswith("DN/")
    assert note.document_type == DocumentType.DEBIT_NOTE


def test_unregistered_tenant_note_never_charges_gst(db, tenant, client_admin):
    invoice = _make_invoice(db, tenant, client_admin)
    tenant.registration_type = RegistrationType.UNREGISTERED
    db.session.commit()

    note = create_note(
        tenant,
        client_admin,
        NoteInput(
            original_invoice_id=invoice.id,
            document_type=DocumentType.CREDIT_NOTE,
            note_date=date(2026, 10, 6),
            reason="Return",
            lines=[_note_line(qty="1")],
        ),
    )
    db.session.commit()

    assert note.total_cgst == Decimal("0.00")
    assert note.total_sgst == Decimal("0.00")
    assert note.grand_total == Decimal("150.00")


def test_cannot_issue_a_note_against_a_voided_invoice(db, tenant, client_admin):
    from app.invoicing.services import void_invoice

    invoice = _make_invoice(db, tenant, client_admin)
    void_invoice(invoice, client_admin, "Cancelled by customer")
    db.session.commit()
    assert invoice.status == InvoiceStatus.VOID

    with pytest.raises(NoteValidationError, match="voided invoice"):
        create_note(
            tenant,
            client_admin,
            NoteInput(
                original_invoice_id=invoice.id,
                document_type=DocumentType.CREDIT_NOTE,
                note_date=date(2026, 10, 6),
                reason="Return",
                lines=[_note_line(qty="1")],
            ),
        )


def test_note_requires_a_reason(db, tenant, client_admin):
    invoice = _make_invoice(db, tenant, client_admin)

    with pytest.raises(NoteValidationError, match="reason"):
        create_note(
            tenant,
            client_admin,
            NoteInput(
                original_invoice_id=invoice.id,
                document_type=DocumentType.CREDIT_NOTE,
                note_date=date(2026, 10, 6),
                reason="   ",
                lines=[_note_line(qty="1")],
            ),
        )


def test_void_note_requires_reason_and_is_not_repeatable(db, tenant, client_admin):
    invoice = _make_invoice(db, tenant, client_admin)
    note = create_note(
        tenant,
        client_admin,
        NoteInput(
            original_invoice_id=invoice.id,
            document_type=DocumentType.CREDIT_NOTE,
            note_date=date(2026, 10, 6),
            reason="Return",
            lines=[_note_line(qty="1")],
        ),
    )
    db.session.commit()

    with pytest.raises(NoteValidationError, match="reason"):
        void_note(note, client_admin, "")

    void_note(note, client_admin, "Entered by mistake")
    db.session.commit()
    assert note.status == NoteStatus.VOID

    with pytest.raises(NoteValidationError, match="already void"):
        void_note(note, client_admin, "again")


def test_client_admin_cannot_create_a_note_against_another_tenants_invoice(client, db, client_admin, tenant):
    from app.models.tenant import Gstin, RegistrationType, Tenant
    from app.models.user import User, UserRole

    other_tenant = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR)
    db.session.add(other_tenant)
    db.session.flush()
    db.session.add(
        Gstin(tenant_id=other_tenant.id, gstin="29CCCCC0000C1Z5", state_code="29", state_name="Karnataka", is_primary=True)
    )
    other_admin = User(
        tenant_id=other_tenant.id, name="Other Admin", email="otheradmin@example.com",
        role=UserRole.CLIENT_ADMIN, must_change_password=False,
    )
    other_admin.set_password("OtherSecret123")
    db.session.add(other_admin)
    db.session.commit()

    other_invoice = _make_invoice(db, other_tenant, other_admin)

    client.post("/auth/login", data={"email": client_admin.email, "password": "ClientSecret123"}, follow_redirects=True)
    resp = client.get(f"/notes/new?invoice_id={other_invoice.id}&type=credit")
    assert resp.status_code == 404
