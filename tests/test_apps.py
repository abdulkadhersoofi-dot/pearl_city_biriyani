"""The Android apps on arfa.live (app/apps): the Phone apps page is for
people signed in only, the APKs come from the private bucket through
short-lived links, and both apps' update checks ask this site first."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from app.apps import store
from tests.test_scan_app import API, _login as _scan_login, scanning  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
APK = b"PK\x03\x04 pretend apk"


def _web_login(client, email="admin@acme.example.com", password="ClientSecret123"):
    return client.post("/auth/login", data={"email": email, "password": password})


@pytest.fixture()
def published(app, tmp_path):
    folder = tmp_path / "latest"
    folder.mkdir()
    for apk, meta, code in (("ARFA-Counter.apk", "version.json", 41), ("ARFA-Scan.apk", "scan-version.json", 42)):
        (folder / apk).write_bytes(APK)
        (folder / meta).write_text(json.dumps({"versionCode": code, "versionName": f"1.{code}", "commit": "abc",
                                               "built_at": "2026-11-04T09:30:00Z", "size": 36700160,
                                               "apk": "https://github.com/x/releases/download/android-latest/" + apk,
                                               "server_url": "https://arfa.live"}))
    app.config.update(APPS_DIR=str(tmp_path), APPS_BUCKET="")
    store.forget()
    yield tmp_path
    store.forget()


def test_the_page_is_for_people_signed_in_only(client, db, published):
    res = client.get("/apps")
    assert res.status_code == 302 and "/auth/login" in res.headers["Location"]
    assert client.get("/apps/counter/download").status_code == 302
    assert client.get("/apps/counter/latest.json").status_code == 401


def test_the_page_shows_each_app_with_its_version(client, db, published, scanning, client_admin):
    _web_login(client)
    page = client.get("/apps").get_data(as_text=True)
    assert "ARFA Counter" in page and "ARFA Scan" in page
    assert "Version 1.41" in page and "Version 1.42" in page and "04 Nov 2026" in page and "35 MB" in page
    assert "/apps/scan/download" in page and "<svg" in page          # the QR code that opens the page on a phone
    assert "private" in client.get("/apps").headers["Cache-Control"]


def test_scan_is_offered_only_where_scan_bills_is_on(client, db, published, tenant, client_admin):
    _web_login(client)
    page = client.get("/apps").get_data(as_text=True)
    assert "ARFA Counter" in page and "ARFA Scan" not in page


def test_nothing_published_yet_says_so(client, db, app, client_admin):
    app.config.update(APPS_DIR="", APPS_BUCKET="")
    store.forget()
    _web_login(client)
    page = client.get("/apps").get_data(as_text=True)
    assert "aren't published on this server yet" in page and "Not published here yet" in page
    assert client.get("/apps/counter/download").status_code == 404
    assert client.get("/apps/counter/latest.json").get_json() == {"versionCode": 0, "available": False}


def test_a_signed_in_person_downloads_the_apk(client, db, published, client_admin):
    _web_login(client)
    res = client.get("/apps/counter/download")
    assert res.status_code == 200 and res.data == APK
    assert res.mimetype == store.APK_TYPE and "ARFA-Counter.apk" in res.headers["Content-Disposition"]
    assert client.get("/apps/nope/download").status_code == 404


def test_the_counter_update_check_hands_out_a_link_that_works_in_a_browser(app, client, db, published, client_admin):
    _web_login(client)
    body = client.get("/apps/counter/latest.json").get_json()
    assert body["available"] and body["versionCode"] == 41 and body["server_url"] == "https://arfa.live"
    assert "/apps/counter/get?t=" in body["apk"] and "github" not in body["apk"]
    link = body["apk"].split("localhost", 1)[-1]
    browser = app.test_client()                                       # the phone's browser: not signed in
    assert browser.get(link).data == APK
    assert browser.get(link.replace("/counter/", "/scan/")).status_code == 404   # a link is for one app
    assert browser.get(link[:-3] + "xyz").status_code == 404                     # nor can it be altered


def test_an_old_link_has_expired(app, client, db, published, client_admin, monkeypatch):
    _web_login(client)
    link = client.get("/apps/counter/latest.json").get_json()["apk"].split("localhost", 1)[-1]
    import itsdangerous.timed

    later = datetime.now(timezone.utc).timestamp() + 2 * 86400
    monkeypatch.setattr(itsdangerous.timed.time, "time", lambda: later)
    res = app.test_client().get(link)
    assert res.status_code == 410 and "expired" in res.get_data(as_text=True)


def test_the_scan_app_asks_with_its_token(client, db, published, scanning, client_admin):
    auth = _scan_login(client)
    body = client.get(f"{API}/app-update", headers=auth).get_json()
    assert body["versionCode"] == 42 and "/apps/scan/get?t=" in body["apk"]
    assert client.get(f"{API}/app-update").status_code == 401


# --- the bucket --------------------------------------------------------------------------------------

class _Res:
    def __init__(self, status, body=b"", json_body=None, text=""):
        self.status_code, self.content, self._json, self.text = status, body, json_body, text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise store.requests.HTTPError(str(self.status_code))


def test_from_the_bucket_a_download_is_a_short_signed_link(app, client, db, client_admin, monkeypatch):
    app.config.update(APPS_BUCKET="arfa-gst-prod-1-apps", APPS_DIR="")
    store.forget()
    store._token_cache.clear()
    meta = json.dumps({"versionCode": 41, "versionName": "1.41"}).encode()
    seen = []

    def get(url, headers=None, timeout=None):
        seen.append(url)
        if url.endswith("/token"):
            return _Res(200, json_body={"access_token": "tok", "expires_in": 3600})
        if url.endswith("/email"):
            return _Res(200, text="arfa-run@arfa-gst-prod-1.iam.gserviceaccount.com")
        assert headers["Authorization"] == "Bearer tok"
        return _Res(200, meta) if "latest%2Fversion.json" in url else _Res(404)

    def post(url, json=None, headers=None, timeout=None):
        assert url.endswith("arfa-run@arfa-gst-prod-1.iam.gserviceaccount.com:signBlob")
        return _Res(200, json_body={"signedBlob": "AAEC"})

    monkeypatch.setattr(store.requests, "get", get)
    monkeypatch.setattr(store.requests, "post", post)
    _web_login(client)
    res = client.get("/apps/counter/download")
    assert res.status_code == 302
    where = res.headers["Location"]
    assert where.startswith("https://storage.googleapis.com/arfa-gst-prod-1-apps/latest/ARFA-Counter.apk?")
    assert "X-Goog-Expires=900" in where and where.endswith("X-Goog-Signature=000102")
    assert client.get("/apps/scan/download").status_code == 404        # no scan-version.json in the bucket
    store.forget()


def test_a_bucket_that_cant_be_reached_says_try_later(app, client, db, client_admin, monkeypatch):
    app.config.update(APPS_BUCKET="arfa-gst-prod-1-apps", APPS_DIR="")
    store.forget()
    store._token_cache.clear()
    monkeypatch.setattr(store.requests, "get", lambda *a, **k: _Res(500))
    _web_login(client)
    assert "try again in a minute" in client.get("/apps").get_data(as_text=True)
    assert client.get("/apps/counter/download").status_code == 503
    assert client.get("/apps/counter/latest.json").get_json()["versionCode"] == 0
    store.forget()


def test_the_signed_link_is_googles_v4_signature():
    # Checked against google-cloud-storage's own generate_signed_url_v4 with
    # the same request time and a stand-in signer (sha256 of the text).
    import hashlib

    url = store.signed_url("arfa-gst-prod-1-apps", "latest/ARFA-Scan.apk", 900,
                           disposition='attachment; filename="ARFA-Scan.apk"', content_type=store.APK_TYPE,
                           account="arfa-run@p.iam.gserviceaccount.com", signer=lambda b: hashlib.sha256(b).digest(),
                           now=datetime(2026, 11, 4, 10, 20, 30, tzinfo=timezone.utc))
    assert url.endswith("X-Goog-Signature=c1f099e678dde9d88fc6508bae994cdfa1ec74572bf857e87c9aa80ee387f406")


# --- the build and the apps ----------------------------------------------------------------------------

def test_the_build_puts_the_apps_in_the_bucket():
    workflow = yaml.safe_load((ROOT / ".github/workflows/android-apk.yml").read_text())
    assert workflow["permissions"]["id-token"] == "write"
    steps = workflow["jobs"]["build"]["steps"]
    upload = next(s for s in steps if "Publish to arfa.live" in s.get("name", ""))
    script = upload["run"]
    assert script.index("ARFA-Scan.apk") < script.index("scan-version.json")   # the APKs before the news of them
    settings = (ROOT / "deploy/gcp/settings.env").read_text()
    assert workflow["env"]["APPS_BUCKET"] == "arfa-gst-prod-1-apps" and 'APPS_BUCKET="${PROJECT_ID}-apps"' in settings
    assert "APPS_BUCKET=" in (ROOT / "scripts/gcp/release.sh").read_text()
    names = [s.get("name", "") for s in steps]
    assert names.index("Publish to arfa.live (the apps bucket)") < next(
        i for i, n in enumerate(names) if "latest\" release" in n)


def test_both_apps_ask_arfa_first():
    scan = (ROOT / "android/scan/src/main/java/com/arfa/scan/UpdateChecker.kt").read_text()
    assert 'Api.get("/app-update")' in scan
    counter = (ROOT / "android/app/src/main/java/com/arfa/pos/UpdateChecker.kt").read_text()
    assert re.search(r'"\$base/apps/counter/latest\.json"', counter) and "CookieManager" in counter
