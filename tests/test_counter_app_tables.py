"""ARFA Counter app, stage 3: tables and KOT on the phone. A KOT is sent as
what changed on a table (+ / - quantities) and added to the table's order
as it stands on the server, once per KOT id; offline KOTs carry the
phone's own slip number; a table billed offline takes what it billed off
the table when the bill reaches the books."""

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.audit_log import AuditLog
from app.models.business import DiningTable, KitchenTicket, KotStatus
from app.models.platform import OfflineBill, OfflineBillStatus
from app.models.pos_bill import POSBill, POSBillStatus
from app.models.product import Product
from app.models.tenant import BillingCycle, RegistrationType, Tenant
from app.restaurant.services import running_order

D = Decimal


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _cashier(client):
    return _login(client, "cashier@acme.example.com", "CashierSecret123")


@pytest.fixture()
def hall(db, tenant, staff):
    tenant.set_feature("restaurant_mode", True)
    t1 = DiningTable(tenant_id=tenant.id, name="T1", section="Hall", sort_order=0)
    t2 = DiningTable(tenant_id=tenant.id, name="T2", section="Hall", sort_order=1)
    biryani = Product(tenant_id=tenant.id, name="Biryani", hsn_or_sac_code="9963", gst_rate=D("5"), unit="NOS",
                      sale_price=D("150"))
    tea = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="9963", gst_rate=D("5"), unit="NOS",
                  sale_price=D("20"), price_includes_tax=True)
    db.session.add_all([t1, t2, biryani, tea])
    db.session.commit()
    return {"t1": t1, "t2": t2, "biryani": biryani, "tea": tea}


def _kot(client, table, *items, kot_id=None, ref=""):
    kot = {"uuid": kot_id or str(uuid.uuid4()), "table_id": table.id, "ref": ref,
           "items": [{"product_id": p.id, "name": p.name, "note": "", "qty": q} for p, q in items]}
    res = client.post("/app/kot", json={"kots": [kot]})
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()["results"][0]


def _qty(tenant, table):
    order = running_order(tenant.id, table.id)
    return {l.description: l.qty for l in order.lines} if order else {}


# --- the screens ---------------------------------------------------------------------------------------

def test_tables_are_there_only_for_a_restaurant(client, db, tenant, staff):
    _cashier(client)
    assert client.get("/app/tables").status_code == 404
    assert client.get("/app/tables/state").status_code == 404
    assert client.post("/app/kot", json={"kots": []}).status_code == 404
    assert "/app/tables" not in client.get("/app/").get_data(as_text=True)


def test_the_tables_screen_and_state(client, db, tenant, staff, hall):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    db.session.add(DiningTable(tenant_id=other.id, name="Their table"))
    db.session.commit()
    _cashier(client)
    assert "/app/tables" in client.get("/app/").get_data(as_text=True)
    res = client.get("/app/tables")
    assert res.status_code == 200 and res.headers["X-Counter-Screen"] == "1"
    html = res.get_data(as_text=True)
    assert '"view": "tables"' in html and "counter_tables.js" in html and '"kot_url": "/app/kot"' in html
    _kot(client, hall["t1"], (hall["biryani"], 2), (hall["tea"], 1))
    tables = {t["name"]: t for t in client.get("/app/tables/state").get_json()["tables"]}
    assert set(tables) == {"T1", "T2"}  # this business's tables only
    t1 = tables["T1"]
    assert t1["busy"] and t1["kots"] == 1 and t1["total"] == 335.0  # 300 + 5% + 20, rounded
    assert {l["name"]: l["qty"] for l in t1["lines"]} == {"Biryani": 2, "Tea": 1}
    assert t1["sent"] == {f"{hall['biryani'].id}|Biryani|": 2, f"{hall['tea'].id}|Tea|": 1}
    assert tables["T2"]["busy"] is False and tables["T2"]["lines"] == []


# --- KOTs as changes -----------------------------------------------------------------------------------

