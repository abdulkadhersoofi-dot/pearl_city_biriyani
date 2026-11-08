"""The ARFA Warehouse app's API (/api/wh/v1). JSON only, no cookies, no CSRF -
every call carries "Authorization: Bearer <access token>".

Signing in (once per phone): POST /auth/login with the business code, the
login id and password gives a refresh token (only its hash is kept, one
WarehouseSession per phone) and an access token. After that the app opens
with the PIN: POST /auth/pin with the refresh token and the PIN gives a new
access token (12 hours; each PIN unlock replaces the last). Five wrong PINs
sign the phone out. Switching a login off, resetting its password or
signing the phone out on the website ends it on the next call.

The tenant, the login and the warehouses it may see always come from the
session - never from the request. Every order, line and pick is looked up
within that tenant and those warehouses; anything else is a 404.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta
from functools import wraps

from flask import current_app, g, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func

from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.media import UploadedImage
from app.models.mixins import utcnow
from app.models.sales import SalesDocument, SalesDocumentKind, SalesDocumentLine, SalesDocumentStatus
from app.models.tenant import Tenant
from app.models.warehouse import DispatchStatus, PickMethod, PickResult, SerialPick, WarehouseLogin, WarehouseSession
from app.utils.audit import record_audit
from app.warehouse import dispatch, labels, notify
from app.warehouse_api import warehouse_api_bp as api

ACCESS_HOURS = 12
PIN_TRIES = 5
PIN = re.compile(r"^[0-9]{4,6}$")
PHOTO_MAX = 1024 * 1024
PER_PAGE = 50
POLL_SECONDS = 30
IDLE_LOCK_MINUTES = 5


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _error(message: str, status: int = 400, **extra):
    return jsonify({"error": message, **extra}), status


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="warehouse-app-access")


def _access_token(session: WarehouseSession) -> str:
    return _serializer().dumps({"s": session.id, "g": session.generation})


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _tenant_blocked(tenant) -> str | None:
    from app.utils.billing import tenant_access_status

    if tenant is None:
        return "Business not found."
    if not dispatch.is_on(tenant):
        return "Warehouse dispatch isn't switched on for this business - ask the owner."
    status = tenant_access_status(tenant)
    return status.message if status.blocked else None


def _find_tenant(code: str) -> Tenant | None:
    code = (code or "").strip()
    if not code:
        return None
    tenant = Tenant.query.filter(func.lower(Tenant.login_slug) == code.lower()).first()
    if tenant is None and code.isdigit():
        tenant = db.session.get(Tenant, int(code))
    return tenant


def _me(session: WarehouseSession) -> dict:
    login = session.login
    tenant = db.session.get(Tenant, login.tenant_id)
    return {"login": {"id": login.id, "name": login.name, "login_id": login.login_id,
                      "manual_serial": login.manual_serial},
            "business": {"name": tenant.display_name},
            "warehouses": [{"id": w.id, "name": w.name} for w in login.locations if w.is_active],
            "pin_set": bool(login.pin_hash), "session_id": session.id,
            "poll_seconds": POLL_SECONDS, "idle_lock_minutes": IDLE_LOCK_MINUTES,
            "auto_release_minutes": dispatch.IDLE_RELEASE_MINUTES}


# --- signing in ----------------------------------------------------------------------------------

def _ip_failures(window: int) -> int:
    since = utcnow() - timedelta(minutes=window)
    return AuditLog.query.filter(AuditLog.action == "warehouse_login_failed", AuditLog.ip_address == request.remote_addr,
                                 AuditLog.created_at >= since).count()


@api.route("/auth/login", methods=["POST"])
def login():
    body = _body()
    cfg = current_app.config
    login_id = str(body.get("login_id") or "").strip()
    password = str(body.get("password") or "")
    if not login_id or not password or not str(body.get("business_code") or "").strip():
        return _error("Enter the business code, your login id and password.")
    if _ip_failures(cfg["LOGIN_RATE_LIMIT_WINDOW_MINUTES"]) >= cfg["LOGIN_IP_LIMIT"]:
        return _error("Too many attempts. Please try again in a few minutes.", 429)
    tenant = _find_tenant(body.get("business_code"))
    person = (WarehouseLogin.query.filter(WarehouseLogin.tenant_id == tenant.id,
                                          func.lower(WarehouseLogin.login_id) == login_id.lower()).first()
              if tenant is not None else None)
    if person is not None and person.locked_until and person.locked_until > utcnow():
        return _error("This login is locked after too many wrong passwords - try again later, or ask the owner "
                      "to reset the password.", 423)
    if person is None or not person.is_active or not person.check_password(password):
        if person is not None and person.is_active:
            person.failed_attempts = (person.failed_attempts or 0) + 1
            if person.failed_attempts >= cfg["LOGIN_LOCKOUT_ATTEMPTS"]:
                person.locked_until = utcnow() + timedelta(minutes=cfg["LOGIN_LOCKOUT_MINUTES"])
                person.failed_attempts = 0
        record_audit(None, "warehouse_login_failed", tenant_id=tenant.id if tenant else None,
                     details={"login_id": login_id[:40]})
        db.session.commit()
        return _error("The business code, login id or password isn't right.", 401)
    why = _tenant_blocked(tenant)
    if why:
        return _error(why, 403)
    if not person.location_ids:
        return _error("You aren't given a warehouse yet - ask the owner to tick yours on the website.", 403)
    person.failed_attempts = 0
    person.locked_until = None
    refresh = secrets.token_urlsafe(32)
    session = WarehouseSession(tenant_id=tenant.id, login_id=person.id, token_hash=_hash(refresh),
                               device_name=str(body.get("device_name") or request.headers.get("User-Agent") or "")[:120],
                               app_version=str(body.get("app_version") or "")[:20] or None, last_seen_at=utcnow(),
                               generation=1)
    db.session.add(session)
    person.last_seen_at = utcnow()
    db.session.flush()
    record_audit(None, "warehouse_app_signed_in", tenant_id=tenant.id, entity_type="warehouse_login",
                 entity_id=person.id, details={"login": person.name, "device": session.device_name})
    db.session.commit()
    return jsonify({"refresh_token": refresh, "access_token": _access_token(session),
                    "expires_in": ACCESS_HOURS * 3600, **_me(session)})


def _session_from_refresh(token: str) -> WarehouseSession | None:
    session = WarehouseSession.query.filter_by(token_hash=_hash(token)).first() if token else None
    if session is None or session.revoked_at is not None or not session.login.is_active:
        return None
    return session


@api.route("/auth/pin", methods=["POST"])
def unlock():
    """The PIN opens the app: a fresh access token for this phone."""
    body = _body()
    session = _session_from_refresh(str(body.get("refresh_token") or ""))
    if session is None:
        return _error("Signed out - sign in again.", 401, signed_out=True)
    login = session.login
    why = _tenant_blocked(db.session.get(Tenant, login.tenant_id))
    if why:
        return _error(why, 403)
    if not login.pin_hash:
        return _error("Choose a PIN first.", 409, pin_set=False)
    if not login.check_pin(str(body.get("pin") or "")):
        session.pin_failures = (session.pin_failures or 0) + 1
        left = PIN_TRIES - session.pin_failures
        if left <= 0:
            session.revoked_at = utcnow()
            session.revoke_reason = "wrong PIN too many times"
            record_audit(None, "warehouse_pin_locked", tenant_id=login.tenant_id, entity_type="warehouse_login",
                         entity_id=login.id, details={"device": session.device_name})
            db.session.commit()
            return _error("Too many wrong PINs - sign in again with your password.", 401, signed_out=True)
        db.session.commit()
        return _error(f"Wrong PIN - {left} {'try' if left == 1 else 'tries'} left.", 401, tries_left=left)
    session.pin_failures = 0
    session.generation = (session.generation or 0) + 1
    session.last_seen_at = utcnow()
    login.last_seen_at = utcnow()
    db.session.commit()
    return jsonify({"access_token": _access_token(session), "expires_in": ACCESS_HOURS * 3600, **_me(session)})


def signed_in(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        try:
            data = _serializer().loads(token, max_age=ACCESS_HOURS * 3600) if token else None
        except SignatureExpired:
            return _error("Enter your PIN.", 401, pin_required=True)
        except BadSignature:
            data = None
        session = db.session.get(WarehouseSession, data.get("s")) if isinstance(data, dict) else None
        if session is None or session.revoked_at is not None or not session.login.is_active:
            return _error("Signed out - sign in again.", 401, signed_out=True)
        if data.get("g") != session.generation:
            return _error("Enter your PIN.", 401, pin_required=True)
        tenant = db.session.get(Tenant, session.tenant_id)
        why = _tenant_blocked(tenant)
        if why:
            return _error(why, 403)
        now = utcnow()
        version = (request.headers.get("X-App-Version") or "")[:20]
        if session.last_seen_at is None or now - session.last_seen_at > timedelta(minutes=2) or \
                (version and version != session.app_version):
            session.last_seen_at = now
            session.login.last_seen_at = now
            if version:
                session.app_version = version
            db.session.commit()
        g.wh_session, g.wh_login, g.wh_tenant = session, session.login, tenant
        return view(*args, **kwargs)
    return wrapper


@api.route("/auth/refresh", methods=["POST"])
@signed_in
def refresh():
    """While the app is in use: a later expiry for the same unlock."""
    if _session_from_refresh(str(_body().get("refresh_token") or "")) is not g.wh_session:
        return _error("Signed out - sign in again.", 401, signed_out=True)
    return jsonify({"access_token": _access_token(g.wh_session), "expires_in": ACCESS_HOURS * 3600})


@api.route("/auth/pin", methods=["PUT"])
@signed_in
def set_pin():
    body = _body()
    pin = str(body.get("pin") or "")
    if not PIN.match(pin):
        return _error("A PIN is 4 to 6 digits.")
    login = g.wh_login
    if login.pin_hash and not login.check_pin(str(body.get("current_pin") or "")):
        return _error("Your current PIN isn't right.", 401)
    login.set_pin(pin)
    record_audit(None, "warehouse_pin_set", tenant_id=login.tenant_id, entity_type="warehouse_login", entity_id=login.id)
    db.session.commit()
    return jsonify({"ok": True, "pin_set": True})


@api.route("/auth/logout", methods=["POST"])
@signed_in
def logout():
    g.wh_session.revoked_at = utcnow()
    g.wh_session.revoke_reason = "signed out on the phone"
    db.session.commit()
    return jsonify({"ok": True})


@api.route("/auth/fcm-token", methods=["PUT"])
@signed_in
def fcm_token():
    token = str(_body().get("token") or "").strip()[:400] or None
    if token:
        # One phone, one session: an older session that had this token is stale.
        WarehouseSession.query.filter(WarehouseSession.fcm_token == token,
                                      WarehouseSession.id != g.wh_session.id).update(
            {WarehouseSession.fcm_token: None}, synchronize_session=False)
    g.wh_session.fcm_token = token
    db.session.commit()
    return jsonify({"ok": True})


@api.route("/me")
@signed_in
def me():
    return jsonify(_me(g.wh_session))


@api.route("/app/config")
def app_config():
    """What the app needs before signing in: the push (Firebase) settings,
    when the server has them - else the app keeps to polling."""
    import json

    push = None
    raw = current_app.config.get("WAREHOUSE_FCM_CONFIG") or ""
    if raw:
        try:
            cfg = json.loads(raw)
            if all(cfg.get(k) for k in ("app_id", "api_key", "project_id", "sender_id")):
                push = {k: str(cfg[k]) for k in ("app_id", "api_key", "project_id", "sender_id")}
        except ValueError:
            push = None
    return jsonify({"push": push, "poll_seconds": POLL_SECONDS, "idle_lock_minutes": IDLE_LOCK_MINUTES})


@api.route("/app/version")
@signed_in
def app_version():
    """The update check: the newest ARFA Warehouse on this site (app/apps)."""
    from app.apps.routes import update_info
    from app.apps.store import APPS

    response = jsonify(update_info(APPS["warehouse"], None, owner=g.wh_login))
    response.headers["Cache-Control"] = "private, no-store"
    return response


# --- orders -----------------------------------------------------------------------------------------

def _location_id() -> int:
    raw = request.args.get("location_id") or _body().get("location_id")
    allowed = g.wh_login.location_ids
    if raw in (None, "") and len(allowed) == 1:
        return next(iter(allowed))
    try:
        location_id = int(raw)
    except (TypeError, ValueError):
        raise _ApiAbort("Choose your warehouse.", 400)
    if location_id not in allowed:
        raise _ApiAbort("You don't work in that warehouse.", 403)
    return location_id


class _ApiAbort(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


@api.errorhandler(_ApiAbort)
def _abort(exc: _ApiAbort):
    return _error(exc.message, exc.status, **exc.extra)


@api.errorhandler(dispatch.DispatchError)
def _dispatch_error(exc: dispatch.DispatchError):
    db.session.rollback()
    return _error(exc.message, exc.status, **exc.extra)


def _order(doc_id: int, lock: bool = False) -> SalesDocument:
    query = SalesDocument.query.filter(SalesDocument.id == doc_id, SalesDocument.tenant_id == g.wh_tenant.id,
                                       SalesDocument.kind == SalesDocumentKind.SALES_ORDER,
                                       SalesDocument.dispatch_status.isnot(None),
                                       SalesDocument.dispatch_location_id.in_(g.wh_login.location_ids or [0]))
    if lock:
        query = query.populate_existing().with_for_update()
    doc = query.first()
    if doc is None:
        raise _ApiAbort("Order not found.", 404)
    return doc


def _iso(value):
    return value.isoformat() if value else None


def _card(doc: SalesDocument, progress: dict | None = None) -> dict:
    progress = progress if progress is not None else dispatch.line_progress(doc)
    me = g.wh_login
    units = sum(p["qty"] for l_id, p in progress.items())
    picked = sum(p["picked"] for p in progress.values())
    creator = doc.created_by
    return {
        "id": doc.id, "number": doc.number, "status": doc.dispatch_status.value,
        "cancelled": doc.status == SalesDocumentStatus.CANCELLED,
        "location_id": doc.dispatch_location_id,
        "branch": creator.name if creator is not None and creator.is_staff else "Office",
        "salesperson": creator.name if creator is not None else "",
        "customer": (doc.customer_snapshot or {}).get("name", ""),
        "items": [{"description": l.description, "qty": progress[l.id]["qty"]} for l in doc.lines],
        "units": units, "picked": picked,
        "created_at": _iso(doc.created_at),
        "age_minutes": int((utcnow() - doc.created_at).total_seconds() // 60) if doc.created_at else 0,
        "claimed_by": ({"id": doc.claimed_by.id, "name": doc.claimed_by.name} if doc.claimed_by else None),
        "mine": doc.claimed_by_id == me.id,
        "confirmed_at": _iso(doc.picked_confirmed_at),
    }


@api.route("/pipeline")
@signed_in
def pipeline():
    location_id = _location_id()
    if dispatch.auto_release(g.wh_tenant.id, location_id):
        notify.commit_and_push()
    tab = request.args.get("tab", "pipeline")
    query = SalesDocument.query.filter(SalesDocument.tenant_id == g.wh_tenant.id,
                                       SalesDocument.kind == SalesDocumentKind.SALES_ORDER,
                                       SalesDocument.dispatch_location_id == location_id)
    if tab == "mine":
        query = query.filter(SalesDocument.status == SalesDocumentStatus.OPEN,
                             SalesDocument.claimed_by_id == g.wh_login.id,
                             SalesDocument.dispatch_status.in_((DispatchStatus.PICKING, DispatchStatus.PICKED)))
        order = SalesDocument.claimed_at
    elif tab == "done":
        start = utcnow() - timedelta(hours=24)
        query = query.filter(SalesDocument.dispatch_status.in_((DispatchStatus.PICKED, DispatchStatus.INVOICED)),
                             SalesDocument.picked_confirmed_at >= start)
        order = SalesDocument.picked_confirmed_at.desc()
    else:
        query = query.filter(SalesDocument.status == SalesDocumentStatus.OPEN,
                             SalesDocument.dispatch_status.in_((DispatchStatus.TO_PICK, DispatchStatus.PICKING)))
        order = SalesDocument.created_at
    page = max(1, request.args.get("page", 1, type=int))
    rows = query.order_by(order, SalesDocument.id).offset((page - 1) * PER_PAGE).limit(PER_PAGE + 1).all()
    return jsonify({"orders": [_card(d) for d in rows[:PER_PAGE]], "page": page, "more": len(rows) > PER_PAGE,
                    "location_id": location_id, "server_time": _iso(utcnow())})


def _pick_json(p: SerialPick) -> dict:
    return {"id": p.id, "line_id": p.sales_line_id, "serial_no": p.serial_no, "result": p.result.value,
            "ok": p.is_good, "method": p.method.value, "client_pick_id": p.client_pick_id,
            "has_photo": bool(p.photo_id), "at": _iso(p.created_at), "by": p.login.name if p.login else None,
            "message": dispatch.message_for(p)}


@api.route("/orders/<int:doc_id>")
@signed_in
def order_detail(doc_id):
    doc = _order(doc_id)
    progress = dispatch.line_progress(doc)
    lines = []
    for line in doc.lines:
        product = line.product
        serials, truncated = dispatch.allowed_serials(doc, line)
        info = progress[line.id]
        lines.append({
            "id": line.id, "description": line.description, "qty": info["qty"], "picked": info["picked"],
            "needs_scan": dispatch.needs_scan(line),
            "product": ({"id": product.id, "name": product.name, "model_no": product.model_no or "",
                         "barcode": product.barcode or "", "sku": product.sku or "",
                         "track_serial": bool(product.track_serial)} if product else None),
            "allowed_serials": serials, "truncated": truncated,
            "picks": [_pick_json(p) for p in info["picks"]],
        })
    out = _card(doc, progress)
    out.update(lines=lines, notes=doc.notes or "", complete=dispatch.is_complete(doc),
               idle_minutes=dispatch.idle_minutes(doc) if doc.dispatch_status == DispatchStatus.PICKING else 0)
    return jsonify(out)


@api.route("/orders/<int:doc_id>/claim", methods=["POST"])
@signed_in
def claim(doc_id):
    doc = _order(doc_id)
    dispatch.claim(doc, g.wh_login)
    record_audit(None, "warehouse_order_claimed", tenant_id=doc.tenant_id, entity_type="sales_document",
                 entity_id=doc.id, details={"number": doc.number, "picker": g.wh_login.name})
    notify.commit_and_push()
    return jsonify(_card(doc))


@api.route("/orders/<int:doc_id>/release", methods=["POST"])
@signed_in
def release(doc_id):
    doc = _order(doc_id, lock=True)
    if doc.claimed_by_id != g.wh_login.id:
        raise _ApiAbort("Only the picker who took it (or a supervisor on the website) can release it.", 403)
    dispatch.release(doc, "released by picker")
    record_audit(None, "warehouse_order_released", tenant_id=doc.tenant_id, entity_type="sales_document",
                 entity_id=doc.id, details={"number": doc.number, "picker": g.wh_login.name})
    notify.commit_and_push()
    return jsonify(_card(doc))


@api.route("/orders/<int:doc_id>/confirm", methods=["POST"])
@signed_in
def confirm(doc_id):
    doc = _order(doc_id, lock=True)
    dispatch.confirm(doc, g.wh_login)
    record_audit(None, "warehouse_order_picked", tenant_id=doc.tenant_id, entity_type="sales_document",
                 entity_id=doc.id, details={"number": doc.number, "picker": g.wh_login.name})
    notify.commit_and_push()
    return jsonify({**_card(doc), "message": f"Sent to {doc.dispatch_location.name} computer for invoicing."})


@api.route("/orders/<int:doc_id>/short", methods=["POST"])
@signed_in
def short(doc_id):
    body = _body()
    doc = _order(doc_id, lock=True)
    line = _line(doc, body.get("line_id"))
    note = dispatch.short(doc, line, g.wh_login, str(body.get("reason") or ""))
    record_audit(None, "warehouse_unit_not_found", tenant_id=doc.tenant_id, entity_type="sales_document",
                 entity_id=doc.id, details={"number": doc.number, "note": note})
    db.session.commit()
    return jsonify({"ok": True, "note": note})


def _line(doc: SalesDocument, line_id) -> SalesDocumentLine:
    try:
        line_id = int(line_id)
    except (TypeError, ValueError):
        raise _ApiAbort("Which line?", 400)
    line = next((l for l in doc.lines if l.id == line_id), None)
    if line is None:
        raise _ApiAbort("That line isn't on this order.", 404)
    return line


def _method(value) -> PickMethod:
    try:
        return PickMethod(str(value or "barcode"))
    except ValueError:
        return PickMethod.BARCODE


@api.route("/orders/<int:doc_id>/lines/<int:line_id>/picks", methods=["POST"])
@signed_in
def add_pick(doc_id, line_id):
    body = _body()
    client_pick_id = str(body.get("client_pick_id") or "").strip()[:64] or None
    if client_pick_id:
        seen = SerialPick.query.filter_by(tenant_id=g.wh_tenant.id, client_pick_id=client_pick_id).first()
        if seen is not None:                                  # a retry: the same answer as the first time
            return jsonify({"pick": _pick_json(seen), "replayed": True})
    doc = _order(doc_id, lock=True)
    line = _line(doc, line_id)
    pick, _msg = dispatch.pick(doc, line, g.wh_login, text=str(body.get("serial_no") or body.get("text") or ""),
                               method=_method(body.get("method")), client_pick_id=client_pick_id,
                               allow_new=bool(body.get("allow_new")), session_id=g.wh_session.id)
    db.session.commit()
    progress = dispatch.line_progress(doc)[line.id]
    return jsonify({"pick": _pick_json(pick), "line": {"id": line.id, "qty": progress["qty"],
                                                       "picked": progress["picked"]},
                    "complete": dispatch.is_complete(doc)}), (201 if pick.is_good else 200)


def _pick(pick_id: int) -> SerialPick:
    pick = SerialPick.query.filter_by(id=pick_id, tenant_id=g.wh_tenant.id).first()
    if pick is None:
        raise _ApiAbort("Pick not found.", 404)
    _order(pick.sales_document_id)                            # within this login's warehouses
    return pick


@api.route("/picks/<int:pick_id>", methods=["DELETE"])
@signed_in
def undo_pick(pick_id):
    pick = _pick(pick_id)
    doc = _order(pick.sales_document_id, lock=True)
    if doc.claimed_by_id != g.wh_login.id:
        raise _ApiAbort("Only the picker who took this order can undo its scans.", 403)
    dispatch.undo(pick)
    record_audit(None, "warehouse_pick_undone", tenant_id=doc.tenant_id, entity_type="sales_document",
                 entity_id=doc.id, details={"number": doc.number, "serial": pick.serial_no})
    db.session.commit()
    return jsonify({"ok": True, "complete": dispatch.is_complete(doc)})


def _jpeg() -> bytes:
    file = request.files.get("photo")
    if file is None:
        raise _ApiAbort("Attach the photo.")
    data = file.read(PHOTO_MAX + 1)
    if len(data) > PHOTO_MAX:
        raise _ApiAbort("The photo is over 1 MB - the app sends it smaller.", 413)
    if not data.startswith(b"\xff\xd8\xff"):
        raise _ApiAbort("The photo must be a JPEG.", 415)
    return data


@api.route("/picks/<int:pick_id>/photo", methods=["POST"])
@signed_in
def pick_photo(pick_id):
    pick = _pick(pick_id)
    data = _jpeg()
    if pick.photo_id is None:
        image = UploadedImage(content_type="image/jpeg", data=data, is_private=True)
        db.session.add(image)
        db.session.flush()
        pick.photo_id = image.id
        unit = pick.serial
        if unit is not None and pick.is_good and unit.pick_photo_id is None:
            unit.pick_photo_id = image.id
        db.session.commit()
    return jsonify({"ok": True, "photo_id": pick.photo_id})


@api.route("/label/extract", methods=["POST"])
@signed_in
def label_extract():
    """A label photo read by ARFA: {model_no, serial_no, confidence}. Only a
    suggestion - the app sends it back as a pick, checked like any scan."""
    data = _jpeg()
    products, model_nos = [], []
    doc_id = request.form.get("order_id", type=int)
    if doc_id:
        doc = _order(doc_id)
        for line in doc.lines:
            if line.product is not None:
                products.append(line.product.name)
                model_nos.append(line.product.model_no or "")
    try:
        result = labels.extract(g.wh_tenant.id, data, products, model_nos)
    except labels.LabelError as exc:
        return _error(exc.message, exc.status)
    record_audit(None, "warehouse_label_read", tenant_id=g.wh_tenant.id, entity_type="warehouse_login",
                 entity_id=g.wh_login.id, details={"serial": result["serial_no"], "model_no": result["model_no"]})
    db.session.commit()
    return jsonify(result)


from app.warehouse_api import receiving  # noqa: E402,F401  (receive and count)
