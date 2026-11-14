"""Warehouse dispatch, stages 2 and 4: the ARFA Warehouse app's API
(/api/wh/v1). Sign-in once per phone and the PIN; a login sees only its own
warehouses; two phones claiming at once - exactly one wins; only the
claimer scans and confirms; every scan is checked on the server (and the
same scan sent twice is one pick); idle claims go back; pushes reach every
phone of that warehouse and no other."""

import threading
from datetime import timedelta

import pytest

from app.extensions import db as _db
from app.models.inventory import ProductSerial, SerialStatus
from app.models.mixins import utcnow
from app.models.sales import SalesDocument
from app.models.warehouse import (
    DispatchStatus, PickResult, SerialPick, WarehouseLogin, WarehouseNotice, WarehouseSession,
)
from app.warehouse import dispatch, notify
from tests.test_warehouse_dispatch import buy, line, order, wh  # noqa: F401

API = "/api/wh/v1"


def make_login(w, name, login_id, *locations, password="Picker@123", pin="2580", manual=False):
    person = WarehouseLogin(tenant_id=w.tenant.id, name=name, login_id=login_id, manual_serial=manual)
    person.set_password(password)
    if pin:
        person.set_pin(pin)
    person.locations = list(locations)
    _db.session.add(person)
    _db.session.commit()
    return person


def sign_in(client, w, login_id, password="Picker@123", device="Redmi 12"):
    return client.post(f"{API}/auth/login", json={"business_code": str(w.tenant.id), "login_id": login_id,
                                                  "password": password, "device_name": device,
                                                  "app_version": "1.1"})


def auth(client, w, login_id, **kw):
    body = sign_in(client, w, login_id, **kw).get_json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body


@pytest.fixture()
def pickers(wh):
    ravi = make_login(wh, "Ravi", "ravi", wh.a)
    siva = make_login(wh, "Siva", "siva", wh.a)
    bala = make_login(wh, "Bala", "bala", wh.b)
    return ravi, siva, bala


# --- signing in ---------------------------------------------------------------------------------------

def test_sign_in_once_then_the_pin_opens_the_app(client, wh, pickers):
    res = sign_in(client, wh, "RAVI")                                     # login ids aren't case-sensitive
    body = res.get_json()
    assert res.status_code == 200 and body["refresh_token"] and body["access_token"]
    assert body["warehouses"] == [{"id": wh.a.id, "name": "Warehouse A"}] and body["pin_set"]
    session = WarehouseSession.query.one()
    assert session.token_hash != body["refresh_token"] and session.device_name == "Redmi 12"
    old = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get(f"{API}/me", headers=old).status_code == 200
    unlocked = client.post(f"{API}/auth/pin", json={"refresh_token": body["refresh_token"], "pin": "2580"}).get_json()
    assert client.get(f"{API}/me", headers=old).get_json()["pin_required"]    # the PIN replaced the old token
    assert client.get(f"{API}/me", headers={"Authorization": f"Bearer {unlocked['access_token']}"}).status_code == 200


def test_wrong_passwords_lock_and_wrong_pins_sign_the_phone_out(client, wh, pickers, app):
    assert sign_in(client, wh, "ravi", password="nope").status_code == 401
    assert sign_in(client, wh, "ghost").status_code == 401
    assert client.post(f"{API}/auth/login", json={"business_code": "9999", "login_id": "ravi",
                                                  "password": "Picker@123"}).status_code == 401
    for _ in range(app.config["LOGIN_LOCKOUT_ATTEMPTS"] - 1):
        sign_in(client, wh, "ravi", password="nope")
    assert sign_in(client, wh, "ravi").status_code == 423                  # locked, even with the right one
    siva_refresh = sign_in(client, wh, "siva").get_json()["refresh_token"]
    for left in (4, 3, 2, 1):
        res = client.post(f"{API}/auth/pin", json={"refresh_token": siva_refresh, "pin": "0000"})
        assert res.status_code == 401 and res.get_json()["tries_left"] == left
    res = client.post(f"{API}/auth/pin", json={"refresh_token": siva_refresh, "pin": "0000"})
    assert res.get_json()["signed_out"]
    assert client.post(f"{API}/auth/pin", json={"refresh_token": siva_refresh, "pin": "2580"}).status_code == 401


