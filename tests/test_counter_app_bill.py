"""ARFA Counter app, stage 2: the Bill screen. A branch taps items and takes
cash / UPI / card on its phone; the bill goes through the counter's own
checkout online, or - offline billing on, no internet - into the paired
phone's own series, booked when it is back online."""

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.platform import CounterDevice
from app.models.pos_bill import POSBill
from app.models.price_list import PriceList, PriceListItem
from app.models.product import Product
from app.models.tenant import BillingCycle, RegistrationType, Tenant
from app.utils.module_settings import save_module_settings

D = Decimal


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _cashier(client):
    return _login(client, "cashier@acme.example.com", "CashierSecret123")


def _config(html):
    raw = re.search(r'<script type="application/json" id="bill-config">(.*?)</script>', html, re.S).group(1)
    return json.loads(raw)


@pytest.fixture()
def menu(db, tenant, staff):
    tea = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="0902", gst_rate=D("5"), unit="NOS",
                  sale_price=D("20"), price_includes_tax=True)
    soap = Product(tenant_id=tenant.id, name="Soap", hsn_or_sac_code="3401", gst_rate=D("18"), unit="NOS",
                   sale_price=D("45.50"))
    phone = Product(tenant_id=tenant.id, name="Phone X", hsn_or_sac_code="8517", gst_rate=D("18"), unit="NOS",
                    sale_price=D("9999"), track_serial=True)
    gone = Product(tenant_id=tenant.id, name="Old stock", hsn_or_sac_code="3401", gst_rate=D("18"), unit="NOS",
                   sale_price=D("1"), is_active=False)
    db.session.add_all([tea, soap, phone, gone])
    db.session.commit()
    return {"tea": tea, "soap": soap, "phone": phone}


def _code(client, db, staff):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    html = client.post(f"/branches/{staff.id}/pair").get_data(as_text=True)
    client.get("/auth/logout")
    return "".join(re.search(r'<p class="pair-big"[^>]*><span>(\d{3})</span><span>(\d{3})</span>', html).groups())


def _pair(client, db, staff):
    res = client.post("/app/pair", json={"code": _code(client, db, staff), "name": "Front phone", "platform": "android"})
    assert res.status_code == 200, res.get_json()
    return res.get_json()


def _app_bill(product, qty, mode="cash", checkout_uuid=None, **extra):
    """What the Bill screen posts (counter_bill.js formData + checkout)."""
    data = {"line_product_id[]": str(product.id), "line_description[]": product.name,
            "line_hsn[]": product.hsn_or_sac_code, "line_qty[]": str(qty), "line_rate[]": str(product.sale_price),
            "line_gst_rate[]": str(product.gst_rate), "line_incl[]": "1" if product.price_includes_tax else "0",
            "line_unit[]": product.unit, "line_discount_kind[]": "", "line_discount_value[]": "", "line_note[]": "",
            "line_alt[]": "0", "line_net_weight[]": "", "payment_mode": mode,
            "checkout_uuid": checkout_uuid or str(uuid.uuid4())}
    data.update(extra)
    return data


# --- the screen ----------------------------------------------------------------------------------------

def test_a_branch_gets_its_items_on_the_bill_screen(client, db, tenant, staff, menu):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    db.session.add(Product(tenant_id=other.id, name="Their tea", hsn_or_sac_code="0902", gst_rate=D("5"), unit="NOS",
                           sale_price=D("10")))
    tenant.set_feature("serial_imei", True)
    db.session.commit()
    _cashier(client)
    res = client.get("/app/bill")
    assert res.status_code == 200 and res.headers["X-Counter-Screen"] == "1"
    cfg = _config(res.get_data(as_text=True))
    items = {i["name"]: i for i in cfg["items"]}
    assert set(items) == {"Tea", "Soap", "Phone X"}  # active, this business only
    assert items["Tea"]["rate"] == 20 and items["Tea"]["incl"] is True and items["Soap"]["incl"] is False
    assert items["Phone X"]["counter_only"] is True and items["Tea"]["counter_only"] is False
    assert [m["value"] for m in cfg["modes"]] == ["cash", "upi", "card"]
    assert cfg["checkout_url"] == "/pos/checkout" and cfg["offline"] is None and cfg["cashier"] is None
    assert cfg["order_types"] == [] and cfg["quote_url"] == ""
    assert "/app/bill" in client.get("/app/").get_data(as_text=True)


