"""Per-staff counter permissions, and the server's own word on POS cart
lines (catalog price/tax treatment, discount caps, foreign item ids)."""

from decimal import Decimal

import pytest

from app.models.audit_log import AuditLog
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus
from app.models.product import Product
from app.models.tenant import RegistrationType, Tenant
from app.models.user import User, UserRole
from app.pos.services import CartInput, CartLineInput, POSValidationError, checkout


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def dosa(db, tenant):
    p = Product(tenant_id=tenant.id, name="Masala Dosa", hsn_or_sac_code="2106", gst_rate=Decimal("5"), unit="NOS", sale_price=Decimal("105"), price_includes_tax=True)
    db.session.add(p)
    db.session.commit()
    return p


def _cart(product, rate=None, qty="1", disc="0", incl=False):
    return CartInput(lines=[CartLineInput(
        description=product.name, hsn_or_sac_code=product.hsn_or_sac_code, qty=Decimal(qty),
        rate=Decimal(rate if rate is not None else product.sale_price), gst_rate=product.gst_rate,
        product_id=product.id, unit=product.unit, discount_percent=Decimal(disc), price_includes_tax=incl,
    )])


def _post_checkout(client, product, rate=None, disc="0", incl="0"):
    return client.post(
        "/pos/checkout",
        data={
            "payment_mode": "cash",
            "line_description[]": product.name,
            "line_hsn[]": product.hsn_or_sac_code,
            "line_qty[]": "1",
            "line_rate[]": str(rate if rate is not None else product.sale_price),
            "line_gst_rate[]": str(product.gst_rate),
            "line_product_id[]": str(product.id),
            "line_unit[]": product.unit,
            "line_incl[]": incl,
            "line_discount[]": disc,
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )


def test_staff_defaults_reproduce_pre_existing_behaviour(staff):
    assert staff.can_refund and staff.can_view_reports
    assert not (staff.can_give_discount or staff.can_void or staff.can_edit_price_at_pos or staff.can_manage_stock)


def test_inclusive_item_bills_at_its_shelf_price(db, tenant, staff, dosa):
    bill = checkout(tenant, staff, _cart(dosa), PaymentMode.CASH)
    db.session.commit()
    line = bill.lines[0]
    assert line.price_includes_tax is True
    assert line.taxable_value == Decimal("100.00")
    assert line.cgst_amount + line.sgst_amount == Decimal("5.00")
    assert bill.grand_total == Decimal("105.00")


def test_tax_treatment_comes_from_the_item_not_the_terminal(client, db, tenant, staff, dosa):
    _login(client, staff.email, "CashierSecret123")
    resp = _post_checkout(client, dosa, incl="0")  # terminal claims "exclusive"
    assert resp.status_code == 200
    bill = POSBill.query.get(resp.get_json()["bill_id"])
    assert bill.grand_total == Decimal("105.00")


def test_staff_cannot_change_a_price_without_permission(client, db, tenant, staff, dosa):
    _login(client, staff.email, "CashierSecret123")
    resp = _post_checkout(client, dosa, rate="1")
    bill = POSBill.query.get(resp.get_json()["bill_id"])
    assert bill.lines[0].rate == Decimal("105.00")
    assert bill.grand_total == Decimal("105.00")


def test_staff_with_price_permission_can_change_it(client, db, tenant, staff, dosa):
    staff.can_edit_price_at_pos = True
    db.session.commit()
    _login(client, staff.email, "CashierSecret123")
    bill = POSBill.query.get(_post_checkout(client, dosa, rate="84").get_json()["bill_id"])
    assert bill.grand_total == Decimal("84.00")


def test_discount_needs_permission_and_respects_the_cap(db, tenant, staff, dosa):
    with pytest.raises(POSValidationError, match="isn't allowed to give discounts"):
        checkout(tenant, staff, _cart(dosa, disc="5"), PaymentMode.CASH)
    db.session.rollback()

    staff.can_give_discount = True
    staff.max_discount_percent = Decimal("10")
    db.session.commit()
    with pytest.raises(POSValidationError, match="at most 10%"):
        checkout(tenant, staff, _cart(dosa, disc="15"), PaymentMode.CASH)
    db.session.rollback()

    bill = checkout(tenant, staff, _cart(dosa, disc="10"), PaymentMode.CASH)
    assert bill.lines[0].line_total == Decimal("94.50")


def test_client_admin_can_always_discount(db, tenant, client_admin, dosa):
    bill = checkout(tenant, client_admin, _cart(dosa, disc="50"), PaymentMode.CASH)
    assert bill.lines[0].line_total == Decimal("52.50")


def test_an_item_from_another_client_is_refused(db, tenant, staff):
    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR)
    db.session.add(other)
    db.session.flush()
    foreign = Product(tenant_id=other.id, name="Theirs", hsn_or_sac_code="2106", gst_rate=Decimal("5"), sale_price=Decimal("10"))
    db.session.add(foreign)
    db.session.commit()
    with pytest.raises(POSValidationError, match="isn't in your catalog"):
        checkout(tenant, staff, _cart(foreign), PaymentMode.CASH)


