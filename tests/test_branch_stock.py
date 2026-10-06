from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.pos_bill import PaymentMode, POSBillStatus
from app.models.product import Product
from app.models.stock import BranchStockAllocation
from app.pos.services import CartInput, CartLineInput, POSValidationError, checkout
from app.pos.stock import (
    StockAllocationError,
    allocate_stock,
    branch_stock_status,
    check_cart_against_stock,
    parse_allocation_lines,
    today_allocations,
    today_sold,
)


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


def _cart(product_id, qty):
    return CartInput(
        lines=[
            CartLineInput(
                description="Chicken Biriyani",
                hsn_or_sac_code="996331",
                qty=Decimal(qty),
                rate=Decimal("100"),
                gst_rate=Decimal("18"),
                unit="plate",
                product_id=product_id,
            )
        ]
    )


def test_allocate_stock_is_additive_across_calls(db, tenant, client_admin, staff, biriyani):
    allocate_stock(tenant.id, staff.id, client_admin.id, [(biriyani.id, Decimal("60"))])
    db.session.commit()
    allocate_stock(tenant.id, staff.id, client_admin.id, [(biriyani.id, Decimal("40"))])
    db.session.commit()

    totals = today_allocations(staff.id)
    assert totals[biriyani.id] == Decimal("100")


def test_allocation_from_yesterday_does_not_count_today(db, tenant, client_admin, staff, biriyani):
    yesterday = date.today() - timedelta(days=1)
    allocate_stock(tenant.id, staff.id, client_admin.id, [(biriyani.id, Decimal("100"))], on_date=yesterday)
    db.session.commit()

    assert today_allocations(staff.id) == {}
    status = branch_stock_status(staff.id, grace_qty=10)
    assert status == []


def test_branch_stock_status_remaining_floors_at_zero(db, tenant, client_admin, staff, biriyani):
    allocate_stock(tenant.id, staff.id, client_admin.id, [(biriyani.id, Decimal("100"))])
    db.session.commit()
    checkout(tenant, staff, _cart(biriyani.id, "100"), PaymentMode.CASH)
    db.session.commit()
    checkout(tenant, staff, _cart(biriyani.id, "5"), PaymentMode.CASH)
    db.session.commit()

    status = branch_stock_status(staff.id, grace_qty=10)
    assert len(status) == 1
    assert status[0].allocated == Decimal("100")
    assert status[0].sold == Decimal("105")
    assert status[0].remaining == Decimal("0")
    assert status[0].available_with_grace == Decimal("5")


def test_checkout_blocked_once_allocated_plus_grace_is_exceeded(db, tenant, client_admin, staff, biriyani):
    allocate_stock(tenant.id, staff.id, client_admin.id, [(biriyani.id, Decimal("10"))])
    db.session.commit()

    # 10 allocated + 10 grace (default) = 20 available; 21 should be blocked.
    with pytest.raises(POSValidationError):
        checkout(tenant, staff, _cart(biriyani.id, "21"), PaymentMode.CASH)


def test_checkout_succeeds_within_grace_buffer(db, tenant, client_admin, staff, biriyani):
    allocate_stock(tenant.id, staff.id, client_admin.id, [(biriyani.id, Decimal("10"))])
    db.session.commit()

    bill = checkout(tenant, staff, _cart(biriyani.id, "18"), PaymentMode.CASH)
    db.session.commit()
    assert bill.status == POSBillStatus.COMPLETED


def test_checkout_respects_tenant_grace_qty_setting(db, tenant, client_admin, staff, biriyani):
    tenant.stock_grace_qty = 2
    db.session.commit()
    allocate_stock(tenant.id, staff.id, client_admin.id, [(biriyani.id, Decimal("10"))])
    db.session.commit()

    with pytest.raises(POSValidationError):
        checkout(tenant, staff, _cart(biriyani.id, "13"), PaymentMode.CASH)

    bill = checkout(tenant, staff, _cart(biriyani.id, "12"), PaymentMode.CASH)
    db.session.commit()
    assert bill.status == POSBillStatus.COMPLETED


def test_client_admin_checkout_is_never_limited_by_stock(db, tenant, client_admin, staff, biriyani):
    allocate_stock(tenant.id, staff.id, client_admin.id, [(biriyani.id, Decimal("1"))])
    db.session.commit()

    # The kitchen's own login sells itself the stock it made - no limit.
    bill = checkout(tenant, client_admin, _cart(biriyani.id, "500"), PaymentMode.CASH)
    db.session.commit()
    assert bill.status == POSBillStatus.COMPLETED


def test_unallocated_product_sells_with_no_limit(db, tenant, staff):
    product = Product(
        tenant_id=tenant.id, name="Soda", hsn_or_sac_code="22021010", unit="bottle", default_price=Decimal("20")
    )
    db.session.add(product)
    db.session.commit()

    bill = checkout(tenant, staff, _cart(product.id, "1000"), PaymentMode.CASH)
    db.session.commit()
    assert bill.status == POSBillStatus.COMPLETED


def test_parse_allocation_lines_rejects_bad_input():
    with pytest.raises(StockAllocationError):
        parse_allocation_lines([""], [""])
    with pytest.raises(StockAllocationError):
        parse_allocation_lines(["1"], ["0"])
    assert parse_allocation_lines(["1", ""], ["5", ""]) == [(1, Decimal("5"))]


def test_branches_blueprint_rename_removed_old_staff_routes(client, db, client_admin, staff):
    client.post(
        "/auth/login", data={"email": client_admin.email, "password": "ClientSecret123"}, follow_redirects=True
    )
    resp = client.get("/branches/")
    assert resp.status_code == 200
    assert b"Branches" in resp.data

    resp = client.get("/staff/")
    assert resp.status_code == 404


def test_client_admin_sends_stock_and_branch_sees_it_on_pos(client, db, client_admin, staff, biriyani):
    client.post(
        "/auth/login", data={"email": client_admin.email, "password": "ClientSecret123"}, follow_redirects=True
    )
    resp = client.post(
        f"/branches/{staff.id}/stock",
        data={"product_id[]": [str(biriyani.id)], "qty[]": ["50"]},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert BranchStockAllocation.query.filter_by(branch_user_id=staff.id).count() == 1

    client.get("/auth/logout")
    client.post("/auth/login", data={"email": staff.email, "password": "CashierSecret123"}, follow_redirects=True)
    resp = client.get("/pos/")
    assert resp.status_code == 200
    assert b"Chicken Biriyani" in resp.data
