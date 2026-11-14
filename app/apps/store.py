"""Where the Android apps' builds live, and the links that download them.

The GitHub build (.github/workflows/android-apk.yml) puts each new build
in the Cloud Storage bucket APPS_BUCKET, under latest/:

  latest/ARFA-Counter.apk   latest/version.json        (ARFA Counter)
  latest/ARFA-Scan.apk      latest/scan-version.json   (ARFA Scan)

The bucket is private. The website reads the small version files with its
own service account, and a download is a redirect to a signed link that
works for a few minutes only - so an APK is only ever handed to someone
signed in (or to a phone app that is). Signing needs no key: Google signs
for the service account (IAM signBlob, scripts/gcp/apps_setup.sh grants
it). In development and tests APPS_DIR, a plain folder, stands in for the
bucket.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from flask import current_app

APK_TYPE = "application/vnd.android.package-archive"
PREFIX = "latest/"
CACHE_SECONDS = 60
LINK_SECONDS = 15 * 60
METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default"
STORAGE_HOST = "storage.googleapis.com"


@dataclass(frozen=True)
class AndroidApp:
    key: str
    name: str
    apk: str
    version_file: str
    blurb: str


APPS = {
    "counter": AndroidApp("counter", "ARFA Counter", "ARFA-Counter.apk", "version.json",
                          "The billing counter for a branch: bills, tables and KOT, printing to a "
                          "Bluetooth or USB printer, and billing on even when the internet drops."),
    "scan": AndroidApp("scan", "ARFA Scan", "ARFA-Scan.apk", "scan-version.json",
                       "Photograph purchase bills, expenses, notes and sales invoices; ARFA reads them, "
                       "and you check and post each one from the phone or the website."),
    "warehouse": AndroidApp("warehouse", "ARFA Warehouse", "ARFA-Warehouse.apk", "warehouse-version.json",
                            "For pickers in a warehouse: new orders ring like a message, scan each unit's label "
                            "to pick it, receive purchases and count stock by scanning."),
}


class StoreError(Exception):
    """The bucket couldn't be reached or refused - the page says "try later"."""


def configured() -> bool:
    cfg = current_app.config
    return bool(cfg.get("APPS_BUCKET") or cfg.get("APPS_DIR"))


# --- what the latest build is ----------------------------------------------------------------------

_cache: dict[str, tuple[float, dict | None]] = {}
_lock = threading.Lock()