def test_only_a_branch_opens_the_bill_screen(client, db, client_admin):
    assert client.get("/app/bill").headers["Location"].endswith("/app/")
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get("/app/bill").headers["Location"].endswith("/app/")


def test_the_phone_keeps_only_signed_in_screens(client, db, staff):
    assert "X-Counter-Screen" not in client.get("/app/").headers  # the pairing screen
    _cashier(client)
    assert client.get("/app/").headers["X-Counter-Screen"] == "1"


def test_the_branch_price_list_prices_the_tiles(client, db, tenant, staff, menu):
    tenant.set_feature("multi_price_list", True)
    plist = PriceList(tenant_id=tenant.id, name="Branch")
    db.session.add(plist)
    db.session.flush()
    db.session.add(PriceListItem(tenant_id=tenant.id, price_list_id=plist.id, product_id=menu["tea"].id, price=D("18")))
    staff.price_list_id = plist.id
    db.session.commit()
    _cashier(client)
    items = {i["name"]: i for i in _config(client.get("/app/bill").get_data(as_text=True))["items"]}
    assert items["Tea"]["rate"] == 18 and items["Soap"]["rate"] == 45.5


def test_ping_gives_a_fresh_token_or_says_signed_out(client, db, staff):
    assert client.get("/app/ping").get_json() == {"signed_in": False}
    _cashier(client)
    data = client.get("/app/ping").get_json()
    assert data["signed_in"] is True and data["user_id"] == staff.id and data["csrf"]


# --- billing -------------------------------------------------------------------------------------------

def test_a_bill_from_the_phone_goes_through_the_counter_checkout(client, db, tenant, client_admin, staff, menu):
    _pair(client, db, staff)
    sent = _app_bill(menu["tea"], 2, cash_tendered="50")
    res = client.post("/pos/checkout", data=sent, headers={"X-Requested-With": "XMLHttpRequest"})
    data = res.get_json()
    assert res.status_code == 200 and data["bill_number"].startswith("POS/") and data["grand_total"] == 40
    assert data["change_due"] == 10
    bill = db.session.get(POSBill, data["bill_id"])
    assert bill.created_by_id == staff.id and bill.tenant_id == tenant.id
    # The reply was lost and the phone sends it again: the same bill.
    again = client.post("/pos/checkout", data=sent, headers={"X-Requested-With": "XMLHttpRequest"}).get_json()
    assert again["bill_id"] == bill.id and again["repeat"] is True
    assert POSBill.query.count() == 1


