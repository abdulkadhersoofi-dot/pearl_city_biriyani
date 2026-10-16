"""Phase 10 offline billing (feature "offline_pos"): each counter device
has its own series (O1, O2...), numbers its bills itself while offline,
and the server books each queued bill exactly once, keeps what it can't
book for the Client Admin (retry / cancel with the number kept), never
double-bills a checkout whose reply was lost - plus the ESC/POS receipt
encoder and the receipt document for direct thermal printing."""

import json
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.audit_log import AuditLog
from app.models.invoice_series import InvoiceSeries
from app.models.platform import CounterDevice, OfflineBill, OfflineBillStatus
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus
from app.models.product import Product
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.utils.financial_year import financial_year_for

D = Decimal
ROOT = Path(__file__).resolve().parent.parent


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def shop(db, tenant, staff):
    tenant.set_feature("offline_pos", True)
    tea = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="0902", gst_rate=D("5"), unit="NOS",
                  sale_price=D("20"), price_includes_tax=True)
    soap = Product(tenant_id=tenant.id, name="Soap", hsn_or_sac_code="3401", gst_rate=D("18"), unit="NOS",
                   sale_price=D("45.50"))
    db.session.add_all([tea, soap])
    db.session.commit()
    return {"tea": tea, "soap": soap}


def _register(client):
    res = client.post("/pos/offline/register", json={"name": "Front counter"})
    assert res.status_code == 200, res.get_json()
    return res.get_json()


def _headers(dev):
    return {"X-Device-Id": str(dev["device_id"]), "X-Device-Token": dev["token"]}