def test_a_new_login_chooses_its_pin(client, wh):
    make_login(wh, "Kumar", "kumar", wh.a, pin=None)
    headers, body = auth(client, wh, "kumar")
    assert body["pin_set"] is False
    assert client.post(f"{API}/auth/pin", json={"refresh_token": body["refresh_token"], "pin": "1234"}).status_code == 409
    assert client.put(f"{API}/auth/pin", json={"pin": "12"}, headers=headers).status_code == 400
    assert client.put(f"{API}/auth/pin", json={"pin": "4321"}, headers=headers).get_json()["pin_set"]
    assert client.put(f"{API}/auth/pin", json={"pin": "9999"}, headers=headers).status_code == 401  # needs the old one
    assert client.post(f"{API}/auth/pin", json={"refresh_token": body["refresh_token"], "pin": "4321"}).status_code == 200


def test_switched_off_reset_or_signed_out_ends_the_session(client, wh, pickers):
    ravi = pickers[0]
    headers, body = auth(client, wh, "ravi")
    session = WarehouseSession.query.one()
    session.revoked_at = utcnow()
    _db.session.commit()
    assert client.get(f"{API}/me", headers=headers).get_json()["signed_out"]
    headers, body = auth(client, wh, "ravi")
    ravi.is_active = False
    _db.session.commit()
    assert client.get(f"{API}/me", headers=headers).status_code == 401
    assert sign_in(client, wh, "ravi").status_code == 401


def test_a_paused_business_or_the_module_off_is_turned_away(client, wh, pickers):
    headers, _ = auth(client, wh, "ravi")
    wh.tenant.set_feature("warehouse_dispatch", False)
    _db.session.commit()
    assert client.get(f"{API}/me", headers=headers).status_code == 403
    wh.tenant.set_feature("warehouse_dispatch", True)
    wh.tenant.is_active = False
    _db.session.commit()
    res = client.get(f"{API}/pipeline", headers=headers)
    assert res.status_code == 403 and "paused" in res.get_json()["error"]


# --- the pipeline -----------------------------------------------------------------------------------

def test_a_login_sees_only_its_own_warehouses_orders(client, wh, pickers):
    at_a = order(wh)
    at_b = order(wh, location=wh.b)
    ravi, _ = auth(client, wh, "ravi")
    bala, _ = auth(client, wh, "bala")
    cards = client.get(f"{API}/pipeline", headers=ravi).get_json()["orders"]
    assert [c["number"] for c in cards] == [at_a.number] and cards[0]["units"] == 2 and cards[0]["branch"] == "Office"
    assert client.get(f"{API}/pipeline?location_id={wh.b.id}", headers=ravi).status_code == 403
    assert client.get(f"{API}/orders/{at_b.id}", headers=ravi).status_code == 404
    assert client.post(f"{API}/orders/{at_b.id}/claim", headers=ravi).status_code == 404
    assert [c["number"] for c in client.get(f"{API}/pipeline", headers=bala).get_json()["orders"]] == [at_b.number]


def test_the_order_comes_with_the_serials_in_stock_there(client, wh, pickers):
    doc = order(wh)
    ravi, _ = auth(client, wh, "ravi")
    detail = client.get(f"{API}/orders/{doc.id}", headers=ravi).get_json()
    first = detail["lines"][0]
    assert first["product"]["model_no"] == "AR18TY3QBWK" and first["needs_scan"]
    assert first["allowed_serials"] == ["ACA1", "ACA2", "ACA3", "ACA4", "ACA5"] and not first["truncated"]


def test_two_phones_claim_at_once_and_exactly_one_wins(app, client, wh, pickers):
    doc = order(wh)
    tokens = [auth(app.test_client(), wh, name)[0] for name in ("ravi", "siva")]
    results = []

    def tap(headers):
        with app.app_context():
            res = app.test_client().post(f"{API}/orders/{doc.id}/claim", headers=headers)
            results.append((res.status_code, res.get_json()))
            _db.session.remove()

    threads = [threading.Thread(target=tap, args=(h,)) for h in tokens]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(code for code, _ in results)
    assert codes == [200, 409]
    loser = next(body for code, body in results if code == 409)
    winner = next(body for code, body in results if code == 200)
    assert loser["error"] == f"Already taken by {winner['claimed_by']['name']}."
    _db.session.expire_all()
    assert _db.session.get(SalesDocument, doc.id).dispatch_status == DispatchStatus.PICKING


