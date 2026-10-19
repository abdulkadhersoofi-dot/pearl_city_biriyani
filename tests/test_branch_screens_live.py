"""Branch screens and live tables. A branch login gets a menu with the
screens its business has beyond the counter (a restaurant's Tables,
Kitchen and Sold out; Job cards) - only what is on for that login. Tables
and KOTs reach every screen without a reload: the Tables page and an open
table look again every few seconds, the owner has a kitchen screen with
every counter's KOTs, and a KOT or bill sent from an out-of-date view of
a table is refused (with the table as it is now) instead of cancelling
or leaving off what another screen added."""

import re
import uuid
from decimal import Decimal

import pytest

from app.models.business import DiningTable, KitchenTicket
from app.models.pos_bill import POSBill, POSBillStatus
from app.models.product import Product
from app.models.user import User, UserRole
from app.restaurant.services import running_order

D = Decimal
XHR = {"X-Requested-With": "XMLHttpRequest"}


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _cashier(client):
    return _login(client, "cashier@acme.example.com", "CashierSecret123")


def _owner(client):
    return _login(client, "admin@acme.example.com", "ClientSecret123")


def _menu(html):
    return re.findall(r'<span class="nav-label">([^<]+)</span>', html)


@pytest.fixture()
def hall(db, tenant, staff, client_admin):
    tenant.set_feature("restaurant_mode", True)
    t1 = DiningTable(tenant_id=tenant.id, name="T1", sort_order=0)
    t2 = DiningTable(tenant_id=tenant.id, name="T2", sort_order=1)
    biryani = Product(tenant_id=tenant.id, name="Biryani", hsn_or_sac_code="9963", gst_rate=D("5"), unit="NOS",
                      sale_price=D("150"))
    tea = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="9963", gst_rate=D("5"), unit="NOS",
                  sale_price=D("20"), price_includes_tax=True)
    db.session.add_all([t1, t2, biryani, tea])
    db.session.commit()
    return {"t1": t1, "t2": t2, "biryani": biryani, "tea": tea}