def test_refund_and_reports_follow_permissions(client, db, tenant, staff, dosa):
    bill = checkout(tenant, staff, _cart(dosa), PaymentMode.CASH)
    db.session.commit()
    _login(client, staff.email, "CashierSecret123")
    assert client.get("/pos/bills").status_code == 200
    assert b"Day-book" in client.get("/pos/").data

    staff.can_refund = False
    staff.can_view_reports = False
    db.session.commit()
    assert client.get("/pos/bills").status_code == 403
    assert client.get("/pos/z-report").status_code == 403
    assert b"Day-book" not in client.get("/pos/").data
    resp = client.post(f"/pos/bills/{bill.id}/refund", data={"amount": "105", "reason": "x"})
    assert resp.status_code == 403
    db.session.refresh(bill)
    assert bill.status == POSBillStatus.COMPLETED


def test_client_admin_edits_branch_permissions(client, db, tenant, client_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")
    assert b"Permissions" in client.get("/branches/").data
    resp = client.post(
        f"/branches/{staff.id}/permissions",
        data={"can_give_discount": "y", "max_discount_percent": "12.5", "can_view_reports": "y"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(staff)
    assert staff.can_give_discount and staff.max_discount_percent == Decimal("12.50")
    assert staff.can_refund is False  # unticked
    assert AuditLog.query.filter_by(action="branch_permissions_changed", entity_id=staff.id).count() == 1


def test_switching_discounts_off_clears_the_limit(client, db, tenant, client_admin, staff):
    staff.can_give_discount = True
    staff.max_discount_percent = Decimal("20")
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    client.post(f"/branches/{staff.id}/permissions", data={"max_discount_percent": "20"})
    db.session.refresh(staff)
    assert staff.can_give_discount is False and staff.max_discount_percent == Decimal("0")


def test_discount_limit_over_100_is_refused(client, db, tenant, client_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")
    client.post(f"/branches/{staff.id}/permissions", data={"can_give_discount": "y", "max_discount_percent": "150"})
    db.session.refresh(staff)
    assert staff.can_give_discount is False


def test_cannot_edit_another_clients_branch_permissions(client, db, tenant, client_admin):
    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR)
    db.session.add(other)
    db.session.flush()
    foreign = User(tenant_id=other.id, name="Theirs", email="theirs@x.com", role=UserRole.STAFF, must_change_password=False)
    foreign.set_password("Whatever123")
    db.session.add(foreign)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get(f"/branches/{foreign.id}/permissions").status_code == 404
    client.post(f"/branches/{foreign.id}/permissions", data={"can_void": "y"})
    db.session.refresh(foreign)
    assert foreign.can_void is False


def test_staff_cannot_open_the_permissions_page(client, db, staff):
    _login(client, staff.email, "CashierSecret123")
    assert client.get(f"/branches/{staff.id}/permissions").status_code == 403


def test_composition_client_terminal_never_previews_gst(client, db, tenant, staff, dosa):
    tenant.registration_type = RegistrationType.COMPOSITION
    db.session.commit()
    _login(client, staff.email, "CashierSecret123")
    assert b"const CHARGES_GST = false;" in client.get("/pos/").data