def test_the_claimer_scans_others_cant(client, wh, pickers):
    doc = order(wh)
    ravi, _ = auth(client, wh, "ravi")
    siva, _ = auth(client, wh, "siva")
    client.post(f"{API}/orders/{doc.id}/claim", headers=ravi)
    card = next(c for c in client.get(f"{API}/pipeline", headers=siva).get_json()["orders"] if c["id"] == doc.id)
    assert card["claimed_by"]["name"] == "Ravi" and not card["mine"]
    line_id = doc.lines[0].id
    res = client.post(f"{API}/orders/{doc.id}/lines/{line_id}/picks", json={"serial_no": "ACA1"}, headers=siva)
    assert res.status_code == 403
    assert client.post(f"{API}/orders/{doc.id}/confirm", headers=siva).status_code == 403
    assert client.post(f"{API}/orders/{doc.id}/release", headers=siva).status_code == 403


def _pick(client, headers, doc, serial, **extra):
    return client.post(f"{API}/orders/{doc.id}/lines/{doc.lines[0].id}/picks",
                       json={"serial_no": serial, "method": "barcode", **extra}, headers=headers)


def test_each_scan_is_checked_and_kept(client, wh, pickers, db):
    other = order(wh, lines=[line(wh.fridge, "1")])
    doc = order(wh)
    ravi, _ = auth(client, wh, "ravi")
    client.post(f"{API}/orders/{doc.id}/claim", headers=ravi)
    sold = ProductSerial.query.filter_by(serial_no="ACA5").one()
    sold.status = SerialStatus.SOLD
    db.session.commit()
    expect = [("FRA1", "wrong_product", "This is Fridge 260L, the order needs LG AC 1.5T"),
              ("ACB1", "wrong_location", "ACB1 is at Warehouse B"),
              ("ACA5", "not_in_stock", "isn't in stock"),
              ("NOSUCH", "unknown", "isn't in ARFA"),
              ("aca1", "ok", "ACA1 picked"),                                # case and spaces don't matter
              ("ACA1", "duplicate", "already scanned on this order")]
    for serial, result, message in expect:
        pick = _pick(client, ravi, doc, serial).get_json()["pick"]
        assert (pick["result"], message in pick["message"]) == (result, True), (serial, pick)
    assert SerialPick.query.filter_by(sales_document_id=doc.id).count() == 6     # failures are kept too
    unit = ProductSerial.query.filter_by(serial_no="ACA1").one()
    assert unit.status == SerialStatus.PICKED and unit.picked_sales_line_id == doc.lines[0].id
    assert unit.picked_by_id == pickers[0].id
    _pick(client, ravi, doc, "ACA2")
    assert _pick(client, ravi, doc, "ACA3").get_json()["pick"]["result"] == "line_full"
    assert other.dispatch_status == DispatchStatus.TO_PICK


def test_a_serial_never_entered_is_added_when_the_picker_says_the_box_is_right(client, wh, pickers):
    doc = order(wh)
    ravi, _ = auth(client, wh, "ravi")
    client.post(f"{API}/orders/{doc.id}/claim", headers=ravi)
    assert _pick(client, ravi, doc, "NEWAC77").get_json()["pick"]["result"] == "unknown"
    res = _pick(client, ravi, doc, "NEWAC77", allow_new=True)
    assert res.status_code == 201 and res.get_json()["pick"]["result"] == "added"
    unit = ProductSerial.query.filter_by(serial_no="NEWAC77").one()
    assert unit.added_at_pick and unit.status == SerialStatus.PICKED and unit.location_id == wh.a.id
    assert unit.product_id == wh.ac.id
    assert _pick(client, ravi, doc, "AR18TY3QBWK", allow_new=True).get_json()["pick"]["result"] == "unknown"  # model no.
    pick = SerialPick.query.filter_by(serial_no="NEWAC77", result=PickResult.ADDED).one()
    assert client.delete(f"{API}/picks/{pick.id}", headers=ravi).status_code == 200
    assert ProductSerial.query.filter_by(serial_no="NEWAC77").count() == 0       # undone: never was in ARFA


def test_the_same_scan_sent_twice_is_one_pick(client, wh, pickers):
    doc = order(wh)
    ravi, _ = auth(client, wh, "ravi")
    client.post(f"{API}/orders/{doc.id}/claim", headers=ravi)
    first = _pick(client, ravi, doc, "ACA2", client_pick_id="phone1-0001")
    again = _pick(client, ravi, doc, "ACA2", client_pick_id="phone1-0001")
    assert first.status_code == 201 and again.get_json()["replayed"]
    assert again.get_json()["pick"]["id"] == first.get_json()["pick"]["id"]
    assert SerialPick.query.count() == 1