def test_a_kot_from_the_phone_starts_the_table(client, db, tenant, staff, hall):
    _cashier(client)
    result = _kot(client, hall["t1"], (hall["biryani"], 2))
    assert result["status"] == "sent" and result["kot_no"] == 1
    kot = db.session.get(KitchenTicket, result["kot_id"])
    assert kot.table_id == hall["t1"].id and kot.status == KotStatus.SENT and kot.client_uuid == result["uuid"]
    assert kot.items == [{"product_id": hall["biryani"].id, "name": "Biryani", "note": "", "qty": "2"}]
    assert _qty(tenant, hall["t1"]) == {"Biryani": D("2")}
    order = running_order(tenant.id, hall["t1"].id)
    assert order.order_type == "dine_in" and order.grand_total == D("315")
    log = AuditLog.query.filter_by(action="kot_sent").one()
    assert log.details["from"] == "app"


def test_the_same_kot_is_never_booked_twice(client, db, tenant, staff, hall):
    _cashier(client)
    same = str(uuid.uuid4())
    first = _kot(client, hall["t1"], (hall["tea"], 3), kot_id=same)
    again = _kot(client, hall["t1"], (hall["tea"], 3), kot_id=same)
    assert again["status"] == "sent" and again["kot_id"] == first["kot_id"]
    assert KitchenTicket.query.count() == 1 and _qty(tenant, hall["t1"]) == {"Tea": D("3")}


def test_kots_add_up_from_every_phone_and_the_counter(client, db, tenant, staff, hall):
    _cashier(client)
    _kot(client, hall["t1"], (hall["biryani"], 2))
    # The counter's own Send KOT (the whole order as it stands) adds a tea.
    client.post(f"/restaurant/tables/{hall['t1'].id}/kot", data={
        "line_product_id[]": [str(hall["biryani"].id), str(hall["tea"].id)], "line_description[]": ["Biryani", "Tea"],
        "line_hsn[]": ["9963", "9963"], "line_qty[]": ["2", "1"], "line_rate[]": ["150", "20"],
        "line_gst_rate[]": ["5", "5"], "line_unit[]": ["NOS", "NOS"]})
    assert _qty(tenant, hall["t1"]) == {"Biryani": D("2"), "Tea": D("1")}
    # A phone that was offline the whole time adds one biryani: added, not replaced.
    _kot(client, hall["t1"], (hall["biryani"], 1), ref="O1-1")
    assert _qty(tenant, hall["t1"]) == {"Biryani": D("3"), "Tea": D("1")}
    assert KitchenTicket.query.filter_by(device_ref="O1-1").one().kot_no == 3
    # Taking one off goes to the kitchen as a cancel line.
    cancel = _kot(client, hall["t1"], (hall["biryani"], -1))
    assert cancel["items"] == [{"product_id": hall["biryani"].id, "name": "Biryani", "note": "", "qty": "-1"}]
    assert _qty(tenant, hall["t1"]) == {"Biryani": D("2"), "Tea": D("1")}


def test_taking_everything_off_frees_the_table(client, db, tenant, staff, hall):
    _cashier(client)
    _kot(client, hall["t1"], (hall["tea"], 2))
    result = _kot(client, hall["t1"], (hall["tea"], -5))  # never below nothing
    assert result["items"][0]["qty"] == "-2"
    assert running_order(tenant.id, hall["t1"].id) is None
    assert {k.status for k in KitchenTicket.query} == {KotStatus.CANCELLED}
    assert _kot(client, hall["t1"], (hall["tea"], -1))["status"] == "unchanged"


def test_kots_the_server_cant_take_are_refused(client, db, tenant, staff, hall):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    theirs = DiningTable(tenant_id=other.id, name="X1")
    their_tea = Product(tenant_id=other.id, name="Their tea", hsn_or_sac_code="9963", gst_rate=D("5"), unit="NOS",
                        sale_price=D("9"))
    db.session.add_all([theirs, their_tea])
    hall["biryani"].sold_out_on = date.today()
    db.session.commit()
    _cashier(client)
    assert "isn't in use" in _kot(client, theirs, (hall["tea"], 1))["error"]
    assert "isn't on your menu" in _kot(client, hall["t1"], (their_tea, 1))["error"]
    assert "sold out" in _kot(client, hall["t1"], (hall["biryani"], 1))["error"]
    assert KitchenTicket.query.count() == 0 and running_order(tenant.id, hall["t1"].id) is None


