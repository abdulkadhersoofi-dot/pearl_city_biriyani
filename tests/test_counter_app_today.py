"""ARFA Counter app, stage 4: Held bills, Today, cashier switching from the
home screen, and the kitchen screen (KOTs marked ready)."""

import uuid
from decimal import Decimal

import pytest

from app.models.business import DiningTable, KitchenTicket, KotStatus
from app.models.pos_bill import POSBill, POSBillStatus
from app.models.product import Product
from app.models.tenant import BillingCycle, RegistrationType, Tenant
from app.models.user import User, UserRole

D = Decimal


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _cashier(client):
    return _login(client, "cashier@acme.example.com", "CashierSecret123")


@pytest.fixture()
def menu(db, tenant, staff):
    tea = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="9963", gst_rate=D("5"), unit="NOS",
                  sale_price=D("20"), price_includes_tax=True)
    cake = Product(tenant_id=tenant.id, name="Cake", hsn_or_sac_code="1905", gst_rate=D("18"), unit="NOS",
                   sale_price=D("100"))
    db.session.add_all([tea, cake])
    db.session.commit()
    return {"tea": tea, "cake": cake}


@pytest.fixture()
def other_branch(db, tenant):
    user = User(tenant_id=tenant.id, name="Other Branch", email="other.branch@acme.example.com", role=UserRole.STAFF,
                must_change_password=False, can_view_reports=True)
    user.set_password("OtherBranch123")
    db.session.add(user)
    db.session.commit()
    return user


def _hold(client, *lines, label="", order_type=None):
    return client.post("/app/held", json={"label": label, "order_type": order_type,
                                          "lines": [{"product_id": p.id, "qty": q, "note": ""} for p, q in lines]})


def _sell(client, product, qty, mode):
    res = client.post("/pos/checkout", data={
        "line_product_id[]": str(product.id), "line_description[]": product.name, "line_hsn[]": product.hsn_or_sac_code,
        "line_qty[]": str(qty), "line_rate[]": str(product.sale_price), "line_gst_rate[]": str(product.gst_rate),
        "line_incl[]": "1" if product.price_includes_tax else "0", "line_unit[]": "NOS", "payment_mode": mode,
        "checkout_uuid": str(uuid.uuid4())}, headers={"X-Requested-With": "XMLHttpRequest"})
    assert res.status_code == 200, res.get_json()
    return res.get_json()


# --- held bills ----------------------------------------------------------------------------------------

def test_a_bill_is_held_listed_and_resumed(client, db, tenant, staff, menu):
    _cashier(client)
    res = _hold(client, (menu["tea"], 2), (menu["cake"], 1), label="Ravi")
    assert res.status_code == 200
    held = res.get_json()["held"]
    assert held["label"] == "Ravi" and held["count"] == 2 and held["total"] == 158  # 40 + 118
    bill = db.session.get(POSBill, held["id"])
    assert bill.status == POSBillStatus.HELD and bill.created_by_id == staff.id and bill.table_id is None
    assert [h["id"] for h in client.get("/app/held").get_json()["held"]] == [bill.id]
    resumed = client.post(f"/app/held/{bill.id}/resume").get_json()["held"]
    assert {l["name"]: l["qty"] for l in resumed["lines"]} == {"Tea": 2, "Cake": 1}
    assert client.get("/app/held").get_json()["held"] == []
    assert POSBill.query.count() == 0
    assert client.post(f"/app/held/{bill.id}/resume").status_code == 404  # opened once only


def test_a_held_bill_is_thrown_away(client, db, tenant, staff, menu):
    _cashier(client)
    bill_id = _hold(client, (menu["tea"], 1)).get_json()["held"]["id"]
    assert client.post(f"/app/held/{bill_id}/discard").status_code == 200
    assert POSBill.query.count() == 0


def test_held_bills_belong_to_their_counter(client, db, tenant, staff, menu, other_branch):
    _login(client, other_branch.email, "OtherBranch123")
    theirs = _hold(client, (menu["cake"], 1)).get_json()["held"]["id"]
    client.get("/auth/logout")
    _cashier(client)
    assert client.get("/app/held").get_json()["held"] == []
    assert client.post(f"/app/held/{theirs}/resume").status_code == 404
    assert client.post(f"/app/held/{theirs}/discard").status_code == 404
    assert db.session.get(POSBill, theirs) is not None


def test_only_your_own_items_can_be_held(client, db, tenant, staff, menu):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    theirs = Product(tenant_id=other.id, name="Their cake", hsn_or_sac_code="1905", gst_rate=D("18"), unit="NOS",
                     sale_price=D("1"))
    db.session.add(theirs)
    db.session.commit()
    _cashier(client)
    res = _hold(client, (theirs, 1))
    assert res.status_code == 400 and "isn't in your catalog" in res.get_json()["error"]
    assert _hold(client).status_code == 400  # nothing to hold
    assert client.get("/app/held").status_code == 200 and POSBill.query.count() == 0


def test_a_restaurant_holds_the_order_type_but_never_a_table(client, db, tenant, staff, menu):
    tenant.set_feature("restaurant_mode", True)
    db.session.commit()
    _cashier(client)
    assert _hold(client, (menu["tea"], 1), order_type="takeaway").get_json()["held"]["order_type"] == "takeaway"
    assert _hold(client, (menu["tea"], 1), order_type="dine_in").get_json()["held"]["order_type"] == ""


def test_held_needs_a_branch_login(client, db, client_admin):
    assert client.get("/app/held").status_code == 401
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get("/app/held").status_code == 401


# --- today -------------------------------------------------------------------------------------------