def test_confirm_only_when_every_unit_is_scanned(client, wh, pickers):
    doc = order(wh)
    ravi, _ = auth(client, wh, "ravi")
    client.post(f"{API}/orders/{doc.id}/claim", headers=ravi)
    _pick(client, ravi, doc, "ACA1")
    res = client.post(f"{API}/orders/{doc.id}/confirm", headers=ravi)
    assert res.status_code == 409 and res.get_json()["missing"] == 1
    _pick(client, ravi, doc, "ACA2")
    res = client.post(f"{API}/orders/{doc.id}/confirm", headers=ravi)
    assert res.status_code == 200 and "Sent to Warehouse A computer" in res.get_json()["message"]
    assert doc.dispatch_status == DispatchStatus.PICKED and doc.picked_confirmed_by_id == pickers[0].id
    assert client.get(f"{API}/pipeline", headers=ravi).get_json()["orders"] == []
    done = client.get(f"{API}/pipeline?tab=done", headers=ravi).get_json()["orders"]
    assert [c["id"] for c in done] == [doc.id]
    # Undo after confirming takes it back to picking.
    pick = SerialPick.query.filter_by(serial_no="ACA2").one()
    client.delete(f"{API}/picks/{pick.id}", headers=ravi)
    assert doc.dispatch_status == DispatchStatus.PICKING


