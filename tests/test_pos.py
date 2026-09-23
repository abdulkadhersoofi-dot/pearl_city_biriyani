from datetime import date
from decimal import Decimal

import pytest

from app.models.pos_bill import POSBill, POSBillStatus
from app.models.tenant import RegistrationType
from app.pos.services import (
    CartInput,
    CartLineInput,
    POSValidationError,
    checkout,
    discard_held_bill,
    hold_cart,
    refund_bill,
    z_report,
)
from app.utils.financial_year import current_financial_year


def _cart(qty="2", rate="100", gst_rate="18"):
    return CartInput(
        lines=[
            CartLineInput(
                description="Chicken Biriyani",
                hsn_or_sac_code="996331",
                qty=Decimal(qty),
                rate=Decimal(rate),
                gst_rate=Decimal(gst_rate),
                unit="plate",
            )
        ]
    )


def test_checkout_computes_intra_state_split_and_allocates_number(db, tenant, client_admin):
    from app.models.pos_bill import PaymentMode

    bill = checkout(tenant, client_admin, _cart(), PaymentMode.CASH)
    db.session.commit()

    assert bill.status == POSBillStatus.COMPLETED
    assert bill.bill_number == f"POS/{current_financial_year()}/0001"
    assert bill.total_taxable_value == Decimal("200.00")
    assert bill.total_cgst == Decimal("18.00")
    assert bill.total_sgst == Decimal("18.00")
    assert bill.total_igst == Decimal("0.00")
    assert bill.grand_total == Decimal("236.00")


def test_checkout_sequential_numbering(db, tenant, client_admin):
    from app.models.pos_bill import PaymentMode

    first = checkout(tenant, client_admin, _cart(), PaymentMode.CASH)
    db.session.commit()
    second = checkout(tenant, client_admin, _cart(), PaymentMode.UPI)
    db.session.commit()

    first_seq = int(first.bill_number.rsplit("/", 1)[1])
    second_seq = int(second.bill_number.rsplit("/", 1)[1])
    assert second_seq == first_seq + 1


def test_composition_tenant_never_charges_gst_at_pos(db, tenant, client_admin):
    from app.models.pos_bill import PaymentMode

    tenant.registration_type = RegistrationType.COMPOSITION
    db.session.commit()

    bill = checkout(tenant, client_admin, _cart(), PaymentMode.CASH)
    db.session.commit()

    assert bill.total_cgst == Decimal("0.00")
    assert bill.total_sgst == Decimal("0.00")
    assert bill.grand_total == Decimal("200.00")


def test_hold_then_discard(db, tenant, client_admin):
    bill = hold_cart(tenant, client_admin, _cart(), "Table 4")
    db.session.commit()

    assert bill.status == POSBillStatus.HELD
    assert bill.bill_number is None
    assert bill.hold_label == "Table 4"

    discard_held_bill(bill)
    db.session.commit()
    assert db.session.get(POSBill, bill.id) is None


def test_cannot_discard_a_completed_bill(db, tenant, client_admin):
    from app.models.pos_bill import PaymentMode

    bill = checkout(tenant, client_admin, _cart(), PaymentMode.CASH)
    db.session.commit()

    with pytest.raises(POSValidationError):
        discard_held_bill(bill)


def test_refund_requires_reason_and_valid_amount(db, tenant, client_admin):
    from app.models.pos_bill import PaymentMode

    bill = checkout(tenant, client_admin, _cart(), PaymentMode.CARD)
    db.session.commit()

    with pytest.raises(POSValidationError, match="reason"):
        refund_bill(bill, client_admin, bill.grand_total, "")

    with pytest.raises(POSValidationError, match="Refund amount"):
        refund_bill(bill, client_admin, bill.grand_total * 2, "too much")

    refund_bill(bill, client_admin, bill.grand_total, "Customer changed mind")
    db.session.commit()
    assert bill.status == POSBillStatus.REFUNDED
    assert bill.refunded_amount == bill.grand_total


def test_z_report_splits_by_payment_mode_and_excludes_refunds(db, tenant, client_admin):
    from app.models.pos_bill import PaymentMode

    cash_bill = checkout(tenant, client_admin, _cart(qty="1", rate="100", gst_rate="18"), PaymentMode.CASH)
    upi_bill = checkout(tenant, client_admin, _cart(qty="1", rate="50", gst_rate="18"), PaymentMode.UPI)
    refunded_bill = checkout(tenant, client_admin, _cart(qty="1", rate="30", gst_rate="18"), PaymentMode.CASH)
    db.session.commit()

    refund_bill(refunded_bill, client_admin, refunded_bill.grand_total, "return")
    db.session.commit()

    report = z_report(tenant.id, date.today())

    assert report["bill_count"] == 2
    assert report["total_sales"] == cash_bill.grand_total + upi_bill.grand_total
    assert report["by_mode"][PaymentMode.CASH]["count"] == 1
    assert report["by_mode"][PaymentMode.UPI]["count"] == 1
    assert report["total_refunds"] == refunded_bill.refunded_amount


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def test_staff_can_reach_pos_terminal_unlike_invoicing(client, db, tenant, staff):
    _login(client, staff.email, "CashierSecret123")

    resp = client.get("/pos/")
    assert resp.status_code == 200

    resp = client.get("/invoices/")
    assert resp.status_code == 403


def test_pos_day_book_is_tenant_isolated(client, db, tenant, client_admin):
    from app.models.pos_bill import PaymentMode
    from app.models.tenant import Gstin, Tenant
    from app.models.user import User, UserRole

    other_tenant = Tenant(legal_name="Beta Corp", registration_type=RegistrationType.REGULAR)
    db.session.add(other_tenant)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other_tenant.id, gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka", is_primary=True))
    other_admin = User(tenant_id=other_tenant.id, name="Beta Admin", email="admin@beta.example.com", role=UserRole.CLIENT_ADMIN, must_change_password=False)
    other_admin.set_password("BetaSecret123")
    db.session.add(other_admin)
    db.session.commit()

    own_bill = checkout(tenant, client_admin, _cart(), PaymentMode.CASH)
    other_bill = checkout(other_tenant, other_admin, _cart(), PaymentMode.CASH)
    db.session.commit()

    # Numbering is scoped per tenant, so both bills legitimately share the
    # same "POS/<FY>/0001" number - isolation has to be checked by id (the
    # receipt link), not by that shared, tenant-scoped number string.
    assert own_bill.bill_number == other_bill.bill_number
    assert own_bill.id != other_bill.id

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/pos/bills")

    assert f"/pos/bills/{own_bill.id}/receipt".encode() in resp.data
    assert f"/pos/bills/{other_bill.id}/receipt".encode() not in resp.data
