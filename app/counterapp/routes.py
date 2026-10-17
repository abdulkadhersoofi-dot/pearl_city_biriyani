"""The ARFA Counter app's screens and its sign-in (counter app, stage 1).

  /app/          the app's start: pair this phone (not signed in), the
                 home screen (a branch login), or a note for any other login
  /app/pair      POST {code, name, platform} - use the owner's code
  /app/resume    POST {device_id, token} - a paired phone signs back in
  /app/unpair    POST - this phone stops being a counter device

Pairing and resuming are authenticated by the code / the device's secret,
not by a session, so they carry no CSRF token (there is no session yet to
protect); both are rate-limited per address.
"""

import time

from flask import after_this_request, jsonify, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user

from app.auth.routes import APP_COOKIE

from app.counterapp import counterapp_bp
from app.counterapp import pairing as pairing_service
from app.extensions import csrf, db
from app.models.mixins import utcnow
from app.models.user import UserRole
from app.utils.audit import record_audit


def _signed_in_as(user, device, method):
    @after_this_request
    def mark_phone(response):
        # This browser is a paired counter: a sign-out sends it back to
        # /app/ (which signs it in again), not to the email form.
        response.set_cookie(APP_COOKIE, "1", max_age=400 * 24 * 3600, samesite="Lax", httponly=True,
                            secure=request.is_secure)
        return response

    login_user(user, remember=True)
    session["last_active"] = time.time()
    session["counter_device_id"] = device.id
    user.last_login_at = utcnow()
    record_audit(user, "login_success", tenant_id=user.tenant_id,
                 details={"method": method, "device": device.code})


def _device_payload(device, token=None) -> dict:
    from app.pos import offline

    user = device.user
    payload = {"device_id": device.id, "code": device.code, "name": device.name, "tenant_id": user.tenant_id,
               "user_id": user.id, "branch": user.name, "business": user.tenant.display_name,
               "home": url_for("counterapp.index")}
    try:
        payload["next_number"] = offline.device_state(user.tenant, device)["next_number"]
    except Exception:  # no GSTIN / location yet - offline billing can't number, the rest still works
        payload["next_number"] = None
    if token:
        payload["token"] = token
    return payload


def _refused(exc: pairing_service.PairingError, detail: dict | None = None):
    record_audit(None, pairing_service.FAILED_ACTION, details=detail or {})
    db.session.commit()
    return jsonify({"error": exc.message}), exc.status


@counterapp_bp.route("/")
def index():
    if not current_user.is_authenticated:
        return render_template("counterapp/pair.html")
    if current_user.role != UserRole.STAFF:
        return render_template("counterapp/not_branch.html")
    from app.utils.features import current_tenant_has

    from datetime import datetime

    from app.einvoice.providers.common import IST

    tenant = current_user.tenant
    return render_template("counterapp/home.html", tenant=tenant, now_hour=datetime.now(IST).hour,
                           tables_on=current_tenant_has("restaurant_mode"),
                           offline_on=current_tenant_has("offline_pos"),
                           device_id=session.get("counter_device_id"))


@counterapp_bp.route("/pair", methods=["POST"])
@csrf.exempt
def pair():
    if pairing_service.too_many_attempts(request.remote_addr):
        return jsonify({"error": "Too many wrong codes from this network - wait 15 minutes and try again."}), 429
    data = request.get_json(silent=True) or {}
    try:
        device, token = pairing_service.pair(data.get("code"), (data.get("name") or "").strip()[:60],
                                             data.get("platform") or "app")
    except pairing_service.PairingError as exc:
        db.session.rollback()
        return _refused(exc, {"step": "pair"})
    user = device.user
    record_audit(user, "counter_device_paired", tenant_id=user.tenant_id, entity_type="counter_device",
                 entity_id=device.id, details={"code": device.code, "name": device.name, "platform": device.platform})
    _signed_in_as(user, device, "pairing code")
    db.session.commit()
    return jsonify(_device_payload(device, token))


@counterapp_bp.route("/resume", methods=["POST"])
@csrf.exempt
def resume():
    if pairing_service.too_many_attempts(request.remote_addr):
        return jsonify({"error": "Too many tries from this network - wait 15 minutes and try again."}), 429
    data = request.get_json(silent=True) or {}
    try:
        device = pairing_service.resume(data.get("device_id"), data.get("token"))
    except pairing_service.PairingError as exc:
        db.session.rollback()
        return _refused(exc, {"step": "resume", "device_id": str(data.get("device_id"))[:12]})
    _signed_in_as(device.user, device, "counter device")
    db.session.commit()
    return jsonify(_device_payload(device))


@counterapp_bp.route("/unpair", methods=["POST"])
def unpair():
    """This phone stops being a counter device: switched off on the
    business's list (bills it sent stay), and signed out."""
    from app.models.platform import CounterDevice

    if not current_user.is_authenticated:
        return jsonify({"ok": True})
    device_id = session.get("counter_device_id")
    device = CounterDevice.query.filter_by(id=device_id, tenant_id=current_user.tenant_id).first() if device_id else None
    if device is not None:
        device.is_active = False
        record_audit(current_user, "counter_device_unpaired", tenant_id=current_user.tenant_id,
                     entity_type="counter_device", entity_id=device.id, details={"code": device.code})
    db.session.commit()
    logout_user()
    session.clear()
    response = jsonify({"ok": True})
    response.delete_cookie(APP_COOKIE)
    return response


@counterapp_bp.route("/manifest.webmanifest")
def manifest():
    """So a phone browser can add the app to its home screen too."""
    response = jsonify({
        "name": "ARFA Counter", "short_name": "Counter", "start_url": url_for("counterapp.index"),
        "scope": url_for("counterapp.index"), "display": "standalone", "background_color": "#f4f7f2",
        "theme_color": "#1f7a4d",
        "icons": [{"src": url_for("static", filename="img/counter-icon.svg"), "sizes": "any", "type": "image/svg+xml"}],
    })
    response.mimetype = "application/manifest+json"
    return response
