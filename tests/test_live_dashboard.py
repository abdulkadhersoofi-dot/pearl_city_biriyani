from decimal import Decimal

from app.live.metrics import branch_leaderboard, item_sales_today
from app.models.pos_bill import PaymentMode
from app.models.product import Product
from app.models.user import User, UserRole
from app.pos.services import CartInput, CartLineInput, checkout


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _branch(db, tenant, name, email):
    user = User(tenant_id=tenant.id, name=name, email=email, role=UserRole.STAFF, must_change_password=False)
    user.set_password("BranchSecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _product(db, tenant, name="Chicken Biriyani", price="150"):
    product = Product(
        tenant_id=tenant.id, name=name, hsn_or_sac_code="996331", gst_rate=Decimal("18"), unit="plate",
        default_price=Decimal(price),
    )
    db.session.add(product)
    db.session.commit()
    return product


def _sell(tenant, branch, product, qty):
    checkout(
        tenant, branch,
        CartInput(lines=[CartLineInput(description=product.name, hsn_or_sac_code="996331", qty=Decimal(qty), rate=product.default_price, gst_rate=Decimal("18"), unit="plate", product_id=product.id)]),
        PaymentMode.CASH,
    )


# --- branch_leaderboard -------------------------------------------------------

def test_leaderboard_ranks_branches_by_todays_revenue_highest_first(db, tenant, staff):
    biriyani = _product(db, tenant)
    busy = _branch(db, tenant, "Busy Branch", "busy@acme.example.com")
    quiet = _branch(db, tenant, "Quiet Branch", "quiet@acme.example.com")

    _sell(tenant, busy, biriyani, "10")
    _sell(tenant, staff, biriyani, "2")
    db.session.commit()

    rows = branch_leaderboard(tenant)
    names = [r.branch_name for r in rows]
    assert names.index("Busy Branch") < names.index(staff.name) < names.index("Quiet Branch")
    quiet_row = next(r for r in rows if r.branch_name == "Quiet Branch")
    assert quiet_row.revenue == Decimal("0")
    assert quiet_row.order_count == 0


def test_leaderboard_includes_every_active_branch_even_with_zero_sales(db, tenant, staff):
    rows = branch_leaderboard(tenant)
    assert len(rows) == 1
    assert rows[0].branch_id == staff.id
    assert rows[0].revenue == Decimal("0")


# --- item_sales_today ---------------------------------------------------------

def test_item_sales_today_ranks_by_revenue_and_computes_share(db, tenant, staff):
    biriyani = _product(db, tenant, "Chicken Biriyani", "150")
    soda = _product(db, tenant, "Soda", "20")

    _sell(tenant, staff, biriyani, "2")  # 177 each -> 354 revenue
    _sell(tenant, staff, soda, "1")  # 23.6 revenue
    db.session.commit()

    rows = item_sales_today(tenant)
    assert rows[0].product_name == "Chicken Biriyani"
    assert rows[0].share_percent > rows[1].share_percent
    assert round(sum(r.share_percent for r in rows)) == 100


def test_item_sales_today_empty_when_nothing_sold(db, tenant, staff):
    assert item_sales_today(tenant) == []


# --- routes --------------------------------------------------------------------

def test_live_dashboard_renders_with_embedded_snapshot(client, db, tenant, client_admin, staff):
    biriyani = _product(db, tenant)
    _sell(tenant, staff, biriyani, "3")
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/live/")
    assert resp.status_code == 200
    assert b'"name": "Cashier"' in resp.data or b'"name":"Cashier"' in resp.data
    assert b"Chicken Biriyani" in resp.data


def test_live_data_json_reflects_latest_sale(client, db, tenant, client_admin, staff):
    biriyani = _product(db, tenant)
    _sell(tenant, staff, biriyani, "1")
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/live/data.json")
    assert resp.status_code == 200
    payload = resp.get_json()
    branch_row = next(b for b in payload["branches"] if b["id"] == staff.id)
    assert branch_row["revenue"] == 177.0
    assert branch_row["order_count"] == 1
    assert payload["items"][0]["name"] == "Chicken Biriyani"
    assert any(s["name"] == "Chicken Biriyani" for s in payload["stock"])


def test_live_dashboard_is_gated_to_client_admin(client, db, tenant, staff):
    _login(client, staff.email, "CashierSecret123")
    resp = client.get("/live/")
    assert resp.status_code == 403


def test_live_views_are_tenant_isolated(client, db, tenant, client_admin):
    from app.models.tenant import Gstin, RegistrationType, Tenant

    other_tenant = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR)
    db.session.add(other_tenant)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other_tenant.id, gstin="29CCCCC0000C1Z5", state_code="29", state_name="Karnataka", is_primary=True))
    db.session.commit()
    other_branch = _branch(db, other_tenant, "Foreign Branch", "foreign@other.example.com")
    other_product = _product(db, other_tenant, "Foreign Dish")
    _sell(other_tenant, other_branch, other_product, "5")
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/live/data.json")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert all(b["name"] != "Foreign Branch" for b in payload["branches"])
    assert all(i["name"] != "Foreign Dish" for i in payload["items"])
