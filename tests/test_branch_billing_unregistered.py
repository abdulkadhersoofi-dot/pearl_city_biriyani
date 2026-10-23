"""Stock sent to a branch from Invoices, for every kind of GST
registration. A branch the business owns gets a Delivery Challan (no GST)
whatever the registration. A third-party branch gets a Tax Invoice from a
regular business, and a Bill of Supply (no GST) from a composition or
unregistered one - which may not issue Tax Invoices."""

from datetime import date
from decimal import Decimal

import pytest

from app.models.invoice import Invoice
from app.models.invoice_series import DocumentType
from app.models.product import Product
from app.models.tenant import RegistrationType
from app.models.user import BranchType, User, UserRole


def _branch(db, tenant, name, kind):
    user = User(tenant_id=tenant.id, name=name, email=f"{name.lower()}@acme.example.com", role=UserRole.STAFF,
                branch_type=kind, must_change_password=False)
    user.set_password("BranchSecret123")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def biriyani(db, tenant):
    p = Product(tenant_id=tenant.id, name="Chicken Biriyani", hsn_or_sac_code="996331", gst_rate=Decimal("5"),
                unit="plate", sale_price=Decimal("100"))
    db.session.add(p)
    db.session.commit()
    return p


def _register(db, tenant, kind):
    tenant.registration_type = kind
    if kind == RegistrationType.UNREGISTERED:
        tenant.gstins.first().gstin = None
    db.session.commit()


def _send(client, tenant, branch, product):
    gstin = tenant.gstins.first()
    return client.post("/invoices/new", data={
        "gstin_id": str(gstin.id), "document_type": "tax_invoice", "branch_user_id": str(branch.id), "customer_id": "",
        "place_of_supply_state_code": gstin.state_code, "invoice_date": date.today().isoformat(), "notes": "",
        "line_description[]": product.name, "line_hsn[]": "996331", "line_qty[]": "10", "line_rate[]": "100",
        "line_discount[]": "0", "line_gst_rate[]": "5", "line_product_id[]": str(product.id), "line_unit[]": "plate",
    }, follow_redirects=True)


@pytest.mark.parametrize("registration, kind, expected, gst", [
    (RegistrationType.UNREGISTERED, BranchType.THIRD_PARTY, DocumentType.BILL_OF_SUPPLY, False),
    (RegistrationType.UNREGISTERED, BranchType.OWNED, DocumentType.DELIVERY_CHALLAN, False),
    (RegistrationType.COMPOSITION, BranchType.THIRD_PARTY, DocumentType.BILL_OF_SUPPLY, False),
    (RegistrationType.COMPOSITION, BranchType.OWNED, DocumentType.DELIVERY_CHALLAN, False),
    (RegistrationType.REGULAR, BranchType.THIRD_PARTY, DocumentType.TAX_INVOICE, True),
    (RegistrationType.REGULAR, BranchType.OWNED, DocumentType.DELIVERY_CHALLAN, False),
])
def test_stock_to_a_branch_for_each_registration(client, db, tenant, client_admin, biriyani, registration, kind,
                                                 expected, gst):
    _register(db, tenant, registration)
    branch = _branch(db, tenant, "Dongle", kind)
    client.post("/auth/login", data={"email": client_admin.email, "password": "ClientSecret123"})
    res = _send(client, tenant, branch, biriyani)
    assert res.status_code == 200 and "cannot issue this document type" not in res.get_data(as_text=True)
    invoice = Invoice.query.filter_by(tenant_id=tenant.id, branch_user_id=branch.id).one()
    assert invoice.document_type == expected
    assert (invoice.total_cgst + invoice.total_sgst + invoice.total_igst > 0) is gst
    assert invoice.grand_total == (Decimal("1050.00") if gst else Decimal("1000.00"))
    # The stock reaches the branch's counter either way.
    from app.pos.stock import today_allocations

    assert today_allocations(branch.id) == {biriyani.id: Decimal("10")}


def test_the_form_says_what_an_unregistered_business_issues(client, db, tenant, client_admin):
    _register(db, tenant, RegistrationType.UNREGISTERED)
    client.post("/auth/login", data={"email": client_admin.email, "password": "ClientSecret123"})
    html = client.get("/invoices/new").get_data(as_text=True)
    assert "const SELLER_REGISTERED = false;" in html and "Bill of Supply - no GST" in html
    assert ">Billing from<" in html and "Billing from (your GSTIN)" not in html