def test_offline_bills_from_the_paired_phone_land_in_its_series(client, db, tenant, client_admin, staff, menu):
    tenant.set_feature("offline_pos", True)
    db.session.commit()
    paired = _pair(client, db, staff)
    assert paired["fy"] and paired["next_number"] == 1
    cfg = _config(client.get("/app/bill").get_data(as_text=True))
    off = cfg["offline"]
    assert off["sw_url"] == "/app/sw.js" and off["sw_scope"] == "/app/" and off["user_id"] == staff.id
    assert off["modes"] == ["cash", "upi", "card"]
    payload = {"uuid": str(uuid.uuid4()), "number": f"O1/{paired['fy']}/0001", "fy": paired["fy"], "seq": 1,
               "billed_at": (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(),
               "lines": [{"product_id": menu["tea"].id, "qty": "3", "rate": "20"}], "payment_mode": "upi",
               "total": "60.00"}
    res = client.post("/pos/offline/sync", json={"bills": [payload]},
                      headers={"X-Device-Id": str(paired["device_id"]), "X-Device-Token": paired["token"]})
    result = res.get_json()["results"][0]
    assert result["status"] == "synced" and result["bill_number"] == f"O1/{paired['fy']}/0001"
    bill = db.session.get(POSBill, result["bill_id"])
    assert bill.device_id == paired["device_id"] and bill.grand_total == D("60")
    assert res.get_json()["next_number"] == 2
    assert CounterDevice.query.count() == 1


def test_items_that_need_the_full_counter_are_kept_off_offline_bills(client, db, tenant, staff, menu):
    tenant.set_feature("offline_pos", True)
    tenant.set_feature("serial_imei", True)
    db.session.commit()
    _cashier(client)
    cfg = _config(client.get("/app/bill").get_data(as_text=True))
    assert cfg["offline"]["blocked_ids"] == [menu["phone"].id]
    assert 'href="/pos/"' in client.get("/app/bill").get_data(as_text=True)  # "Open the full counter"


def test_the_app_has_its_own_service_worker_and_the_counter_keeps_its_own(client):
    res = client.get("/app/sw.js")
    assert res.status_code == 200 and res.headers["Service-Worker-Allowed"] == "/app/"
    assert b"arfa-app-v1" in res.data and b"X-Counter-Screen" in res.data
    assert client.get("/pos/sw.js").headers["Service-Worker-Allowed"] == "/pos/"


# --- what the counter needs first ------------------------------------------------------------------------

def test_the_day_is_opened_from_the_phone(client, db, tenant, staff, menu):
    tenant.set_feature("day_close", True)
    db.session.commit()
    _cashier(client)
    html = client.get("/app/bill").get_data(as_text=True)
    assert "Open the day" in html and 'name="back" value="app"' in html
    res = client.post("/pos/day/open", data={"opening_cash": "500", "back": "app"})
    assert res.headers["Location"].endswith("/app/bill")
    assert "Open the day" not in client.get("/app/bill").get_data(as_text=True)
    assert client.post("/pos/day/open", data={"opening_cash": "1"}).headers["Location"].endswith("/pos/")


def test_a_counter_with_cashier_pins_asks_for_one(client, db, tenant, staff):
    from app.pos import cashiers

    tenant.set_feature("cashier_pin", True)
    cashiers.add_cashier(tenant, staff, "Ravi", "4821")
    db.session.commit()
    _cashier(client)
    cfg = _config(client.get("/app/bill").get_data(as_text=True))
    assert cfg["cashier"]["current"] is None and cfg["cashier"]["unlock_url"] == "/pos/cashier/unlock"


def test_a_restaurant_picks_takeaway_and_the_server_prices_it(client, db, tenant, staff, menu):
    tenant.set_feature("restaurant_mode", True)
    save_module_settings(tenant, "restaurant", {"packing_charge": "10"})
    db.session.commit()
    _cashier(client)
    cfg = _config(client.get("/app/bill").get_data(as_text=True))
    values = [t["value"] for t in cfg["order_types"]]
    assert "dine_in" not in values and cfg["order_type"] == "takeaway"
    assert cfg["quote_url"] == "/pos/quote" and "takeaway" in cfg["packing_on"]
    res = client.post("/pos/checkout", data=_app_bill(menu["tea"], 1, order_type="takeaway"),
                      headers={"X-Requested-With": "XMLHttpRequest"})
    assert res.get_json()["grand_total"] > 20  # the packing charge, added by the server


# --- the owner's side --------------------------------------------------------------------------------------

def test_the_owner_is_told_how_phones_bill_offline(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    assert "switch on <strong>Offline billing</strong>" in client.get("/branches/").get_data(as_text=True)
    tenant.set_feature("offline_pos", True)
    db.session.commit()
    assert "switch on <strong>Offline billing</strong>" not in client.get("/branches/").get_data(as_text=True)