def test_a_takeaway_checkout_tells_the_phone_its_kot_number(client, db, tenant, staff, hall):
    _cashier(client)
    res = client.post("/pos/checkout", data={
        "line_product_id[]": str(hall["tea"].id), "line_description[]": "Tea", "line_hsn[]": "9963", "line_qty[]": "2",
        "line_rate[]": "20", "line_gst_rate[]": "5", "line_incl[]": "1", "line_unit[]": "NOS", "payment_mode": "cash",
        "order_type": "takeaway"}, headers={"X-Requested-With": "XMLHttpRequest"})
    data = res.get_json()
    assert data["kot_no"] == 1 and data["kot_pdf"]


# --- bills made offline ----------------------------------------------------------------------------------

def _paired(client, db, staff):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    html = client.post(f"/branches/{staff.id}/pair").get_data(as_text=True)
    client.get("/auth/logout")
    code = "".join(re.search(r'<p class="pair-big"[^>]*><span>(\d{3})</span><span>(\d{3})</span>', html).groups())
    return client.post("/app/pair", json={"code": code, "name": "Waiter phone"}).get_json()


def _offline_bill(client, dev, seq, lines, **extra):
    payload = {"uuid": str(uuid.uuid4()), "number": f"{dev['code']}/{dev['fy']}/{seq:04d}", "fy": dev["fy"], "seq": seq,
               "billed_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
               "lines": [{"product_id": p.id, "qty": str(q), "rate": str(p.sale_price)} for p, q in lines],
               "payment_mode": "cash", **extra}
    res = client.post("/pos/offline/sync", json={"bills": [payload]},
                      headers={"X-Device-Id": str(dev["device_id"]), "X-Device-Token": dev["token"]})
    return res.get_json()["results"][0]


def test_a_table_billed_offline_is_free_once_the_bill_is_booked(client, db, tenant, client_admin, staff, hall):
    tenant.set_feature("offline_pos", True)
    db.session.commit()
    dev = _paired(client, db, staff)
    _kot(client, hall["t1"], (hall["tea"], 2), ref="O1-1")  # the KOT goes first...
    result = _offline_bill(client, dev, 1, [(hall["tea"], 2)], table_id=hall["t1"].id, order_type="dine_in")
    assert result["status"] == "synced"  # ...then the bill that settles the table
    bill = db.session.get(POSBill, result["bill_id"])
    assert bill.table_id == hall["t1"].id and bill.order_type == "dine_in"
    assert running_order(tenant.id, hall["t1"].id) is None
    kot = KitchenTicket.query.one()
    assert kot.status == KotStatus.BILLED and kot.pos_bill_id == bill.id


def test_what_others_added_stays_on_the_table(client, db, tenant, client_admin, staff, hall):
    tenant.set_feature("offline_pos", True)
    db.session.commit()
    dev = _paired(client, db, staff)
    _kot(client, hall["t1"], (hall["tea"], 2), (hall["biryani"], 1))
    _offline_bill(client, dev, 1, [(hall["tea"], 2)], table_id=hall["t1"].id)
    assert _qty(tenant, hall["t1"]) == {"Biryani": D("1")}
    assert KitchenTicket.query.one().status == KotStatus.SENT


def test_a_takeaway_billed_offline_gets_its_kitchen_ticket(client, db, tenant, client_admin, staff, hall):
    tenant.set_feature("offline_pos", True)
    db.session.commit()
    dev = _paired(client, db, staff)
    result = _offline_bill(client, dev, 1, [(hall["biryani"], 1)], order_type="takeaway")
    kot = KitchenTicket.query.one()
    assert kot.status == KotStatus.BILLED and kot.order_type == "takeaway" and kot.pos_bill_id == result["bill_id"]
    assert kot.device_ref == result["bill_number"]


def test_an_offline_bill_for_someone_elses_table_waits_for_the_owner(client, db, tenant, client_admin, staff, hall):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    theirs = DiningTable(tenant_id=other.id, name="X1")
    db.session.add(theirs)
    tenant.set_feature("offline_pos", True)
    db.session.commit()
    dev = _paired(client, db, staff)
    result = _offline_bill(client, dev, 1, [(hall["tea"], 1)], table_id=theirs.id)
    assert result["status"] == "failed" and "isn't one of yours" in result["error"]
    assert OfflineBill.query.one().status == OfflineBillStatus.FAILED
    assert POSBill.query.filter(POSBill.status != POSBillStatus.HELD).count() == 0