def test_today_shows_this_counters_bills(client, db, tenant, staff, menu, other_branch):
    _login(client, other_branch.email, "OtherBranch123")
    theirs = _sell(client, menu["cake"], 5, "card")
    client.get("/auth/logout")
    staff.can_view_reports = True
    db.session.commit()
    _cashier(client)
    first = _sell(client, menu["tea"], 2, "cash")
    second = _sell(client, menu["cake"], 1, "upi")
    res = client.get("/app/today")
    html = res.get_data(as_text=True)
    assert res.status_code == 200 and res.headers["X-Counter-Screen"] == "1"
    assert first["bill_number"] in html and second["bill_number"] in html and theirs["bill_number"] not in html
    assert "₹158.00" in html  # 40 + 118
    assert "Cash" in html and "UPI" in html and "/pos/z-report" in html
    assert 'class="app-tab is-on"' in html


def test_today_needs_permission_to_see_reports(client, db, tenant, staff):
    staff.can_view_reports = False
    db.session.commit()
    _cashier(client)
    assert client.get("/app/today").status_code == 403
    assert "/app/today" not in client.get("/app/").get_data(as_text=True)


# --- the home screen ----------------------------------------------------------------------------------

def test_the_home_screen_has_tabs_and_the_cashier(client, db, tenant, staff):
    from app.pos import cashiers

    tenant.set_feature("cashier_pin", True)
    cashiers.add_cashier(tenant, staff, "Ravi", "4821")
    db.session.commit()
    _cashier(client)
    html = client.get("/app/").get_data(as_text=True)
    assert 'aria-label="ARFA Counter"' in html and "No cashier signed in" in html and "/app/bill?lock=1" in html
    assert "/app/kitchen" not in html
    client.post("/pos/cashier/unlock", data={"pin": "4821"})
    assert "Cashier: <strong>Ravi</strong>" in client.get("/app/").get_data(as_text=True)


# --- the kitchen screen -------------------------------------------------------------------------------

@pytest.fixture()
def kitchen(db, tenant, staff, menu):
    tenant.set_feature("restaurant_mode", True)
    t1 = DiningTable(tenant_id=tenant.id, name="T1")
    db.session.add(t1)
    db.session.commit()
    return t1


def _kot(client, table, product, qty):
    res = client.post("/app/kot", json={"kots": [{"uuid": str(uuid.uuid4()), "table_id": table.id,
                                                  "items": [{"product_id": product.id, "name": product.name, "qty": qty}]}]})
    return res.get_json()["results"][0]


def test_the_kitchen_screen_is_for_a_restaurant(client, db, tenant, staff):
    _cashier(client)
    assert client.get("/app/kitchen").status_code == 404
    assert client.get("/app/kitchen/state").status_code == 404


def test_kots_go_to_the_kitchen_and_are_marked_ready(client, db, tenant, staff, menu, kitchen):
    _cashier(client)
    assert "/app/kitchen" in client.get("/app/").get_data(as_text=True)
    assert client.get("/app/kitchen").status_code == 200
    kot = _kot(client, kitchen, menu["tea"], 2)
    _sell_takeaway = client.post("/pos/checkout", data={
        "line_product_id[]": str(menu["cake"].id), "line_description[]": "Cake", "line_hsn[]": "1905", "line_qty[]": "1",
        "line_rate[]": "100", "line_gst_rate[]": "18", "line_unit[]": "NOS", "payment_mode": "cash",
        "order_type": "takeaway"}, headers={"X-Requested-With": "XMLHttpRequest"})
    assert _sell_takeaway.status_code == 200
    state = client.get("/app/kitchen/state").get_json()
    assert [k["label"] for k in state["cooking"]] == ["Table T1", "Takeaway"]  # oldest first
    assert state["cooking"][0]["items"] == [{"name": "Tea", "qty": "2", "note": "", "cancel": False}]
    assert state["ready"] == []
    assert client.post(f"/app/kitchen/{kot['kot_id']}/ready", json={"ready": True}).status_code == 200
    ticket = db.session.get(KitchenTicket, kot["kot_id"])
    assert ticket.ready_at is not None and ticket.ready_by_id == staff.id
    state = client.get("/app/kitchen/state").get_json()
    assert [k["label"] for k in state["cooking"]] == ["Takeaway"] and state["ready"][0]["id"] == kot["kot_id"]
    client.post(f"/app/kitchen/{kot['kot_id']}/ready", json={"ready": False})
    assert len(client.get("/app/kitchen/state").get_json()["cooking"]) == 2


def test_a_cancel_line_shows_and_a_cancelled_order_drops_off(client, db, tenant, staff, menu, kitchen):
    _cashier(client)
    _kot(client, kitchen, menu["tea"], 2)
    _kot(client, kitchen, menu["tea"], -1)
    cooking = client.get("/app/kitchen/state").get_json()["cooking"]
    assert cooking[1]["items"][0]["cancel"] is True and cooking[1]["items"][0]["qty"] == "1"
    _kot(client, kitchen, menu["tea"], -1)  # the last one off: the order is cancelled
    assert client.get("/app/kitchen/state").get_json()["cooking"] == []
    assert {t.status for t in KitchenTicket.query} == {KotStatus.CANCELLED}


def test_a_kitchen_sees_only_its_own_branch(client, db, tenant, staff, menu, kitchen, other_branch):
    _login(client, other_branch.email, "OtherBranch123")
    theirs = _kot(client, kitchen, menu["tea"], 1)
    client.get("/auth/logout")
    _cashier(client)
    assert client.get("/app/kitchen/state").get_json()["cooking"] == []
    assert client.post(f"/app/kitchen/{theirs['kot_id']}/ready", json={}).status_code == 404
    assert db.session.get(KitchenTicket, theirs["kot_id"]).ready_at is None