def _bill(dev, seq, lines, mode="cash", total=None, billed_at=None, fy=None, **kw):
    fy = fy or dev["fy"]
    return {"uuid": kw.pop("uuid", str(uuid.uuid4())), "number": f"{dev['code']}/{fy}/{seq:04d}", "fy": fy, "seq": seq,
            "billed_at": (billed_at or datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "lines": lines, "payment_mode": mode, "total": total, **kw}


def _sync(client, dev, *bills):
    res = client.post("/pos/offline/sync", json={"bills": list(bills)}, headers=_headers(dev))
    assert res.status_code == 200, res.get_json()
    return res.get_json()


def test_device_gets_its_own_series_and_the_counter_series_is_untouched(client, db, tenant, staff, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    first = _register(client)
    second = _register(client)
    assert (first["code"], second["code"]) == ("O1", "O2")
    assert first["next_number"] == 1 and len(first["token"]) > 20
    device = db.session.get(CounterDevice, first["device_id"])
    assert first["token"] not in device.token_hash  # only a hash is kept
    assert device.user_id == staff.id and device.tenant_id == tenant.id
    assert AuditLog.query.filter_by(action="counter_device_registered").count() == 2

    # A normal online bill still takes the counter's own POS series.
    res = client.post("/pos/checkout", data={"line_product_id[]": str(shop["tea"].id), "line_description[]": "Tea",
                                             "line_hsn[]": "0902", "line_qty[]": "1", "line_rate[]": "20",
                                             "line_gst_rate[]": "5", "line_unit[]": "NOS", "payment_mode": "cash"},
                      headers={"X-Requested-With": "XMLHttpRequest"})
    assert res.get_json()["bill_number"].startswith("POS/")


def test_queued_bills_are_booked_once_with_the_device_number(client, db, tenant, staff, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    dev = _register(client)
    tea, soap = shop["tea"], shop["soap"]
    b1 = _bill(dev, 1, [{"product_id": tea.id, "qty": "2", "rate": "20"}], total="40.00", cash_tendered="50")
    b2 = _bill(dev, 2, [{"product_id": soap.id, "qty": "1", "rate": "45.50"}], mode="upi", total="54.00")
    out = _sync(client, dev, b1, b2)
    assert [r["status"] for r in out["results"]] == ["synced", "synced"]
    assert out["next_number"] == 3

    bill = POSBill.query.filter_by(bill_number=f"O1/{dev['fy']}/0001").one()
    assert bill.status == POSBillStatus.COMPLETED and bill.device.code == "O1"
    assert bill.created_by_id == staff.id and bill.grand_total == D("40.00")
    assert bill.total_taxable_value == D("38.10") and bill.total_cgst == D("0.95") and bill.total_sgst == D("0.95")
    assert bill.cash_tendered == D("50.00") and bill.change_due == D("10.00")
    assert bill.payments[0].mode == PaymentMode.CASH and bill.payments[0].amount == D("40.00")
    assert bill.original_printed_at is not None  # the device printed the original
    upi = POSBill.query.filter_by(bill_number=f"O1/{dev['fy']}/0002").one()
    assert upi.grand_total == D("54.00") and upi.payment_mode == PaymentMode.UPI  # 45.50 + 18% = 53.69 -> 54

    # Sent again (the reply was lost): nothing booked twice.
    again = _sync(client, dev, b1)
    assert again["results"][0]["status"] == "synced"
    assert POSBill.query.filter(POSBill.bill_number.like("O1/%")).count() == 2
    assert OfflineBill.query.count() == 2
    assert AuditLog.query.filter_by(action="pos_bill_synced_offline").count() == 2
    series = InvoiceSeries.query.filter_by(device_id=dev["device_id"]).one()
    assert series.prefix == "O1" and series.next_number == 3


def test_out_of_order_bills_move_the_series_forward_only(client, db, tenant, staff, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    dev = _register(client)
    line = [{"product_id": shop["tea"].id, "qty": "1", "rate": "20"}]
    assert _sync(client, dev, _bill(dev, 3, line, total="20.00"))["next_number"] == 4
    assert _sync(client, dev, _bill(dev, 1, line, total="20.00"))["next_number"] == 4
    state = client.get("/pos/offline/state", headers=_headers(dev)).get_json()
    assert state["next_number"] == 4 and state["csrf_token"]


def test_a_bill_the_server_cannot_book_is_kept_and_can_be_cancelled(client, db, tenant, staff, client_admin, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    dev = _register(client)
    gone = _bill(dev, 1, [{"product_id": 999999, "qty": "1", "rate": "20"}], total="20.00")
    out = _sync(client, dev, gone)
    assert out["results"][0]["status"] == "failed" and "catalog" in out["results"][0]["error"]
    record = OfflineBill.query.one()
    assert record.status == OfflineBillStatus.FAILED and record.pos_bill_id is None
    assert out["next_number"] == 1  # nothing claimed yet

    client.get("/auth/logout")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.get("/pos/offline")
    assert page.status_code == 200 and f"O1/{dev['fy']}/0001".encode() in page.data
    client.post("/pos/offline", data={"action": "void", "record_id": record.id, "reason": ""})
    db.session.refresh(record)
    assert record.status == OfflineBillStatus.FAILED  # a reason is required
    client.post("/pos/offline", data={"action": "void", "record_id": record.id, "reason": "Item deleted"})
    db.session.refresh(record)
    assert record.status == OfflineBillStatus.VOIDED
    cancelled = db.session.get(POSBill, record.pos_bill_id)
    assert cancelled.status == POSBillStatus.VOIDED and cancelled.bill_number == f"O1/{dev['fy']}/0001"
    assert "Item deleted" in cancelled.void_reason
    assert InvoiceSeries.query.filter_by(device_id=dev["device_id"]).one().next_number == 2  # no gap, no reuse
    assert AuditLog.query.filter_by(action="offline_bill_cancelled").count() == 1


def test_retry_books_a_failed_bill_once_the_problem_is_fixed(client, db, tenant, staff, client_admin, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    dev = _register(client)
    shop["soap"].has_variants = True  # can't be billed offline
    db.session.commit()
    _sync(client, dev, _bill(dev, 1, [{"product_id": shop["soap"].id, "qty": "1", "rate": "45.50"}], total="54.00"))
    record = OfflineBill.query.one()
    assert record.status == OfflineBillStatus.FAILED
    shop["soap"].has_variants = False
    db.session.commit()
    client.get("/auth/logout")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    client.post("/pos/offline", data={"action": "retry", "record_id": record.id})
    db.session.refresh(record)
    assert record.status == OfflineBillStatus.SYNCED and record.pos_bill.grand_total == D("54.00")
    assert record.resolved_by_id == client_admin.id


def test_wrong_clock_is_recorded_not_lost(client, db, tenant, staff, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    dev = _register(client)
    future = _bill(dev, 1, [{"product_id": shop["tea"].id, "qty": "1", "rate": "20"}], total="20.00",
                   billed_at=datetime.now(timezone.utc) + timedelta(days=2))
    out = _sync(client, dev, future)
    assert out["results"][0]["status"] == "failed" and "future" in out["results"][0]["error"]
    assert OfflineBill.query.count() == 1


def test_total_mismatch_is_booked_and_flagged(client, db, tenant, staff, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    dev = _register(client)
    _sync(client, dev, _bill(dev, 1, [{"product_id": shop["tea"].id, "qty": "1", "rate": "20"}], total="25.00"))
    record = OfflineBill.query.one()
    assert record.status == OfflineBillStatus.SYNCED and record.total_mismatch
    assert record.pos_bill.grand_total == D("20.00")


def test_a_number_outside_the_device_series_is_refused(client, db, tenant, staff, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    dev = _register(client)
    bad = _bill(dev, 1, [{"product_id": shop["tea"].id, "qty": "1", "rate": "20"}], total="20.00")
    bad["number"] = f"POS/{dev['fy']}/0001"
    out = _sync(client, dev, bad)
    assert out["results"][0]["status"] == "rejected"
    assert OfflineBill.query.count() == 0 and POSBill.query.count() == 0


def test_only_plain_payments_are_booked_offline(client, db, tenant, staff, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    dev = _register(client)
    out = _sync(client, dev, _bill(dev, 1, [{"product_id": shop["tea"].id, "qty": "1", "rate": "20"}],
                                   mode="credit", total="20.00"))
    assert out["results"][0]["status"] == "failed" and "cash, UPI or card" in out["results"][0]["error"]


def test_stock_goes_out_even_past_zero(client, db, tenant, staff, shop):
    from app.inventory import ledger

    tenant.set_feature("inventory", True)
    db.session.commit()
    shop["tea"].allow_negative_stock = False
    db.session.commit()
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    dev = _register(client)
    out = _sync(client, dev, _bill(dev, 1, [{"product_id": shop["tea"].id, "qty": "3", "rate": "20"}], total="60.00"))
    assert out["results"][0]["status"] == "synced"
    loc = ledger.location_for_user(tenant.id, staff)
    assert ledger.stock_on_hand(tenant.id, [shop["tea"].id], loc.id).get(shop["tea"].id) == D("-3")


def test_a_checkout_retried_after_a_lost_reply_is_not_billed_twice(client, db, tenant, staff, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    data = {"line_product_id[]": str(shop["tea"].id), "line_description[]": "Tea", "line_hsn[]": "0902",
            "line_qty[]": "1", "line_rate[]": "20", "line_gst_rate[]": "5", "line_unit[]": "NOS",
            "payment_mode": "cash", "checkout_uuid": "3f2a9c1e-0000-4000-8000-000000000001"}
    first = client.post("/pos/checkout", data=data, headers={"X-Requested-With": "XMLHttpRequest"}).get_json()
    second = client.post("/pos/checkout", data=data, headers={"X-Requested-With": "XMLHttpRequest"}).get_json()
    assert first["bill_id"] == second["bill_id"] and second["repeat"]
    assert POSBill.query.count() == 1

    # The same sale also queued offline (the reply never arrived): flagged, not booked.
    dev = _register(client)
    out = _sync(client, dev, _bill(dev, 1, [{"product_id": shop["tea"].id, "qty": "1", "rate": "20"}],
                                   total="20.00", uuid="3f2a9c1e-0000-4000-8000-000000000001"))
    assert out["results"][0]["status"] == "failed" and first["bill_number"] in out["results"][0]["error"]
    assert POSBill.query.count() == 1


def test_device_belongs_to_its_login_and_tenant(app, client, db, tenant, staff, shop, super_admin):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    dev = _register(client)
    client.get("/auth/logout")

    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY, features={"offline_pos": True})
    db.session.add(other)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other.id, gstin="29AAAAA0000A1Z5", state_code="29", state_name="Karnataka",
                         is_primary=True))
    intruder = User(tenant_id=other.id, name="Other", email="o@other.example.com", role=UserRole.STAFF,
                    must_change_password=False)
    intruder.set_password("OtherSecret123")
    db.session.add(intruder)
    db.session.commit()
    _login(client, "o@other.example.com", "OtherSecret123")
    # Guessing the device id - even with its token - gets nothing.
    assert client.get("/pos/offline/state", headers=_headers(dev)).status_code == 403
    res = client.post("/pos/offline/sync", json={"bills": [_bill(dev, 1, [{"product_id": shop["tea"].id, "qty": "1",
                                                                           "rate": "20"}], total="20.00")]},
                      headers=_headers(dev))
    assert res.status_code == 403
    assert POSBill.query.count() == 0 and OfflineBill.query.count() == 0
    # Their own product ids can't be billed through a device of theirs either.
    own = _register(client)
    out = _sync(client, own, _bill(own, 1, [{"product_id": shop["tea"].id, "qty": "1", "rate": "20"}], total="20.00"))
    assert out["results"][0]["status"] == "failed"
    assert POSBill.query.filter_by(tenant_id=other.id).count() == 0


def test_offline_admin_page_hides_other_tenants_rows(client, db, tenant, staff, client_admin, shop, super_admin):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY, features={"offline_pos": True})
    db.session.add(other)
    db.session.flush()
    branch = User(tenant_id=other.id, name="Other", email="o@other.example.com", role=UserRole.STAFF,
                  must_change_password=False)
    branch.set_password("x")
    db.session.add(branch)
    db.session.flush()
    device = CounterDevice(tenant_id=other.id, user_id=branch.id, code="O1", name="Their till", token_hash="0" * 64)
    db.session.add(device)
    db.session.flush()
    rec = OfflineBill(tenant_id=other.id, device_id=device.id, client_uuid="abcdefgh-1", bill_number="O1/2026-27/0099",
                      billed_at=datetime.now(timezone.utc), payload={"total": "1"}, status=OfflineBillStatus.FAILED)
    db.session.add(rec)
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.get("/pos/offline")
    assert b"Their till" not in page.data and b"O1/2026-27/0099" not in page.data
    assert client.post("/pos/offline", data={"action": "void", "record_id": rec.id, "reason": "x"}).status_code == 404
    assert client.post("/pos/offline", data={"action": "device_off", "device_id": device.id}).status_code == 404
    db.session.refresh(rec)
    db.session.refresh(device)
    assert rec.status == OfflineBillStatus.FAILED and device.is_active


def test_switched_off_device_can_no_longer_send(client, db, tenant, staff, client_admin, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    dev = _register(client)
    client.get("/auth/logout")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    client.post("/pos/offline", data={"action": "device_off", "device_id": dev["device_id"]})
    client.get("/auth/logout")
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    res = client.get("/pos/offline/state", headers=_headers(dev))
    assert res.status_code == 403 and "switched off" in res.get_json()["error"]


def test_everything_is_hidden_with_the_flag_off(client, db, tenant, staff):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    assert client.post("/pos/offline/register", json={}).status_code == 404
    assert client.get("/pos/offline/state").status_code == 404
    assert client.post("/pos/offline/sync", json={}).status_code == 404
    page = client.get("/pos/").data
    assert b"js/pos_offline.js" not in page and b'"sw_url"' not in page


def test_terminal_carries_the_offline_setup(client, db, tenant, staff, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    page = client.get("/pos/").data.decode()
    assert "pos_offline.js" in page and '"sw_url": "/pos/sw.js"' in page
    assert '"modes": ["cash", "upi", "card"]' in page


def test_service_worker_is_served_for_the_pos_scope(client):
    res = client.get("/pos/sw.js")
    assert res.status_code == 200 and res.headers["Service-Worker-Allowed"] == "/pos/"
    assert b"arfa-pos-v1" in res.data


# --- receipt document + ESC/POS ------------------------------------------------------------------

def test_receipt_json_is_the_original_once_then_duplicate(client, db, tenant, staff, shop):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    res = client.post("/pos/checkout", data={"line_product_id[]": str(shop["tea"].id), "line_description[]": "Tea",
                                             "line_hsn[]": "0902", "line_qty[]": "2", "line_rate[]": "20",
                                             "line_gst_rate[]": "5", "line_unit[]": "NOS", "payment_mode": "cash"},
                      headers={"X-Requested-With": "XMLHttpRequest"}).get_json()
    doc = client.get(f"/pos/bills/{res['bill_id']}/receipt.json?original=1").get_json()
    assert doc["copy_label"] is None and doc["open_drawer"] is True
    assert doc["number"] == res["bill_number"] and doc["grand"] == ["TOTAL", "40.00"]
    assert doc["lines"][0]["qty"] == "2 NOS x 20.00" and ["CGST", "0.95"] in doc["totals"]
    again = client.get(f"/pos/bills/{res['bill_id']}/receipt.json?original=1").get_json()
    assert again["copy_label"] == "DUPLICATE COPY" and again["open_drawer"] is False


node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run_node(script: str) -> str:
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    return result.stdout


@node
def test_escpos_encodes_a_receipt_for_both_paper_widths():
    doc = {"header": ["Spice Route Kitchen", "GSTIN: 27AAAAA0000A1Z5"], "copy_label": None, "number": "POS/2026-27/0007",
           "title": None, "meta": [["Date", "16 Oct 2026 19:05"], ["Cashier", "Meena"]],
           "lines": [{"desc": "Chicken Biriyani Full Plate Extra Raita And Salan", "details": ["* less spicy"],
                      "qty": "2 NOS x 150.00", "amount": "300.00", "discount": None}],
           "totals": [["Taxable value", "285.71"], ["CGST", "7.14"], ["SGST", "7.15"], ["Round off", "0.00"]],
           "grand": ["TOTAL", "300.00"], "payments": [["Paid via", "CASH"]], "status": None,
           "footer": ["Thank you, visit again! ₹"], "qr": None, "open_drawer": True}
    script = f"""
      require('./app/static/js/escpos.js');
      const doc = {json.dumps(doc)};
      const out = {{}};
      [32, 48].forEach(cols => {{
        const bytes = EscPos.encode(doc, cols);
        const text = Buffer.from(bytes).toString('latin1');
        const lines = text.split('\\n');
        out[cols] = {{
          start: Array.from(bytes.slice(0, 2)), end: Array.from(bytes.slice(-4)),
          maxPlain: Math.max(...lines.map(l => l.replace(/[\\x00-\\x1f]./g, '').replace(/[\\x00-\\x1f]/g, '').length)),
          hasTotal: text.includes('TOTAL'), ascii: Array.from(bytes).every(b => b < 0x80 || b === 0xfa),
          b64: EscPos.toBase64(bytes).length > 0, wrapped: text.includes('Extra Raita'),
        }};
      }});
      out.kick = Array.from(EscPos.drawerKick());
      out.pair = EscPos.pair('Taxable value', '285.71', 32);
      out.html = EscPos.html(doc).includes('POS/2026-27/0007');
      console.log(JSON.stringify(out));
    """
    out = json.loads(_run_node(script))
    for cols in ("32", "48"):
        assert out[cols]["start"] == [0x1b, 0x40]  # ESC @ initialise
        assert out[cols]["end"] == [0x1d, 0x56, 0x42, 0x00]  # GS V B 0: feed and cut
        assert out[cols]["hasTotal"] and out[cols]["ascii"] and out[cols]["b64"] and out[cols]["wrapped"]
    assert out["kick"] == [0x1b, 0x70, 0x00, 0x19, 0xfa]
    assert out["pair"] == ["Taxable value" + " " * (32 - 13 - 6) + "285.71"]
    assert out["html"]


@node
def test_offline_numbers_follow_the_indian_financial_year():
    script = """
      global.window = global; global.navigator = {onLine: true, userAgent: 'node'};
      global.localStorage = {getItem: () => null, setItem: () => {}};
      require('./app/static/js/gst_calc.js'); require('./app/static/js/pos_offline.js');
      console.log(JSON.stringify([ArfaOffline.financialYear(new Date(2026, 2, 31)),
                                  ArfaOffline.financialYear(new Date(2026, 3, 1)),
                                  ArfaOffline.blocker([], {})]));
    """
    fy_march, fy_april, blocked = json.loads(_run_node(script))
    assert (fy_march, fy_april) == ("2025-26", "2026-27")
    assert fy_april == financial_year_for(datetime(2026, 4, 1).date())
    assert blocked  # nothing billed offline before the device is set up