def test_release_undoes_the_scans_and_two_pickers_racing_for_one_serial(client, app, wh, pickers):
    doc = order(wh, lines=[line(wh.ac, "1")])
    doc2 = order(wh, lines=[line(wh.ac, "1")])
    ravi, _ = auth(client, wh, "ravi")
    siva, _ = auth(client, wh, "siva")
    client.post(f"{API}/orders/{doc.id}/claim", headers=ravi)
    client.post(f"{API}/orders/{doc2.id}/claim", headers=siva)
    results = []

    def scan(headers, d):
        with app.app_context():
            res = app.test_client().post(f"{API}/orders/{d.id}/lines/{d.lines[0].id}/picks",
                                         json={"serial_no": "ACA3"}, headers=headers)
            results.append(res.get_json()["pick"]["result"])
            _db.session.remove()

    threads = [threading.Thread(target=scan, args=a) for a in ((ravi, doc), (siva, doc2))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["duplicate", "ok"]
    _db.session.expire_all()
    holder = doc if ProductSerial.query.filter_by(serial_no="ACA3").one().picked_sales_line_id == doc.lines[0].id else doc2
    headers = ravi if holder is doc else siva
    client.post(f"{API}/orders/{holder.id}/release", headers=headers)
    _db.session.expire_all()
    unit = ProductSerial.query.filter_by(serial_no="ACA3").one()
    assert unit.status == SerialStatus.IN_STOCK and unit.picked_sales_line_id is None
    assert _db.session.get(SalesDocument, holder.id).dispatch_status == DispatchStatus.TO_PICK


def test_a_claim_idle_for_30_minutes_goes_back(client, wh, pickers):
    doc = order(wh)
    ravi, _ = auth(client, wh, "ravi")
    client.post(f"{API}/orders/{doc.id}/claim", headers=ravi)
    _pick(client, ravi, doc, "ACA1")
    doc.dispatch_activity_at = utcnow() - timedelta(minutes=29)
    _db.session.commit()
    client.get(f"{API}/pipeline", headers=ravi)
    assert doc.dispatch_status == DispatchStatus.PICKING
    doc.dispatch_activity_at = utcnow() - timedelta(minutes=31)
    _db.session.commit()
    card = client.get(f"{API}/pipeline", headers=ravi).get_json()["orders"][0]
    assert card["status"] == "to_pick" and card["claimed_by"] is None
    assert ProductSerial.query.filter_by(serial_no="ACA1").one().status == SerialStatus.IN_STOCK


def test_cant_find_goes_back_to_the_showroom_as_a_note(client, wh, pickers):
    doc = order(wh)
    ravi, _ = auth(client, wh, "ravi")
    client.post(f"{API}/orders/{doc.id}/claim", headers=ravi)
    res = client.post(f"{API}/orders/{doc.id}/short", json={"line_id": doc.lines[0].id, "reason": "only 1 on the rack"},
                      headers=ravi)
    assert res.status_code == 200 and "Warehouse A (Ravi): LG AC 1.5T - only 1 on the rack" in doc.notes


def test_a_label_photo_is_kept_with_the_pick(client, wh, pickers):
    from app.models.media import UploadedImage

    doc = order(wh)
    ravi, _ = auth(client, wh, "ravi")
    client.post(f"{API}/orders/{doc.id}/claim", headers=ravi)
    pick_id = _pick(client, ravi, doc, "ACA1").get_json()["pick"]["id"]
    import io

    bad = client.post(f"{API}/picks/{pick_id}/photo", headers=ravi,
                      data={"photo": (io.BytesIO(b"GIF89a"), "x.gif")}, content_type="multipart/form-data")
    assert bad.status_code == 415
    good = client.post(f"{API}/picks/{pick_id}/photo", headers=ravi,
                       data={"photo": (io.BytesIO(b"\xff\xd8\xff\xe0 jpeg"), "label.jpg")},
                       content_type="multipart/form-data")
    assert good.status_code == 200
    image = _db.session.get(UploadedImage, good.get_json()["photo_id"])
    assert image.is_private and ProductSerial.query.filter_by(serial_no="ACA1").one().pick_photo_id == image.id


def test_the_label_reader_is_only_a_suggestion(client, wh, pickers, monkeypatch, app):
    import io
    import json

    from app.scan.engine import reader

    app.config.update(GEMINI_API_KEY="test-key")
    monkeypatch.setattr(reader, "call_model", lambda parts, *a, **k: (json.dumps(
        {"model_no": "AR18TY3QBWK", "serial_no": "ACA4", "confidence": 0.9}), "m"))
    monkeypatch.setattr(reader, "compress", lambda data, *a, **k: data)
    doc = order(wh)
    ravi, _ = auth(client, wh, "ravi")
    res = client.post(f"{API}/label/extract", headers=ravi, content_type="multipart/form-data",
                      data={"photo": (io.BytesIO(b"\xff\xd8\xff label"), "l.jpg"), "order_id": str(doc.id)})
    assert res.get_json()["serial_no"] == "ACA4" and res.get_json()["model_no"] == "AR18TY3QBWK"
    assert ProductSerial.query.filter_by(serial_no="ACA4").one().status == SerialStatus.IN_STOCK  # nothing picked


# --- pushes ---------------------------------------------------------------------------------------------

def test_a_new_order_rings_every_phone_of_that_warehouse_only(client, wh, pickers, app, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "deliver", lambda project, payload: (sent.append(payload["message"]), (True, ""))[1])
    app.config["FCM_PROJECT_ID"] = "arfa-test"
    for name, token in (("ravi", "tok-ravi"), ("siva", "tok-siva"), ("bala", "tok-bala")):
        headers, _ = auth(client, wh, name)
        client.put(f"{API}/auth/fcm-token", json={"token": token}, headers=headers)
    doc = order(wh)
    notify.send_pending()
    assert sorted(m["token"] for m in sent) == ["tok-ravi", "tok-siva"]
    assert sent[0]["data"]["kind"] == "new_order" and sent[0]["data"]["silent"] == "0"
    assert sent[0]["android"]["priority"] == "high" and doc.number in sent[0]["data"]["title"]
    assert WarehouseNotice.query.one().sent_at is not None and WarehouseNotice.query.one().recipients == 2
    sent.clear()
    ravi_headers = {"Authorization": "Bearer " + client.post(f"{API}/auth/pin", json={
        "refresh_token": sign_in(client, wh, "ravi").get_json()["refresh_token"], "pin": "2580"}).get_json()["access_token"]}
    client.post(f"{API}/orders/{doc.id}/claim", headers=ravi_headers)       # sends straight after the commit
    assert {m["data"]["kind"] for m in sent} == {"claimed"} and all(m["data"]["silent"] == "1" for m in sent)
    assert "tok-bala" not in {m["token"] for m in sent}


def test_a_dead_phone_token_is_dropped_and_a_failure_retried(client, wh, pickers, app, monkeypatch):
    app.config["FCM_PROJECT_ID"] = "arfa-test"
    headers, _ = auth(client, wh, "ravi")
    client.put(f"{API}/auth/fcm-token", json={"token": "dead"}, headers=headers)
    monkeypatch.setattr(notify, "deliver", lambda project, payload: (False, "UNREGISTERED"))
    order(wh)
    notify.send_pending()
    assert WarehouseSession.query.one().fcm_token is None
    client.put(f"{API}/auth/fcm-token", json={"token": "alive"}, headers=headers)
    monkeypatch.setattr(notify, "deliver", lambda project, payload: (False, "503 busy"))
    order(wh, lines=[line(wh.ac, "1")])
    notify.send_pending()
    notice = WarehouseNotice.query.order_by(WarehouseNotice.id.desc()).first()
    assert notice.sent_at is None and notice.attempts == 1 and "503" in notice.last_error
    monkeypatch.setattr(notify, "deliver", lambda project, payload: (True, ""))
    notify.send_pending()
    assert notice.sent_at is not None


def test_an_order_rolled_back_sends_nothing(client, wh, pickers, app, monkeypatch):
    sent = []
    app.config["FCM_PROJECT_ID"] = "arfa-test"
    monkeypatch.setattr(notify, "deliver", lambda project, payload: (sent.append(1), (True, ""))[1])
    order(wh, commit=False)
    _db.session.rollback()
    notify.send_pending()
    assert sent == [] and WarehouseNotice.query.count() == 0


def test_the_app_config_and_update_check(client, wh, pickers, app):
    assert client.get(f"{API}/app/config").get_json()["push"] is None
    app.config["WAREHOUSE_FCM_CONFIG"] = ('{"app_id": "1:695809637488:android:abc", "api_key": "k", '
                                          '"project_id": "arfa-gst-prod-1", "sender_id": "695809637488"}')
    assert client.get(f"{API}/app/config").get_json()["push"]["project_id"] == "arfa-gst-prod-1"
    headers, _ = auth(client, wh, "ravi")
    assert client.get(f"{API}/app/version", headers=headers).get_json()["versionCode"] == 0


# --- the website: logins and supervising -------------------------------------------------------------

def _web(client, w):
    client.post("/auth/login", data={"email": w.admin.email, "password": "ClientSecret123"})


def test_the_owner_adds_a_login_and_it_counts_against_the_package(client, wh):
    _web(client, wh)
    page = client.get("/warehouse/logins").get_data(as_text=True)
    assert "Business code" in page and str(wh.tenant.id) in page
    res = client.post("/warehouse/logins", data={"name": "Ravi", "login_id": "ravi", "password": "Picker@123",
                                                 "location_ids": [str(wh.a.id)]}, follow_redirects=True)
    assert "can now sign in" in res.get_data(as_text=True)
    assert sign_in(client, wh, "ravi").status_code == 200
    page = client.post("/warehouse/logins", data={"name": "Ravi 2", "login_id": "RAVI", "password": "Picker@123",
                                                  "location_ids": [str(wh.a.id)]}, follow_redirects=True)
    assert "already a login" in page.get_data(as_text=True)
    from app.utils import entitlements

    assert entitlements.limit_usage(wh.tenant, "warehouse_logins") == 1


def test_switching_a_login_off_signs_it_out_and_releases_its_order(client, wh, pickers):
    doc = order(wh)
    headers, _ = auth(client, wh, "ravi")
    client.post(f"{API}/orders/{doc.id}/claim", headers=headers)
    _web(client, wh)
    client.post(f"/warehouse/logins/{pickers[0].id}/toggle")
    assert client.get(f"{API}/me", headers=headers).status_code == 401
    assert doc.dispatch_status == DispatchStatus.TO_PICK


def test_a_supervisor_reassigns_or_releases_a_claim(client, wh, pickers):
    doc = order(wh)
    headers, _ = auth(client, wh, "ravi")
    client.post(f"{API}/orders/{doc.id}/claim", headers=headers)
    _web(client, wh)
    client.post(f"/warehouse/orders/{doc.id}/reassign", data={"login_id": pickers[1].id})
    assert doc.claimed_by_id == pickers[1].id
    page = client.post(f"/warehouse/orders/{doc.id}/reassign", data={"login_id": pickers[2].id},
                       follow_redirects=True).get_data(as_text=True)
    assert "doesn&#39;t work in Warehouse A" in page or "doesn't work in Warehouse A" in page
    client.post(f"/warehouse/orders/{doc.id}/release")
    assert doc.dispatch_status == DispatchStatus.TO_PICK and doc.claimed_by_id is None


def test_tenant_isolation_a_token_cant_reach_another_business(client, wh, pickers, db, super_admin):
    from app.models.tenant import BillingCycle, RegistrationType, Tenant

    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.commit()
    doc = order(wh)
    doc.tenant_id = other.id                                        # an order of another business
    db.session.commit()
    headers, _ = auth(client, wh, "ravi")
    assert client.get(f"{API}/orders/{doc.id}", headers=headers).status_code == 404
    assert client.post(f"{API}/orders/{doc.id}/claim", headers=headers).status_code == 404