@pytest.fixture()
def waiter(db, tenant):
    user = User(tenant_id=tenant.id, name="Waiter Phone", email="waiter@acme.example.com", role=UserRole.STAFF,
                must_change_password=False)
    user.set_password("WaiterSecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _lines(*items):
    data = {"line_product_id[]": [], "line_description[]": [], "line_hsn[]": [], "line_qty[]": [], "line_rate[]": [],
            "line_gst_rate[]": [], "line_incl[]": [], "line_unit[]": [], "line_note[]": []}
    for product, qty in items:
        data["line_product_id[]"].append(str(product.id))
        data["line_description[]"].append(product.name)
        data["line_hsn[]"].append(product.hsn_or_sac_code)
        data["line_qty[]"].append(str(qty))
        data["line_rate[]"].append(str(product.sale_price))
        data["line_gst_rate[]"].append(str(product.gst_rate))
        data["line_incl[]"].append("1" if product.price_includes_tax else "0")
        data["line_unit[]"].append("NOS")
        data["line_note[]"].append("")
    return data


def _web_kot(client, table, *items, sig=None):
    data = _lines(*items)
    if sig is not None:
        data["table_sig"] = sig
    return client.post(f"/restaurant/tables/{table.id}/kot", data=data, headers=XHR)


def _app_kot(client, table, *items):
    kot = {"uuid": str(uuid.uuid4()), "table_id": table.id, "ref": "",
           "items": [{"product_id": p.id, "name": p.name, "note": "", "qty": q} for p, q in items]}
    res = client.post("/app/kot", json={"kots": [kot]})
    assert res.status_code == 200
    return res.get_json()["results"][0]


def _bill_table(client, table, *items, sig=None):
    data = _lines(*items)
    data.update({"payment_mode": "cash", "order_type": "dine_in", "table_id": str(table.id),
                 "checkout_uuid": str(uuid.uuid4())})
    if sig is not None:
        data["table_sig"] = sig
    return client.post("/pos/checkout", data=data, headers=XHR)


def _qty(tenant, table):
    order = running_order(tenant.id, table.id)
    return {l.description: l.qty for l in order.lines if not l.charge_kind} if order else {}


# --- a branch's menu ----------------------------------------------------------------------------------

def test_a_restaurant_branch_gets_its_screens_in_a_menu(client, db, tenant, staff, hall):
    staff.can_view_reports = True
    db.session.commit()
    _cashier(client)
    html = client.get("/pos/").get_data(as_text=True)
    assert 'class="sidenav"' in html
    assert _menu(html) == ["Counter", "Tables", "Kitchen", "Sold out today", "Day-book", "Z-report"]
    for url in ("/restaurant/", "/restaurant/kitchen", "/restaurant/menu", "/pos/bills", "/pos/z-report"):
        assert client.get(url).status_code == 200, url
    palette = {e["label"] for e in __import__("json").loads(
        re.search(r'id="palette-data">(.*?)</script>', html, re.S).group(1))}
    assert {"Tables", "Kitchen", "Day-book"} <= palette
    assert "Items" not in palette and "Branches" not in palette  # the owner's screens stay the owner's


def test_the_menu_follows_the_logins_permissions_and_modules(client, db, tenant, staff, hall):
    staff.can_view_reports = False
    staff.hidden_modules = ["restaurant_mode"]
    tenant.set_feature("job_cards", True)
    db.session.commit()
    _cashier(client)
    html = client.get("/pos/").get_data(as_text=True)
    assert _menu(html) == ["Counter", "Job cards"]
    assert client.get("/restaurant/").status_code == 404
    assert client.get("/restaurant/kitchen").status_code == 404


def test_a_branch_with_only_the_counter_keeps_the_simple_layout(client, db, tenant, staff):
    staff.can_view_reports = True
    db.session.commit()
    _cashier(client)
    html = client.get("/pos/").get_data(as_text=True)
    assert 'class="sidenav"' not in html and "palette-data" not in html


# --- live tables ---------------------------------------------------------------------------------------

def test_the_tables_page_is_live(client, db, tenant, staff, hall, waiter):
    _owner(client)
    page = client.get("/restaurant/").get_data(as_text=True)
    sig = re.search(r'data-sig="([0-9a-f]+)"', page).group(1)
    assert "tables_live.js" in page and "/restaurant/kitchen" in page
    live = client.get("/restaurant/?live=1").get_json()
    assert live["sig"] == sig and 'data-table-id="%d"' % hall["t1"].id in live["html"]
    client.get("/auth/logout")
    _login(client, waiter.email, "WaiterSecret123")
    _app_kot(client, hall["t1"], (hall["tea"], 2))
    client.get("/auth/logout")
    _owner(client)
    live = client.get("/restaurant/?live=1").get_json()
    assert live["sig"] != sig and "1 item" in live["html"] and "1 KOT" in live["html"]


def test_one_tables_state_matches_the_apps(client, db, tenant, staff, hall):
    _cashier(client)
    empty = client.get(f"/restaurant/tables/{hall['t1'].id}/state").get_json()
    assert empty == {"table_id": hall["t1"].id, "sig": "", "lines": []}
    _app_kot(client, hall["t1"], (hall["tea"], 2), (hall["biryani"], 1))
    one = client.get(f"/restaurant/tables/{hall['t1'].id}/state").get_json()
    assert one["sig"] and {l["name"]: l["qty"] for l in one["lines"]} == {"Tea": 2, "Biryani": 1}
    app_t1 = next(t for t in client.get("/app/tables/state").get_json()["tables"] if t["id"] == hall["t1"].id)
    assert app_t1["sig"] == one["sig"]
    # A KOT's reply carries the table's new signature: the phone bills with it at once.
    after = _app_kot(client, hall["t1"], (hall["tea"], 1))
    assert after["sig"] == client.get(f"/restaurant/tables/{hall['t1'].id}/state").get_json()["sig"] != one["sig"]
    assert _bill_table(client, hall["t1"], (hall["tea"], 3), (hall["biryani"], 1), sig=after["sig"]).status_code == 200


def test_the_counter_opens_a_table_with_its_signature(client, db, tenant, staff, hall):
    _cashier(client)
    _app_kot(client, hall["t1"], (hall["tea"], 1))
    sig = client.get(f"/restaurant/tables/{hall['t1'].id}/state").get_json()["sig"]
    html = client.get(f"/pos/?table={hall['t1'].id}").get_data(as_text=True)
    assert f'"table_sig": "{sig}"' in html and f"/restaurant/tables/{hall['t1'].id}/state" in html


# --- an out-of-date view of a table is refused ---------------------------------------------------------

def test_a_kot_from_an_old_view_cannot_cancel_what_another_screen_added(client, db, tenant, staff, hall, waiter):
    t1 = hall["t1"]
    _owner(client)
    assert _web_kot(client, t1, (hall["biryani"], 1), sig="").status_code == 200
    seen = client.get(f"/restaurant/tables/{t1.id}/state").get_json()["sig"]
    client.get("/auth/logout")
    _login(client, waiter.email, "WaiterSecret123")
    _app_kot(client, t1, (hall["tea"], 2))  # the waiter adds tea meanwhile
    client.get("/auth/logout")
    _owner(client)
    # The counter still shows only the biryani and adds one more.
    res = _web_kot(client, t1, (hall["biryani"], 2), sig=seen)
    assert res.status_code == 409 and res.get_json()["table_changed"] is True
    assert {l["name"]: l["qty"] for l in res.get_json()["table"]["lines"]} == {"Biryani": 1, "Tea": 2}
    assert _qty(tenant, t1) == {"Biryani": 1, "Tea": 2}  # nothing cancelled
    assert KitchenTicket.query.count() == 2
    # Folded in (as the screen does) and sent again with the new signature.
    now = res.get_json()["table"]["sig"]
    assert _web_kot(client, t1, (hall["biryani"], 2), (hall["tea"], 2), sig=now).status_code == 200
    assert _qty(tenant, t1) == {"Biryani": 2, "Tea": 2}


def test_a_table_cannot_be_billed_without_what_another_screen_added(client, db, tenant, staff, hall, waiter):
    t1 = hall["t1"]
    _cashier(client)
    _app_kot(client, t1, (hall["biryani"], 1))
    seen = client.get(f"/restaurant/tables/{t1.id}/state").get_json()["sig"]
    client.get("/auth/logout")
    _login(client, waiter.email, "WaiterSecret123")
    _app_kot(client, t1, (hall["tea"], 3))
    client.get("/auth/logout")
    _cashier(client)
    res = _bill_table(client, t1, (hall["biryani"], 1), sig=seen)
    assert res.status_code == 409 and "another screen" in res.get_json()["error"]
    assert POSBill.query.filter(POSBill.bill_number.isnot(None)).count() == 0
    assert _qty(tenant, t1) == {"Biryani": 1, "Tea": 3}
    now = res.get_json()["table"]["sig"]
    res = _bill_table(client, t1, (hall["biryani"], 1), (hall["tea"], 3), sig=now)
    assert res.status_code == 200, res.get_json()
    assert running_order(tenant.id, t1.id) is None
    assert POSBill.query.filter_by(status=POSBillStatus.COMPLETED).one().grand_total == D("218.00")  # 157.50 + 60, rounded


def test_a_screen_that_sends_no_signature_works_as_before(client, db, tenant, staff, hall):
    _cashier(client)
    _app_kot(client, hall["t1"], (hall["biryani"], 1))
    assert _web_kot(client, hall["t1"], (hall["biryani"], 2)).status_code == 200
    assert _bill_table(client, hall["t1"], (hall["biryani"], 2)).status_code == 200


# --- the kitchen on the website -------------------------------------------------------------------------

def test_the_owners_kitchen_has_every_counters_kots(client, db, tenant, staff, hall, waiter):
    _login(client, waiter.email, "WaiterSecret123")
    theirs = _app_kot(client, hall["t1"], (hall["tea"], 2))
    client.get("/auth/logout")
    _cashier(client)
    mine = _app_kot(client, hall["t2"], (hall["biryani"], 1))
    state = client.get("/restaurant/kitchen/state").get_json()
    assert [k["id"] for k in state["cooking"]] == [mine["kot_id"]] and state["cooking"][0]["by"] == ""
    assert client.post(f"/restaurant/kitchen/{theirs['kot_id']}/ready", json={}).status_code == 404
    client.get("/auth/logout")
    _owner(client)
    page = client.get("/restaurant/kitchen")
    assert page.status_code == 200 and "All counters" in page.get_data(as_text=True)
    state = client.get("/restaurant/kitchen/state").get_json()
    assert [(k["label"], k["by"]) for k in state["cooking"]] == [("Table T1", "Waiter Phone"), ("Table T2", "Cashier")]
    assert client.post(f"/restaurant/kitchen/{theirs['kot_id']}/ready", json={"ready": True}).status_code == 200
    assert db.session.get(KitchenTicket, theirs["kot_id"]).ready_at is not None
    assert [k["id"] for k in client.get("/restaurant/kitchen/state").get_json()["cooking"]] == [mine["kot_id"]]


def test_the_kitchen_and_live_tables_are_for_a_restaurant(client, db, tenant, staff, client_admin):
    _owner(client)
    for url in ("/restaurant/kitchen", "/restaurant/kitchen/state", "/restaurant/?live=1", "/restaurant/tables/1/state"):
        assert client.get(url).status_code == 404, url
