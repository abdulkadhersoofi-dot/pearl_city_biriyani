"""The ARFA Counter app opens fast and works without internet.

- Static files are fingerprinted (?v=) and cached for a year; text is
  gzip-compressed for a client that takes it.
- The app's screens carry a version, so the phone's copy is replaced by a
  fresh one when (and only when) it changed.
- A paired phone always bills offline and sends its offline bills,
  whether or not the business has offline billing on for the website
  counter; the owner reviews them on Offline bills.
- The service worker serves the app's screens from the phone at once, and
  the Android app doesn't hold the splash for the server."""

import gzip
import re
from pathlib import Path

import pytest

from app.models.user import User, UserRole

ROOT = Path(__file__).resolve().parent.parent


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password})


@pytest.fixture()
def branch(db, tenant):
    user = User(tenant_id=tenant.id, name="Counter", email="counter@acme.example.com", role=UserRole.STAFF,
                must_change_password=False)
    user.set_password("CounterSecret123")
    db.session.add(user)
    db.session.commit()
    return user


# --- delivery ------------------------------------------------------------------------------------

def test_static_files_are_fingerprinted_and_cached_for_a_year(client):
    page = client.get("/auth/login").get_data(as_text=True)
    url = re.search(r'/static/css/app\.css\?v=[0-9a-f]{10}', page).group(0)
    res = client.get(url)
    assert res.status_code == 200
    assert "immutable" in res.headers["Cache-Control"] and "max-age=31536000" in res.headers["Cache-Control"]
    assert "no-cache" not in res.headers["Cache-Control"]
    # Without the fingerprint, nothing changes: the browser asks again.
    assert "max-age=31536000" not in client.get("/static/css/app.css").headers.get("Cache-Control", "")


def test_text_is_compressed_for_a_client_that_takes_it(client):
    page = client.get("/auth/login").get_data(as_text=True)
    url = re.search(r'/static/css/app\.css\?v=[0-9a-f]{10}', page).group(0)
    plain = client.get(url).get_data()
    zipped = client.get(url, headers={"Accept-Encoding": "gzip, deflate"})
    assert zipped.headers["Content-Encoding"] == "gzip" and "Accept-Encoding" in zipped.headers["Vary"]
    assert gzip.decompress(zipped.get_data()) == plain and len(zipped.get_data()) < len(plain) / 3
    assert "Content-Encoding" not in client.get(url).headers   # nothing asked, nothing compressed
    html = client.get("/auth/login", headers={"Accept-Encoding": "gzip"})
    assert html.headers.get("Content-Encoding") == "gzip"


# --- the app's screens --------------------------------------------------------------------------

def _pair(client, db, tenant, branch, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    html = client.post(f"/branches/{branch.id}/pair").get_data(as_text=True)
    client.get("/auth/logout")
    code = "".join(re.search(r'<p class="pair-big"[^>]*><span>(\d{3})</span><span>(\d{3})</span>', html).groups())
    return client.post("/app/pair", json={"code": code, "name": "Phone"}).get_json()


def test_a_screen_carries_its_version(client, db, tenant, branch, client_admin):
    _pair(client, db, tenant, branch, client_admin)
    first = client.get("/app/bill").headers["X-Screen-Version"]
    assert first == client.get("/app/bill").headers["X-Screen-Version"]   # the same screen, the same version
    from app.models.product import Product
    from decimal import Decimal

    db.session.add(Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="9963", gst_rate=Decimal("5"),
                           unit="NOS", sale_price=Decimal("20")))
    db.session.commit()
    assert client.get("/app/bill").headers["X-Screen-Version"] != first   # a new item: a new version


def test_a_paired_phone_bills_offline_even_with_offline_billing_off(client, db, tenant, branch, client_admin):
    assert not tenant.has_feature("offline_pos")
    device = _pair(client, db, tenant, branch, client_admin)
    html = client.get("/app/bill").get_data(as_text=True)
    assert "pos_offline.js" in html and '"sync_url": "/pos/offline/sync"' in html
    headers = {"X-Device-Id": str(device["device_id"]), "X-Device-Token": device["token"]}
    state = client.get("/pos/offline/state", headers=headers)
    assert state.status_code == 200 and state.get_json()["code"] == device["code"]
    assert client.post("/pos/offline/sync", json={"bills": []}, headers=headers).status_code == 200
    # A branch signed in by email on a browser, not a paired phone: as before, no offline billing.
    client.get("/auth/logout")
    _login(client, branch.email, "CounterSecret123")
    assert client.get("/pos/offline/state", headers=headers).status_code == 404
    assert "pos_offline.js" not in client.get("/app/bill").get_data(as_text=True)
    # The owner sees Offline bills once a phone can bill offline.
    client.get("/auth/logout")
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get("/pos/offline").status_code == 200


def test_offline_bills_stay_hidden_with_nothing_offline(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get("/pos/offline").status_code == 404


# --- the phone side (checked as files: the APK builds on GitHub) -------------------------------------

def test_the_service_worker_opens_the_app_from_the_phone():
    sw = (ROOT / "app/static/js/pos_sw.js").read_text()
    assert "function appScreen" in sw and "X-Screen-Version" in sw and "screen-updated" in sw
    assert "fetchWithin" in sw and 'arfa-app-v2' in sw
    app = (ROOT / "app/static/js/counter_app.js").read_text()
    assert "AbortController" in app and "registerWorker" in app and "arfa:screen-updated" in app
    bill = (ROOT / "app/static/js/counter_bill.js").read_text()
    assert "ArfaApp.registerWorker()" in bill and "reloadIfIdle" in bill


def test_the_android_app_never_holds_the_splash_for_the_server():
    activity = (ROOT / "android/app/src/main/java/com/arfa/pos/MainActivity.kt").read_text()
    assert "SPLASH_MAX_MS = 1500L" in activity and "System.currentTimeMillis() - startedAt < SPLASH_MAX_MS" in activity
    assert "override fun onPageCommitVisible(" in activity and "offscreenPreRaster = true" in activity
