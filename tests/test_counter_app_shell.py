"""ARFA Counter app, stage 5: the Android app. It opens the app's own screens
and tells the server it is the app (its user agent), so a branch login
lands there; a restaurant's phone says what it's for (Counter / Waiter /
Kitchen). The Android project can't be compiled here (GitHub builds it),
so its hand-over points are checked as files."""

import re
import xml.dom.minidom
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANDROID = ROOT / "android"
APP_UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126 Mobile ARFACounter/1.42"}


def _login(client, email, password, headers=None):
    return client.post("/auth/login", data={"email": email, "password": password}, headers=headers or {})


# --- the server knows the app ------------------------------------------------------------------------

def test_the_app_opens_on_its_own_screens(client, db, staff):
    res = client.get("/auth/login", headers=APP_UA)
    assert res.status_code == 302 and res.headers["Location"].endswith("/app/")
    assert client.get("/auth/login?web=1", headers=APP_UA).status_code == 200
    assert client.get("/auth/login").status_code == 200  # a browser: the sign-in form as before


def test_a_branch_signing_in_inside_the_app_lands_on_it(client, db, staff):
    res = _login(client, "cashier@acme.example.com", "CashierSecret123", headers=APP_UA)
    assert res.status_code == 302 and res.headers["Location"].endswith("/app/")
    client.get("/auth/logout")
    res = _login(client, "cashier@acme.example.com", "CashierSecret123")
    assert res.headers["Location"].endswith("/pos/")  # the website's counter, as before


def test_other_logins_inside_the_app_are_not_sent_to_it(client, db, client_admin):
    res = _login(client, client_admin.email, "ClientSecret123", headers=APP_UA)
    assert "/app/" not in res.headers["Location"]


# --- what this phone is for ---------------------------------------------------------------------------

def _code(client, staff):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    html = client.post(f"/branches/{staff.id}/pair").get_data(as_text=True)
    client.get("/auth/logout")
    return "".join(re.search(r'<p class="pair-big"[^>]*><span>(\d{3})</span><span>(\d{3})</span>', html).groups())


def test_pairing_says_whether_to_ask_what_the_phone_is_for(client, db, tenant, client_admin, staff):
    data = client.post("/app/pair", json={"code": _code(client, staff), "name": "Phone"}).get_json()
    assert data["restaurant"] is False
    client.get("/auth/logout")
    tenant.set_feature("restaurant_mode", True)
    db.session.commit()
    data = client.post("/app/pair", json={"code": _code(client, staff), "name": "Phone 2"}).get_json()
    assert data["restaurant"] is True


def test_the_screens_offer_the_choice(client, db, tenant, staff):
    pair = client.get("/app/?start=1").get_data(as_text=True)
    assert "What is this phone for?" in pair and "pickMode('waiter')" in pair and "pickMode('kitchen')" in pair
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    home = client.get("/app/?start=1")
    assert home.status_code == 200 and "counterHome(" in home.get_data(as_text=True)
    assert "Opens on" not in home.get_data(as_text=True)  # not a restaurant: always the counter
    assert "Printer, cash drawer &amp; app settings" in home.get_data(as_text=True)


def test_a_restaurant_phone_can_change_what_it_opens_on(client, db, tenant, staff):
    tenant.set_feature("restaurant_mode", True)
    db.session.commit()
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    html = client.get("/app/").get_data(as_text=True)
    assert "Opens on" in html and "counterHome(null, true)" in html


# --- the Android project ------------------------------------------------------------------------------

def test_the_android_app_is_arfa_counter():
    strings = (ANDROID / "app/src/main/res/values/strings.xml").read_text()
    assert '<string name="app_name">ARFA Counter</string>' in strings
    for res in ("values/strings.xml", "drawable/ic_launcher_foreground.xml", "values/themes.xml"):
        xml.dom.minidom.parse(str(ANDROID / "app/src/main/res" / res))
    activity = (ANDROID / "app/src/main/java/com/arfa/pos/MainActivity.kt").read_text()
    assert '"$baseUrl/app/?start=1"' in activity and '" ARFACounter/"' in activity
    assert '"/app/"' in activity and "FLAG_KEEP_SCREEN_ON" in activity
    # The same application id: phones with the old app update in place.
    assert 'applicationId "com.arfa.pos"' in (ANDROID / "app/build.gradle").read_text()


def test_every_build_is_signed_with_the_same_key():
    gradle = (ANDROID / "app/build.gradle").read_text()
    assert "signingConfig signingConfigs.shared" in gradle
    assert (ANDROID / "app/arfa-counter-debug.keystore").stat().st_size > 1000
    assert "keystore" not in (ANDROID / ".gitignore").read_text()


def test_the_build_publishes_arfa_counter():
    workflow = yaml.safe_load((ROOT / ".github/workflows/android-apk.yml").read_text())
    steps = workflow["jobs"]["build"]["steps"]
    script = "\n".join(step.get("run", "") for step in steps)
    assert "dist/ARFA-Counter.apk" in script and "android-latest/ARFA-Counter.apk" in script
    release = next(step for step in steps if "action-gh-release" in step.get("uses", ""))
    assert "android/dist/ARFA-Counter.apk" in release["with"]["files"]
    updater = (ANDROID / "app/src/main/java/com/arfa/pos/UpdateChecker.kt").read_text()
    assert 'latestUrl("ARFA-Counter.apk")' in updater
