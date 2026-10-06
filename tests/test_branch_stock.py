from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.invoicing.services import InvoiceInput, InvoiceValidationError, LineInput, create_invoice
from app.models.invoice_series import DocumentType
from app.models.pos_bill import PaymentMode, POSBillStatus
from app.models.product import Product
from app.models.stock import BranchStockAllocation
from app.models.user import BranchType, User, UserRole
from app.notes.services import NoteValidationError, create_note, NoteInput
from app.pos.services import CartInput, CartLineInput, checkout
from app.pos.stock import allocate_stock, branch_stock_status, today_allocations


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def biriyani(db, tenant):
    product = Product(
        tenant_id=tenant.id,
        name="Chicken Biriyani",
        hsn_or_sac_code="996331",
        gst_rate=Decimal("18"),
        unit="plate",
        default_price=Decimal("100"),
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture()
def owned_branch(db, tenant):
    user = User(
        tenant_id=tenant.id, name="MG Road (owned)", email="mgroad@acme.example.com",
        role=UserRole.STAFF, branch_type=BranchType.OWNED, must_change_password=False,
    )
    user.set_password("BranchSecret123")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def third_party_branch(db, tenant):
    user = User(
        tenant_id=tenant.id, name="Indiranagar Reseller", email="indiranagar@acme.example.com",
        role=UserRole.STAFF, branch_type=BranchType.THIRD_PARTY, must_change_password=False,
    )
    user.set_password("BranchSecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _invoice_input(tenant, branch_user_id=None, customer_id=None, qty="10", document_type=DocumentType.TAX_INVOICE, product_id=None):
    gstin = tenant.gstins.first()
    return InvoiceInput(
        gstin_id=gstin.id,
        document_type=document_type,
        place_of_supply_state_code=gstin.state_code,
        invoice_date=date.today(),
        notes=None,
        customer_id=customer_id,
        branch_user_id=branch_user_id,
        lines=[
            LineInput(
                description="Chicken Biriyani",
                hsn_or_sac_code="996331",
                qty=Decimal(qty),
                rate=Decimal("100"),
                discount_percent=Decimal("0"),
                gst_rate=Decimal("18"),
                product_id=product_id,
            )
        ],
    )


def test_owned_branch_invoice_is_delivery_challan_with_no_gst(db, tenant, client_admin, owned_branch, biriyani):
    invoice = create_invoice(
        tenant, client_admin, _invoice_input(tenant, branch_user_id=owned_branch.id, qty="25", product_id=biriyani.id)
    )
    db.session.commit()

    assert invoice.document_type == DocumentType.DELIVERY_CHALLAN
    assert invoice.branch_user_id == owned_branch.id
    assert invoice.customer_id is None
    assert invoice.total_cgst == Decimal("0.00")
    assert invoice.total_sgst == Decimal("0.00")
    assert invoice.lines[0].gst_rate == Decimal("0")
    assert invoice.customer_snapshot["name"] == owned_branch.name


def test_third_party_branch_invoice_is_tax_invoice_with_gst(db, tenant, client_admin, third_party_branch, biriyani):
    invoice = create_invoice(
        tenant, client_admin, _invoice_input(tenant, branch_user_id=third_party_branch.id, qty="25", product_id=biriyani.id)
    )
    db.session.commit()

    assert invoice.document_type == DocumentType.TAX_INVOICE
    assert invoice.branch_user_id == third_party_branch.id
    assert invoice.customer_id is None
    # 25 * 100 = 2500 taxable, 18% GST intra-state split CGST/SGST
    assert invoice.total_taxable_value == Decimal("2500.00")
    assert invoice.total_cgst == Decimal("225.00")
    assert invoice.total_sgst == Decimal("225.00")


def test_branch_invoice_requested_document_type_is_ignored_and_overridden(db, tenant, client_admin, owned_branch, biriyani):
    # Even if a caller passes Tax Invoice, an owned branch always becomes
    # a Delivery Challan - this is never the operator's choice.
    invoice = create_invoice(
        tenant,
        client_admin,
        _invoice_input(tenant, branch_user_id=owned_branch.id, document_type=DocumentType.TAX_INVOICE, product_id=biriyani.id),
    )
    db.session.commit()
    assert invoice.document_type == DocumentType.DELIVERY_CHALLAN


def test_branch_invoice_transfers_stock_automatically(db, tenant, client_admin, owned_branch, biriyani):
    create_invoice(tenant, client_admin, _invoice_input(tenant, branch_user_id=owned_branch.id, qty="40", product_id=biriyani.id))
    db.session.commit()

    assert today_allocations(owned_branch.id) == {biriyani.id: Decimal("40")}

    # A second invoice the same day adds to, doesn't replace, the running total.
    create_invoice(tenant, client_admin, _invoice_input(tenant, branch_user_id=owned_branch.id, qty="10", product_id=biriyani.id))
    db.session.commit()
    assert today_allocations(owned_branch.id) == {biriyani.id: Decimal("50")}


def test_adhoc_line_with_no_product_is_not_tracked_as_stock(db, tenant, client_admin, owned_branch):
    invoice = create_invoice(tenant, client_admin, _invoice_input(tenant, branch_user_id=owned_branch.id, qty="5", product_id=None))
    db.session.commit()
    assert invoice.id is not None
    assert today_allocations(owned_branch.id) == {}


def test_create_invoice_requires_a_customer_or_a_branch(db, tenant, client_admin):
    with pytest.raises(InvoiceValidationError):
        create_invoice(tenant, client_admin, _invoice_input(tenant, branch_user_id=None, customer_id=None))


def test_create_invoice_rejects_a_branch_from_another_tenant(db, client_admin, tenant):
    from app.models.tenant import Gstin, RegistrationType, Tenant

    other_tenant = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR)
    db.session.add(other_tenant)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other_tenant.id, gstin="29CCCCC0000C1Z5", state_code="29", state_name="Karnataka", is_primary=True))
    foreign_branch = User(
        tenant_id=other_tenant.id, name="Foreign Branch", email="foreign@other.example.com",
        role=UserRole.STAFF, branch_type=BranchType.OWNED, must_change_password=False,
    )
    foreign_branch.set_password("Secret12345")
    db.session.add(foreign_branch)
    db.session.commit()

    with pytest.raises(InvoiceValidationError):
        create_invoice(tenant, client_admin, _invoice_input(tenant, branch_user_id=foreign_branch.id))


