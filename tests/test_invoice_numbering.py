from app.models.invoice_series import DocumentType
from app.utils.numbering import allocate_number, get_or_create_series


def test_allocate_number_increments_and_formats(db, tenant):
    gstin = tenant.gstins.first()
    series = get_or_create_series(tenant.id, gstin.id, DocumentType.TAX_INVOICE, "2025-26")
    db.session.commit()

    first = allocate_number(series)
    db.session.commit()
    second = allocate_number(series)
    db.session.commit()

    assert first == "INV/2025-26/0001"
    assert second == "INV/2025-26/0002"


def test_get_or_create_series_is_idempotent_per_key(db, tenant):
    gstin = tenant.gstins.first()
    series_a = get_or_create_series(tenant.id, gstin.id, DocumentType.TAX_INVOICE, "2025-26")
    db.session.commit()
    series_b = get_or_create_series(tenant.id, gstin.id, DocumentType.TAX_INVOICE, "2025-26")

    assert series_a.id == series_b.id


def test_different_document_types_get_independent_series(db, tenant):
    gstin = tenant.gstins.first()
    tax_series = get_or_create_series(tenant.id, gstin.id, DocumentType.TAX_INVOICE, "2025-26")
    bos_series = get_or_create_series(tenant.id, gstin.id, DocumentType.BILL_OF_SUPPLY, "2025-26")
    db.session.commit()

    tax_number = allocate_number(tax_series)
    bos_number = allocate_number(bos_series)
    db.session.commit()

    assert tax_number == "INV/2025-26/0001"
    assert bos_number == "BOS/2025-26/0001"
