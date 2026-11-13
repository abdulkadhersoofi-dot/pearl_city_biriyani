"""ARFA Counter app, stage 1: the owner pairs a phone with a branch login
by a one-time code; the phone signs itself back in with its own secret;
the owner sees and switches off the business's counter phones."""

import re
from datetime import timedelta

import pytest

from app.counterapp import pairing
from app.models.audit_log import AuditLog
from app.models.mixins import utcnow
from app.models.platform import CounterDevice, DevicePairing
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _code_from(html):
    return "".join(re.search(r'<p class="pair-big"[^>]*><span>(\d{3})</span><span>(\d{3})</span>', html).groups())


@pytest.fixture()
def code(client, db, client_admin, staff):
    """A code made by the owner on the Branches page, then the owner signs out."""
    _login(client, client_admin.email, "ClientSecret123")
    html = client.post(f"/branches/{staff.id}/pair").get_data(as_text=True)
    client.get("/auth/logout")
    return _code_from(html)


def _pair(client, code, name="Front phone", platform="android"):
    return client.post("/app/pair", json={"code": code, "name": name, "platform": platform})


# --- the owner's side ----------------------------------------------------------------------------

def test_the_owner_gets_a_one_time_code_for_a_branch(client, db, client_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")
    html = client.get("/branches/").get_data(as_text=True)
    assert f"/branches/{staff.id}/pair" in html and "Billing phones &amp; tablets" in html
    page = client.post(f"/branches/{staff.id}/pair").get_data(as_text=True)
    code = _code_from(page)
    assert len(code) == 6 and "Pair a phone with Cashier" in page
    record = DevicePairing.query.one()
    assert record.user_id == staff.id and code not in record.code_hash and record.used_at is None
    assert timedelta(minutes=14) < record.expires_at - utcnow() <= timedelta(minutes=15)
    assert AuditLog.query.filter_by(action="counter_pairing_code_made").count() == 1


def test_only_a_branch_login_of_your_own_business_gets_a_code(client, db, client_admin, staff):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    stranger = User(tenant_id=other.id, name="Their Branch", email="their@other.example.com", role=UserRole.STAFF,
                    must_change_password=False)
    stranger.set_password("Whatever123")
    db.session.add(stranger)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    assert client.post(f"/branches/{stranger.id}/pair").status_code == 403
    resp = client.post(f"/branches/{client_admin.id}/pair", follow_redirects=True)
    assert "Phones pair with a branch login." in resp.get_data(as_text=True)
    staff.is_active = False
    db.session.commit()
    resp = client.post(f"/branches/{staff.id}/pair", follow_redirects=True)
    assert "switched off" in resp.get_data(as_text=True)
    assert DevicePairing.query.count() == 0
    client.get("/auth/logout")
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    assert client.post(f"/branches/{staff.id}/pair").status_code in (302, 403)


def test_a_new_code_retires_the_old_one(client, db, client_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")
    first = _code_from(client.post(f"/branches/{staff.id}/pair").get_data(as_text=True))
    second = _code_from(client.post(f"/branches/{staff.id}/pair").get_data(as_text=True))
    client.get("/auth/logout")
    if first != second:
        assert _pair(client, first).status_code == 400
    assert _pair(client, second).status_code == 200


# --- the phone's side ----------------------------------------------------------------------------

def test_a_phone_pairs_and_is_signed_in_as_the_branch(client, db, tenant, staff, code):
    resp = _pair(client, code)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["code"] == "O1" and data["branch"] == "Cashier" and data["business"] == tenant.display_name
    assert data["token"] and data["tenant_id"] == tenant.id and data["user_id"] == staff.id
    assert "arfa_app=1" in resp.headers.get("Set-Cookie", "")
    device = CounterDevice.query.one()
    assert device.user_id == staff.id and device.platform == "android" and device.name == "Front phone"
    assert DevicePairing.query.one().used_at is not None and DevicePairing.query.one().device_id == device.id
    home = client.get("/app/").get_data(as_text=True)
    assert "What's next?" in home and "Cashier" in home and "/app/bill" in home
    assert AuditLog.query.filter_by(action="counter_device_paired").count() == 1


def test_a_code_works_once_and_not_after_it_expires(client, db, staff, code):
    assert _pair(client, code).status_code == 200
    client.get("/auth/logout")
    client.delete_cookie("arfa_app")
    again = _pair(client, code)
    assert again.status_code == 400 and "expired" in again.get_json()["error"]
    _login(client, "admin@acme.example.com", "ClientSecret123")
    fresh = _code_from(client.post(f"/branches/{staff.id}/pair").get_data(as_text=True))
    client.get("/auth/logout")
    DevicePairing.query.filter_by(used_at=None).update({"expires_at": utcnow() - timedelta(seconds=1)})
    db.session.commit()
    assert _pair(client, fresh).status_code == 400


def test_wrong_codes_are_counted_and_then_refused(client, db, code):
    assert _pair(client, "12345").status_code == 400          # not 6 digits
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(pairing.PAIR_ATTEMPTS - 1):
        assert _pair(client, wrong).status_code == 400
    assert AuditLog.query.filter_by(action=pairing.FAILED_ACTION).count() == pairing.PAIR_ATTEMPTS
    blocked = _pair(client, code)
    assert blocked.status_code == 429                       # even the right code waits now
    assert CounterDevice.query.count() == 0


def _fresh(client):
    """The app opened again later: no session, no remember cookie (the
    tests share one app context, so one client is reused, signed out)."""
    client.get("/auth/logout")
    client.delete_cookie("arfa_app")


def test_a_paired_phone_signs_itself_back_in(client, db, staff, code):
    data = _pair(client, code).get_json()
    _fresh(client)
    assert "Pair this phone" in client.get("/app/").get_data(as_text=True)
    resp = client.post("/app/resume", json={"device_id": data["device_id"], "token": data["token"]})
    assert resp.status_code == 200 and resp.get_json()["code"] == "O1"
    assert "What's next?" in client.get("/app/").get_data(as_text=True)
    _fresh(client)
    assert client.post("/app/resume", json={"device_id": data["device_id"], "token": "wrong"}).status_code == 403


def test_switching_a_phone_off_stops_it(client, db, client_admin, staff, code):
    data = _pair(client, code).get_json()
    _fresh(client)
    _login(client, client_admin.email, "ClientSecret123")
    page = client.get("/branches/").get_data(as_text=True)
    assert "Front phone" in page and "ARFA Counter app" in page and "Bill series O1" in page
    device = CounterDevice.query.one()
    client.post(f"/branches/devices/{device.id}/active", data={"active": "0"})
    db.session.refresh(device)
    assert device.is_active is False
    _fresh(client)
    resp = client.post("/app/resume", json={"device_id": data["device_id"], "token": data["token"]})
    assert resp.status_code == 403 and "switched off by the owner" in resp.get_json()["error"]
    _login(client, client_admin.email, "ClientSecret123")
    client.post(f"/branches/devices/{device.id}/active", data={"active": "1"})
    _fresh(client)
    assert client.post("/app/resume", json={"device_id": data["device_id"], "token": data["token"]}).status_code == 200


def test_a_paused_branch_or_business_stops_the_phone(client, db, tenant, staff, code):
    data = _pair(client, code).get_json()
    body = {"device_id": data["device_id"], "token": data["token"]}
    _fresh(client)
    staff.is_active = False
    db.session.commit()
    assert client.post("/app/resume", json=body).status_code == 403
    staff.is_active = True
    tenant.is_active = False
    db.session.commit()
    assert client.post("/app/resume", json=body).status_code == 403


def test_unpairing_switches_the_phone_off_and_signs_out(client, db, staff, code):
    _pair(client, code)
    resp = client.post("/app/unpair")
    assert resp.status_code == 200 and "arfa_app=;" in resp.headers.get("Set-Cookie", "")
    assert CounterDevice.query.one().is_active is False
    assert "Pair this phone" in client.get("/app/").get_data(as_text=True)


def test_a_paired_phone_signed_out_goes_back_to_the_app(client, db, staff, code):
    _pair(client, code)
    client.get("/auth/logout")
    resp = client.get("/auth/login")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/app/")
    assert client.get("/auth/login?web=1").status_code == 200


def test_other_logins_are_told_the_app_is_for_branches(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    assert "This app is for a branch's counter." in client.get("/app/").get_data(as_text=True)


def test_the_paired_phone_is_the_counters_offline_device(client, db, tenant, staff, code):
    """The same device numbers offline bills - the counter doesn't register a second one."""
    tenant.set_feature("offline_pos", True)
    db.session.commit()
    data = _pair(client, code).get_json()
    resp = client.get("/pos/offline/state", headers={"X-Device-Id": str(data["device_id"]),
                                                      "X-Device-Token": data["token"]})
    assert resp.status_code == 200 and resp.get_json()["code"] == "O1"
    assert CounterDevice.query.count() == 1


def test_another_business_cannot_switch_my_phone(client, db, staff, code):
    _pair(client, code)
    device = CounterDevice.query.one()
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other.id, gstin=None, state_code="27", state_name="Maharashtra", is_primary=True))
    boss = User(tenant_id=other.id, name="Other Boss", email="boss@other.example.com", role=UserRole.CLIENT_ADMIN,
                must_change_password=False)
    boss.set_password("OtherSecret123")
    db.session.add(boss)
    db.session.commit()
    _fresh(client)
    _login(client, boss.email, "OtherSecret123")
    assert client.post(f"/branches/devices/{device.id}/active", data={"active": "0"}).status_code == 404
    assert "Front phone" not in client.get("/branches/").get_data(as_text=True)
    db.session.refresh(device)
    assert device.is_active is True


# --- several phones for one branch -----------------------------------------------------------------

def _as(other_client):
    """Switch to another browser or phone. The tests share one app context, so
    the signed-in user Flask-Login caches there is dropped first."""
    from flask import g

    g.pop("_login_user", None)
    return other_client


def _code_for(client, staff, phones):
    page = client.post(f"/branches/{staff.id}/pair", data={"phones": str(phones)}).get_data(as_text=True)
    return _code_from(page), page


def test_one_code_pairs_several_phones_of_a_branch_then_stops(client, db, client_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")
    code, page = _code_for(client, staff, 3)
    assert "Pair 3 phones with Cashier" in page and "up to 3 phones" in page
    record = DevicePairing.query.one()
    status_url = f"/branches/pairings/{record.id}/status.json"
    assert client.get(status_url).get_json() == {"paired": [], "max": 3, "done": False, "expired": False}
    client.get("/auth/logout")

    phones = [client.application.test_client() for _ in range(4)]
    results = [_pair(_as(p), code, name=f"Counter {i + 1}") for i, p in enumerate(phones)]
    assert [r.status_code for r in results[:3]] == [200, 200, 200]
    assert results[3].status_code != 200                       # the code was for three
    devices = CounterDevice.query.filter_by(user_id=staff.id).order_by(CounterDevice.id).all()
    assert [d.name for d in devices] == ["Counter 1", "Counter 2", "Counter 3"]
    assert len({d.code for d in devices}) == 3                 # each its own bill series
    record = db.session.get(DevicePairing, record.id)
    assert record.uses == 3 and record.used_at is not None

    _login(_as(client), client_admin.email, "ClientSecret123")
    status = client.get(status_url).get_json()
    assert status["done"] is True and [p["name"] for p in status["paired"]] == ["Counter 1", "Counter 2", "Counter 3"]
    # All three bill at once: each phone is signed in as the branch.
    for p in phones[:3]:
        resp = _as(p).get("/app/")
        assert resp.status_code in (200, 302) and "/auth/login" not in (resp.headers.get("Location") or "")


def test_the_phone_count_is_kept_between_one_and_ten(client, db, client_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")
    _code_for(client, staff, 50)
    assert DevicePairing.query.order_by(DevicePairing.id.desc()).first().max_uses == pairing.MAX_PHONES_PER_CODE
    _code_for(client, staff, 0)
    assert DevicePairing.query.order_by(DevicePairing.id.desc()).first().max_uses == 1
    client.post(f"/branches/{staff.id}/pair", data={"phones": "lots"})
    assert DevicePairing.query.order_by(DevicePairing.id.desc()).first().max_uses == 1


def test_phones_are_listed_under_their_branch_and_can_be_renamed(client, db, client_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")
    code, _ = _code_for(client, staff, 2)
    client.get("/auth/logout")
    _pair(_as(client.application.test_client()), code, name="Phone A")
    _pair(_as(client.application.test_client()), code, name="")
    _login(_as(client), client_admin.email, "ClientSecret123")
    html = client.get("/branches/").get_data(as_text=True)
    group = html.split(f'id="phones-{staff.id}"', 1)[1].split('class="phone-group"', 1)[0]
    assert 'value="Phone A"' in group and 'value="Phone"' in group and "2 phones on" in group
    assert f'href="#phones-{staff.id}">2 on</a>' in html
    unnamed = CounterDevice.query.filter_by(name="Phone").one()        # no name typed on the phone
    resp = client.post(f"/branches/devices/{unnamed.id}/rename", data={"name": "  Counter   2 "})
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"#phones-{staff.id}")
    assert db.session.get(CounterDevice, unnamed.id).name == "Counter 2"
    assert AuditLog.query.filter_by(action="counter_device_renamed").count() == 1
    client.post(f"/branches/devices/{unnamed.id}/rename", data={"name": "   "})
    assert db.session.get(CounterDevice, unnamed.id).name == "Counter 2"


def test_another_business_cant_see_or_rename_our_phones(client, db, client_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")
    code, _ = _code_for(client, staff, 1)
    record = DevicePairing.query.one()
    client.get("/auth/logout")
    _pair(_as(client.application.test_client()), code)
    device = CounterDevice.query.one()

    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other.id, gstin="29AAAAA0000A1Z5", state_code="29", state_name="Karnataka",
                         is_primary=True))
    owner = User(tenant_id=other.id, name="Other Owner", email="owner@other.example.com", role=UserRole.CLIENT_ADMIN,
                 must_change_password=False)
    owner.set_password("OtherSecret123")
    db.session.add(owner)
    db.session.commit()
    _login(_as(client), "owner@other.example.com", "OtherSecret123")
    assert client.get(f"/branches/pairings/{record.id}/status.json").status_code == 404
    assert client.post(f"/branches/devices/{device.id}/rename", data={"name": "Mine now"}).status_code == 404
    assert db.session.get(CounterDevice, device.id).name == "Front phone"