def test_allocation_dated_by_invoice_date_not_wall_clock(db, tenant, client_admin, owned_branch, biriyani):
    yesterday = date.today() - timedelta(days=1)
    allocate_stock(tenant.id, owned_branch.id, client_admin.id, [(biriyani.id, Decimal("100"))], on_date=yesterday)
    db.session.commit()

    assert today_allocations(owned_branch.id) == {}
    # Still listed (every active product always is), just at 0 - not
    # carried over from yesterday's allocation.
    status = branch_stock_status(tenant.id, owned_branch.id)
    assert len(status) == 1
    assert status[0].product_id == biriyani.id
    assert status[0].allocated == Decimal("0")


def test_branch_stock_status_remaining_can_go_negative(db, tenant, client_admin, owned_branch, biriyani):
    allocate_stock(tenant.id, owned_branch.id, client_admin.id, [(biriyani.id, Decimal("10"))])
    db.session.commit()

    cart = CartInput(
        lines=[
            CartLineInput(
                description="Chicken Biriyani", hsn_or_sac_code="996331", qty=Decimal("15"),
                rate=Decimal("100"), gst_rate=Decimal("18"), unit="plate", product_id=biriyani.id,
            )
        ]
    )
    # POS never blocks a sale, regardless of how little (or negative) stock remains.
    bill = checkout(tenant, owned_branch, cart, PaymentMode.CASH)
    db.session.commit()
    assert bill.status == POSBillStatus.COMPLETED

    status = branch_stock_status(tenant.id, owned_branch.id)
    assert status[0].allocated == Decimal("10")
    assert status[0].sold == Decimal("15")
    assert status[0].remaining == Decimal("-5")


def test_checkout_never_blocked_even_with_no_allocation_at_all(db, tenant, owned_branch, biriyani):
    cart = CartInput(
        lines=[
            CartLineInput(
                description="Chicken Biriyani", hsn_or_sac_code="996331", qty=Decimal("9999"),
                rate=Decimal("100"), gst_rate=Decimal("18"), unit="plate", product_id=biriyani.id,
            )
        ]
    )
    bill = checkout(tenant, owned_branch, cart, PaymentMode.CASH)
    db.session.commit()
    assert bill.status == POSBillStatus.COMPLETED


def test_credit_debit_note_is_blocked_against_a_branch_invoice(db, tenant, client_admin, owned_branch, biriyani):
    invoice = create_invoice(tenant, client_admin, _invoice_input(tenant, branch_user_id=owned_branch.id, qty="5", product_id=biriyani.id))
    db.session.commit()

    with pytest.raises(NoteValidationError):
        create_note(
            tenant,
            client_admin,
            NoteInput(
                original_invoice_id=invoice.id,
                document_type=DocumentType.CREDIT_NOTE,
                note_date=date.today(),
                reason="test",
                lines=[
                    LineInput(
                        description="Chicken Biriyani", hsn_or_sac_code="996331", qty=Decimal("1"),
                        rate=Decimal("100"), discount_percent=Decimal("0"), gst_rate=Decimal("18"),
                    )
                ],
            ),
        )


def test_branches_blueprint_stock_page_is_view_only(client, db, client_admin, owned_branch):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get(f"/branches/{owned_branch.id}/stock")
    assert resp.status_code == 200

    resp = client.post(f"/branches/{owned_branch.id}/stock", data={})
    assert resp.status_code == 405


