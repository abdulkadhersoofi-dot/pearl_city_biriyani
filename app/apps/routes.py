"""The apps page, the update check, and the download links.

  GET /apps                      the download page (signed in)
  GET /apps/<app>/download       the APK, for someone signed in on the website
  GET /apps/<app>/latest.json    the update check of ARFA Counter (its WebView's
                                 own signed-in session); ARFA Scan asks
                                 /api/scan/v1/app-update with its token instead
  GET /apps/<app>/get?t=...      the APK from a link that check handed out: the
                                 phone opens it in its browser, which isn't
                                 signed in, so the link itself carries a
                                 signed, day-long permission
"""

from __future__ import annotations

import logging

from flask import abort, current_app, jsonify, make_response, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.apps import apps_bp, store
from app.apps.store import APPS, AndroidApp, StoreError

log = logging.getLogger(__name__)

LINK_DAYS = 1


def _app(key: str) -> AndroidApp:
    app = APPS.get(key)
    if app is None:
        abort(404)
    return app


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="android-app-download")


def phone_link(app: AndroidApp, user, owner=None) -> str:
    """The link a phone app's update dialog opens (works for a day). For
    ARFA Warehouse the one asking is a warehouse login (owner), not a user."""
    payload = {"a": app.key, "w": owner.id} if owner is not None else {"a": app.key, "u": user.id}
    token = _serializer().dumps(payload)
    return url_for("apps.get", key=app.key, t=token, _external=True)


def update_info(app: AndroidApp, user, owner=None) -> dict:
    """What an app's update check gets: the newest build here, and a link
    to it. versionCode 0 = nothing published here (the app then looks on
    GitHub, as builds before this did)."""
    try:
        info = store.latest(app)
    except StoreError as exc:
        log.warning("apps: couldn't read the %s version: %s", app.key, exc)
        info = None
    if not info:
        return {"versionCode": 0, "available": False}
    out = {k: info[k] for k in ("versionCode", "versionName", "commit", "built_at", "size", "server_url") if k in info}
    out.update(available=True, apk=phone_link(app, user, owner))
    return out


def _no_cache(response):
    response = make_response(response)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _send(app: AndroidApp):
    """The APK itself: a redirect to a link the bucket signed (a few
    minutes), or the file when a local folder stands in for the bucket."""
    try:
        if not store.latest(app):
            abort(404)
        path = store.local_path(app)
        if path:
            return _no_cache(send_file(path, mimetype=store.APK_TYPE, as_attachment=True, download_name=app.apk))
        return _no_cache(redirect(store.download_url(app)))
    except StoreError as exc:
        log.warning("apps: couldn't hand out %s: %s", app.key, exc)
        return _no_cache((render_template("apps/unavailable.html", app=app), 503))


def _visible(user) -> list[AndroidApp]:
    """ARFA Scan only for a business with Scan bills switched on, ARFA
    Warehouse with warehouse dispatch."""
    tenant = getattr(user, "tenant", None)
    scan = user.is_super_admin or (tenant is not None and tenant.has_feature("doc_scan"))
    warehouse = user.is_super_admin or (tenant is not None and tenant.has_feature("warehouse_dispatch"))
    return [a for a in APPS.values() if (a.key != "scan" or scan) and (a.key != "warehouse" or warehouse)]


@apps_bp.route("")
@login_required
def index():
    from app.pos.share import qr_svg

    cards, trouble = [], False
    for app in _visible(current_user):
        try:
            info = store.latest(app)
        except StoreError as exc:
            log.warning("apps: couldn't read the %s version: %s", app.key, exc)
            info, trouble = None, True
        cards.append({"app": app, "info": info, "built_at": store.built_at(info)})
    page_url = url_for("apps.index", _external=True)
    qr = qr_svg(page_url, scale=4).decode()
    return _no_cache(render_template("apps/index.html", cards=cards, trouble=trouble,
                                     configured=store.configured(), page_url=page_url, qr=qr))


@apps_bp.route("/<key>/download")
@login_required
def download(key):
    return _send(_app(key))


@apps_bp.route("/<key>/latest.json")
def latest(key):
    app = _app(key)
    if not current_user.is_authenticated:
        return _no_cache((jsonify({"error": "Signed out - sign in again.", "signed_out": True}), 401))
    return _no_cache(jsonify(update_info(app, current_user)))


@apps_bp.route("/<key>/get")
def get(key):
    from app.extensions import db
    from app.models.user import User

    app = _app(key)
    try:
        data = _serializer().loads(request.args.get("t", ""), max_age=LINK_DAYS * 86400)
    except SignatureExpired:
        return _no_cache((render_template("apps/link_expired.html", app=app), 410))
    except BadSignature:
        abort(404)
    if data.get("a") != app.key:
        abort(404)
    if data.get("w"):
        from app.models.warehouse import WarehouseLogin

        person = db.session.get(WarehouseLogin, data["w"])
        if person is None or not person.is_active:
            abort(404)
        return _send(app)
    user = db.session.get(User, data.get("u"))
    if user is None or not user.is_active:
        abort(404)
    return _send(app)