def latest(app: AndroidApp) -> dict | None:
    """The newest build's version file ({"versionCode", "versionName",
    "commit", "built_at", "size", ...}), or None when none is published
    here yet. Cached for a minute, so a busy morning of update checks
    reads the bucket once a minute, not once a phone."""
    if not configured():
        return None
    where = current_app.config.get("APPS_BUCKET") or current_app.config.get("APPS_DIR")
    cache_key = f"{where}:{app.key}"
    now = time.monotonic()
    with _lock:
        hit = _cache.get(cache_key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    raw = _read(PREFIX + app.version_file)
    info = None
    if raw is not None:
        try:
            info = json.loads(raw)
        except ValueError:
            info = None
        if not isinstance(info, dict) or not int(info.get("versionCode") or 0):
            info = None
    with _lock:
        _cache[cache_key] = (now, info)
    return info


def forget() -> None:
    with _lock:
        _cache.clear()


def built_at(info: dict | None) -> datetime | None:
    value = (info or {}).get("built_at")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def local_path(app: AndroidApp) -> str | None:
    """The APK on disk when APPS_DIR stands in for the bucket."""
    folder = current_app.config.get("APPS_DIR")
    if not folder or current_app.config.get("APPS_BUCKET"):
        return None
    path = os.path.join(folder, PREFIX, app.apk)
    return path if os.path.isfile(path) else None


def download_url(app: AndroidApp) -> str:
    """A link to the APK that works for LINK_SECONDS (the bucket only)."""
    return signed_url(current_app.config["APPS_BUCKET"], PREFIX + app.apk, LINK_SECONDS,
                      disposition=f'attachment; filename="{app.apk}"', content_type=APK_TYPE)


# --- the bucket ----------------------------------------------------------------------------------------

def _read(name: str) -> bytes | None:
    bucket = current_app.config.get("APPS_BUCKET")
    if not bucket:
        path = os.path.join(current_app.config["APPS_DIR"], name)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()
    url = f"https://{STORAGE_HOST}/storage/v1/b/{quote(bucket, safe='')}/o/{quote(name, safe='')}?alt=media"
    try:
        res = requests.get(url, headers={"Authorization": "Bearer " + _token()}, timeout=10)
    except requests.RequestException as exc:
        raise StoreError(str(exc)) from exc
    if res.status_code == 404:
        return None
    if res.status_code != 200:
        raise StoreError(f"Cloud Storage answered {res.status_code}")
    return res.content


_token_cache: dict[str, tuple[float, str]] = {}


def _token() -> str:
    """The service account's access token, from the metadata server."""
    hit = _token_cache.get("token")
    if hit and time.time() < hit[0]:
        return hit[1]
    try:
        res = requests.get(METADATA + "/token", headers={"Metadata-Flavor": "Google"}, timeout=5)
        res.raise_for_status()
        body = res.json()
    except (requests.RequestException, ValueError) as exc:
        raise StoreError(f"no service account token: {exc}") from exc
    _token_cache["token"] = (time.time() + max(int(body.get("expires_in", 300)) - 120, 30), body["access_token"])
    return body["access_token"]


def _account() -> str:
    hit = _token_cache.get("email")
    if hit:
        return hit[1]
    try:
        res = requests.get(METADATA + "/email", headers={"Metadata-Flavor": "Google"}, timeout=5)
        res.raise_for_status()
    except requests.RequestException as exc:
        raise StoreError(f"no service account: {exc}") from exc
    _token_cache["email"] = (0, res.text.strip())
    return res.text.strip()


def _sign_blob(account: str, payload: bytes) -> bytes:
    """Google signs with the service account's own key (IAM Credentials)."""
    url = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{quote(account, safe='@.')}:signBlob"
    try:
        res = requests.post(url, json={"payload": base64.b64encode(payload).decode()},
                            headers={"Authorization": "Bearer " + _token()}, timeout=10)
    except requests.RequestException as exc:
        raise StoreError(str(exc)) from exc
    if res.status_code != 200:
        raise StoreError(f"signing refused ({res.status_code}) - scripts/gcp/apps_setup.sh grants it")
    return base64.b64decode(res.json()["signedBlob"])


def signed_url(bucket: str, name: str, seconds: int, *, disposition: str | None = None,
               content_type: str | None = None, account: str | None = None, signer=None,
               now: datetime | None = None) -> str:
    """A V4 signed GET link to one object
    (https://cloud.google.com/storage/docs/access-control/signing-urls-manually)."""
    account = account or _account()
    signer = signer or (lambda payload: _sign_blob(account, payload))
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    scope = f"{day}/auto/storage/goog4_request"
    path = f"/{bucket}/{quote(name, safe='/~')}"
    params = {
        "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
        "X-Goog-Credential": f"{account}/{scope}",
        "X-Goog-Date": stamp,
        "X-Goog-Expires": str(seconds),
        "X-Goog-SignedHeaders": "host",
    }
    if disposition:
        params["response-content-disposition"] = disposition
    if content_type:
        params["response-content-type"] = content_type
    query = "&".join(f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(params.items()))
    request = "\n".join(["GET", path, query, f"host:{STORAGE_HOST}\n", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["GOOG4-RSA-SHA256", stamp, scope, hashlib.sha256(request.encode()).hexdigest()])
    signature = binascii.hexlify(signer(to_sign.encode())).decode()
    return f"https://{STORAGE_HOST}{path}?{query}&X-Goog-Signature={signature}"