def test_new_branch_requires_a_branch_type(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        "/branches/new",
        data={"name": "No Type Branch", "email": "notype@example.com", "password": "Secret12345", "confirm_password": "Secret12345"},
    )
    assert resp.status_code == 200
    assert User.query.filter_by(email="notype@example.com").first() is None


def test_invoicing_form_bills_owned_branch_as_delivery_challan_end_to_end(client, db, client_admin, tenant, owned_branch, biriyani):
    _login(client, client_admin.email, "ClientSecret123")
    gstin = tenant.gstins.first()
    resp = client.post(
        "/invoices/new",
        data={
            "gstin_id": str(gstin.id),
            "document_type": "tax_invoice",
            "branch_user_id": str(owned_branch.id),
            "customer_id": "",
            "place_of_supply_state_code": gstin.state_code,
            "invoice_date": date.today().isoformat(),
            "notes": "",
            "line_description[]": "Chicken Biriyani",
            "line_hsn[]": "996331",
            "line_qty[]": "30",
            "line_rate[]": "100",
            "line_discount[]": "0",
            "line_gst_rate[]": "18",
            "line_product_id[]": str(biriyani.id),
            "line_unit[]": "plate",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"created" in resp.data.lower()

    from app.models.invoice import Invoice

    invoice = Invoice.query.filter_by(tenant_id=tenant.id, branch_user_id=owned_branch.id).first()
    assert invoice is not None
    assert invoice.document_type == DocumentType.DELIVERY_CHALLAN
    assert invoice.total_cgst == Decimal("0.00")
    assert BranchStockAllocation.query.filter_by(branch_user_id=owned_branch.id).count() == 1
    assert today_allocations(owned_branch.id) == {biriyani.id: Decimal("30")}


def test_branch_stock_status_lists_every_active_product_even_unallocated(db, tenant, client_admin, owned_branch, biriyani):
    # Soda has never been sent to this branch - it should still show up,
    # at 0 sent / 0 remaining, not be silently missing.
    soda = Product(tenant_id=tenant.id, name="Soda", hsn_or_sac_code="22021010", unit="bottle", default_price=Decimal("20"))
    inactive = Product(
        tenant_id=tenant.id, name="Discontinued Item", hsn_or_sac_code="12345678", unit="pcs",
        default_price=Decimal("10"), is_active=False,
    )
    db.session.add_all([soda, inactive])
    allocate_stock(tenant.id, owned_branch.id, client_admin.id, [(biriyani.id, Decimal("50"))])
    db.session.commit()

    status = branch_stock_status(tenant.id, owned_branch.id)
    names = {s.product_name for s in status}
    assert names == {"Chicken Biriyani", "Soda"}  # inactive product excluded

    soda_status = next(s for s in status if s.product_name == "Soda")
    assert soda_status.allocated == Decimal("0")
    assert soda_status.sold == Decimal("0")
    assert soda_status.remaining == Decimal("0")

    biriyani_status = next(s for s in status if s.product_name == "Chicken Biriyani")
    assert biriyani_status.allocated == Decimal("50")


def test_checkout_json_response_includes_refreshed_stock_for_a_branch(client, db, tenant, client_admin, owned_branch, biriyani):
    allocate_stock(tenant.id, owned_branch.id, client_admin.id, [(biriyani.id, Decimal("100"))])
    db.session.commit()

    _login(client, owned_branch.email, "BranchSecret123")
    resp = client.post(
        "/pos/checkout",
        data={
            "payment_mode": "cash",
            "line_description[]": "Chicken Biriyani",
            "line_hsn[]": "996331",
            "line_qty[]": "10",
            "line_rate[]": "100",
            "line_gst_rate[]": "18",
            "line_product_id[]": str(biriyani.id),
            "line_unit[]": "plate",
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert "stock_status" in payload
    biriyani_entry = next(s for s in payload["stock_status"] if s["product_id"] == biriyani.id)
    assert biriyani_entry["allocated"] == 100.0
    assert biriyani_entry["sold"] == 10.0


def test_checkout_json_response_has_no_stock_status_for_client_admin(client, db, tenant, client_admin, biriyani):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        "/pos/checkout",
        data={
            "payment_mode": "cash",
            "line_description[]": "Chicken Biriyani",
            "line_hsn[]": "996331",
            "line_qty[]": "1",
            "line_rate[]": "100",
            "line_gst_rate[]": "18",
            "line_product_id[]": str(biriyani.id),
            "line_unit[]": "plate",
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    assert "stock_status" not in resp.get_json()


def test_branch_login_header_has_no_nav_but_still_has_sign_out(client, db, owned_branch):
    _login(client, owned_branch.email, "BranchSecret123")
    resp = client.get("/pos/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "<nav" not in body
    assert "Sign out" in body
