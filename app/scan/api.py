"""The ARFA Scan phone app's API (/api/scan/v1) - JSON, signed in with a
token per phone (ScanAppToken), never the website's cookie session.

Who uses it: the owner (Client Admin) and the branches (Staff logins) of a
business with Scan bills switched on. A branch sees the bills it sent; the
owner sees every one. Approving (posting) a bill is the owner's - and a
branch's own bills' too when the owner chose "branches approve"
(services.approvers). Everything the website does with a bill goes through
the same services here: reading, checks, supplier and item matching,
posting."""

from __future__ import annotations

import hashlib
import io
import secrets
from datetime import timedelta
from decimal import Decimal
from functools import wraps

from flask import Blueprint, Response, current_app, g, jsonify, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.extensions import csrf, db
from app.models.mixins import utcnow
from app.models.scan import (
    DISCARDED, FAILED, FILED, POSTED, READING, REJECTED, SPLIT, TO_CHECK, UPLOADED, ScanAppToken, ScanDocument,
)
from app.models.user import User
from app.scan import services
from app.scan.services import CATEGORY_LABELS, KIND_EXPENSE, KIND_FILE, KIND_PURCHASE, ScanError
from app.utils.audit import record_audit

scan_api_bp = Blueprint("scan_api", __name__, url_prefix="/api/scan/v1")
csrf.exempt(scan_api_bp)

TWO_FACTOR_SECONDS = 300
FEED_LIMIT = 200


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _stamp(user) -> str:
    return hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:16]


def _error(message: str, status: int = 400, **extra):
    return jsonify({"error": message, **extra}), status


# --- signing in ---------------------------------------------------------------------------------

def _blocked(user) -> str | None:
    """Why this login can't use the app right now (None when it can)."""
    from app.utils.billing import tenant_access_status

    if not services.may_scan(user):
        if user.tenant is not None and not user.tenant.has_feature("doc_scan"):
            return "Scan bills isn't switched on for your business - ask ARFA to turn it on."
        return "This app is for a business's owner and branches."
    status = tenant_access_status(user.tenant)
    if status.blocked:
        return status.message
    return None


def _issue(user) -> dict:
    token = secrets.token_urlsafe(32)
    device = (request.get_json(silent=True) or {}).get("device") or request.headers.get("User-Agent") or ""
    db.session.add(ScanAppToken(tenant_id=user.tenant_id, user_id=user.id, token_hash=_hash(token),
                                device_name=str(device)[:120], password_stamp=_stamp(user), last_used_at=utcnow()))
    user.last_login_at = utcnow()
    record_audit(user, "scan_app_signed_in", tenant_id=user.tenant_id, details={"device": str(device)[:120]})
    db.session.commit()
    return {"token": token, **_me(user)}


def _pending_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="scan-app-two-factor")


@scan_api_bp.route("/auth/login", methods=["POST"])
def login():
    from app.auth import security
    from app.auth.routes import _recent_failed_logins

    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    if not email or not password:
        return _error("Enter your email and password.")
    window = current_app.config["LOGIN_RATE_LIMIT_WINDOW_MINUTES"]
    if _recent_failed_logins(email, window) >= current_app.config["LOGIN_RATE_LIMIT_ATTEMPTS"] or \
            security.ip_failures(request.remote_addr, window) >= current_app.config["LOGIN_IP_LIMIT"]:
        return _error("Too many attempts. Please try again in a few minutes.", 429)
    user = User.query.filter_by(email=email).first()
    if user is not None and security.locked_until(user) is not None:
        return _error("This account is locked after too many wrong passwords - try again later, or ask your "
                      "administrator to reset the password.", 423)
    if user is None or not user.is_active or not user.check_password(password):
        if user is not None and user.is_active and security.register_failure(user):
            record_audit(user, "account_locked", tenant_id=user.tenant_id)
        record_audit(None, "login_failed", details={"email": email, "via": "scan_app"})
        db.session.commit()
        return _error("Incorrect email or password.", 401)
    security.clear_failures(user)
    why = _blocked(user)
    if why:
        db.session.commit()
        return _error(why, 403)
    if user.has_two_factor:
        db.session.commit()
        return jsonify({"two_factor": True, "pending": _pending_serializer().dumps({"u": user.id})})
    if security.two_factor_required(user):
        db.session.commit()
        return _error("Your account needs two-step sign-in - set it up on the website first, then sign in here.",
                      403)
    return jsonify(_issue(user))


@scan_api_bp.route("/auth/two-factor", methods=["POST"])
def two_factor():
    from app.auth import security

    body = request.get_json(silent=True) or {}
    try:
        data = _pending_serializer().loads(str(body.get("pending") or ""), max_age=TWO_FACTOR_SECONDS)
    except (BadSignature, SignatureExpired):
        return _error("That took too long - sign in again.", 401)
    user = db.session.get(User, data.get("u"))
    if user is None or not user.is_active or _blocked(user):
        return _error("Sign in again.", 401)
    if not security.verify_user_code(user, str(body.get("code") or "").strip()):
        record_audit(user, "two_factor_failed", tenant_id=user.tenant_id)
        if security.register_failure(user):
            record_audit(user, "account_locked", tenant_id=user.tenant_id)
        db.session.commit()
        return _error("That code isn't right.", 401)
    return jsonify(_issue(user))


def signed_in(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        row = ScanAppToken.query.filter_by(token_hash=_hash(token)).first() if token else None
        user = row.user if row is not None else None
        if row is None or row.revoked_at is not None or user is None or not user.is_active \
                or row.password_stamp != _stamp(user):
            return _error("Signed out - sign in again.", 401, signed_out=True)
        why = _blocked(user)
        if why:
            return _error(why, 403)
        now = utcnow()
        if row.last_used_at is None or now - row.last_used_at > timedelta(minutes=5):
            row.last_used_at = now
            db.session.commit()
        g.scan_user, g.scan_token = user, row
        return view(*args, **kwargs)
    return wrapper


@scan_api_bp.route("/auth/logout", methods=["POST"])
@signed_in
def logout():
    g.scan_token.revoked_at = utcnow()
    record_audit(g.scan_user, "scan_app_signed_out", tenant_id=g.scan_user.tenant_id)
    db.session.commit()
    return jsonify({"ok": True})


def _me(user) -> dict:
    tenant = user.tenant
    docs = services.documents_for(user)
    count = lambda *statuses: docs.filter(ScanDocument.status.in_(statuses)).count()  # noqa: E731
    return {"user": {"id": user.id, "name": user.name, "email": user.email, "owner": services.is_owner(user)},
            "business": {"name": tenant.display_name},
            "can_approve": services.can_approve(user), "approvers": services.approvers(tenant),
            "counts": {"to_check": count(TO_CHECK), "reading": count(UPLOADED, READING),
                       "retake": count(REJECTED, FAILED), "posted": count(POSTED)},
            "reading_on": services.is_set_up()}


@scan_api_bp.route("/me")
@signed_in
def me():
    return jsonify(_me(g.scan_user))


# --- the bills ----------------------------------------------------------------------------------

def _doc(doc_id) -> ScanDocument:
    doc = services.documents_for(g.scan_user).filter(ScanDocument.id == doc_id).first()
    if doc is None:
        from flask import abort
        abort(404)
    return doc


def _state(doc: ScanDocument) -> str:
    """One word the app colours a bill by."""
    if doc.status in (UPLOADED, READING):
        return "reading"
    if doc.status == TO_CHECK:
        if doc.duplicate_of:
            return "duplicate"
        return "passed" if (doc.validation or {}).get("status") == "verified" else "look"
    if doc.status in (REJECTED, FAILED):
        return "retake"
    return doc.status   # posted / filed / discarded


def _issues(doc: ScanDocument) -> list[dict]:
    checks = [c for c in (doc.validation or {}).get("checks") or [] if not c.get("ok")]
    return [{"label": c.get("label") or "", "detail": c.get("detail") or "", "hard": c.get("level") == "hard"}
            for c in checks]


def _bubble(doc: ScanDocument) -> dict:
    ex = doc.extracted or {}
    issues = _issues(doc)
    phone = doc.phone_read or {}
    return {
        "id": doc.id, "status": doc.status, "state": _state(doc),
        "supplier": services.party(doc)[0], "gstin": services.party(doc)[1],
        "invoice_number": ex.get("invoice_number") or "", "invoice_date": ex.get("invoice_date") or "",
        "total": ex.get("invoice_value"), "category": CATEGORY_LABELS.get(doc.category or "", doc.category or ""),
        "issues": len(issues), "issue": (issues[0]["label"] if issues else ""),
        "message": doc.message or "", "duplicate_of": doc.duplicate_of or "",
        "filename": doc.filename, "pages": doc.page_count or 1,
        "uploaded_at": doc.created_at.isoformat() if doc.created_at else None,
        "read_at": doc.read_at.isoformat() if doc.read_at else None,
        "posted_at": doc.posted_at.isoformat() if doc.posted_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        "from": doc.uploaded_by.name if doc.uploaded_by is not None else "",
        "qr": bool((ex.get("einvoice") or {}).get("present") or phone.get("qr")),
        "can_approve": doc.status == TO_CHECK and services.can_approve(g.scan_user, doc),
        "thumb": url_for("scan_api.thumb", doc_id=doc.id, v=int(doc.updated_at.timestamp()) if doc.updated_at else 0),
    }


@scan_api_bp.route("/bills")
@signed_in
def bills():
    """The chat: newest last. ?since=<iso> gives only bills changed since
    (the app polls with it); ?before=<id> pages back; ?show= filters."""
    from datetime import datetime

    query = services.documents_for(g.scan_user).filter(ScanDocument.status.notin_((SPLIT, DISCARDED)))
    show = request.args.get("show") or ""
    groups = {"to_check": (TO_CHECK,), "look": (TO_CHECK,), "retake": (REJECTED, FAILED),
              "reading": (UPLOADED, READING), "posted": (POSTED, FILED)}
    if show in groups:
        query = query.filter(ScanDocument.status.in_(groups[show]))
    if show == "look":
        query = query.filter(db.or_(ScanDocument.validation["status"].as_string() != "verified",
                                    ScanDocument.duplicate_of.isnot(None)))
    since = request.args.get("since")
    if since:
        try:
            query = services.documents_for(g.scan_user).filter(
                ScanDocument.updated_at > datetime.fromisoformat(since))
        except ValueError:
            return _error("since must be a date and time.")
    before = request.args.get("before", type=int)
    if before:
        query = query.filter(ScanDocument.id < before)
    q = (request.args.get("q") or "").strip()
    if q:
        from app.scan.routes import _apply_filters, _list_filters
        query = _apply_filters(query, _list_filters({"q": q}))
    docs = query.order_by(ScanDocument.id.desc()).limit(FEED_LIMIT).all()
    return jsonify({"bills": [_bubble(d) for d in reversed(docs)], "me": _me(g.scan_user),
                    "server_time": utcnow().isoformat(), "more": len(docs) == FEED_LIMIT})


@scan_api_bp.route("/bills", methods=["POST"])
@signed_in
def upload():
    """A bill from the phone: the photo or PDF, and what the phone read on
    it (ocr_text, qr). replaces=<id> retakes a bill that couldn't be read."""
    user = g.scan_user
    file = request.files.get("file")
    if file is None:
        return _error("Send the photo or PDF.")
    old = None
    replaces = request.form.get("replaces", type=int)
    if replaces:
        old = _doc(replaces)
    try:
        doc, existing = services.upload(user.tenant, user, file.filename or "bill.jpg", file.read())
        if not existing:
            doc.phone_read = services.phone_read_from(request.form.get("ocr_text"), request.form.get("qr"))
        if old is not None:
            services.retake(user, old, doc)
        db.session.commit()
    except ScanError as exc:
        db.session.rollback()
        return _error(exc.message)
    return jsonify({"bill": _bubble(doc), "existing": existing}), 200 if existing else 201


@scan_api_bp.route("/bills/<int:doc_id>/read", methods=["POST"])
@signed_in
def read(doc_id):
    doc = _doc(doc_id)
    if doc.status not in (UPLOADED, FAILED, READING):
        return jsonify({"bill": _bubble(doc)})
    try:
        docs = services.read_document(g.scan_user.tenant, g.scan_user, doc)
    except ScanError as exc:
        db.session.rollback()
        return _error(exc.message, 409, bill=_bubble(doc))
    return jsonify({"bill": _bubble(doc), "split": [_bubble(d) for d in docs if d.id != doc.id]})


FIELDS = (
    ("supplier_name", "Supplier", True), ("supplier_gstin", "GSTIN", True), ("invoice_number", "Invoice no.", True),
    ("invoice_date", "Date", True), ("taxable_value", "Taxable", False), ("cgst", "CGST", False),
    ("sgst", "SGST", False), ("igst", "IGST", False), ("tds", "TDS", True), ("invoice_value", "Total printed", True),
)


@scan_api_bp.route("/bills/<int:doc_id>")
@signed_in
def bill(doc_id):
    doc = _doc(doc_id)
    tenant, ex, v = g.scan_user.tenant, doc.extracted or {}, doc.validation or {}
    flagged = set(v.get("field_flags") or []) | set(ex.get("flags") or [])
    for c in v.get("checks") or []:
        if not c.get("ok") and c.get("id") in ("totals_match", "inv_number", "date_sane"):
            flagged.add({"totals_match": "invoice_value", "inv_number": "invoice_number",
                         "date_sane": "invoice_date"}[c["id"]])
    qr = (ex.get("einvoice") or {}).get("qr") or {}
    boxes = ex.get("boxes") or {}
    fields = []
    for key, label, editable in FIELDS:
        value = ex.get(key)
        if key in ("cgst", "sgst", "igst", "tds") and not value:
            continue
        fields.append({"key": key, "label": label, "value": "" if value is None else str(value),
                       "flagged": key in flagged or (key == "supplier_gstin" and value and not services.gstin_ok(value)),
                       "from_qr": bool(qr.get(key)), "editable": editable, "box": boxes.get(key)})
    rows = services.line_rows(doc) if doc.status not in (UPLOADED, READING) else []
    lines, taxable_sum, tax_sum = [], Decimal("0"), Decimal("0")
    for row in rows:
        _, taxable = services._row_taxable(row)
        tax = (taxable * Decimal(row["gst_rate"]) / 100).quantize(Decimal("0.01"))
        taxable_sum, tax_sum = taxable_sum + taxable, tax_sum + tax
        lines.append({"description": row["description"], "hsn": row["hsn"], "qty": row["qty"], "unit": row["unit"],
                      "rate": row["rate"], "discount": row["discount"], "disc_type": row.get("disc_type", "pct"),
                      "gst_rate": row["gst_rate"], "taxable": f"{taxable:.2f}", "amount": f"{taxable + tax:.2f}",
                      "serials": row.get("serials") or "", "box": row.get("box")})
    rcm = (ex.get("reverse_charge") or "").lower() in ("y", "yes", "true")
    total = (taxable_sum + (Decimal("0") if rcm else tax_sum)).quantize(Decimal("1"))
    printed = services._money(ex.get("invoice_value"))
    tds = services._money(ex.get("tds"))
    supplier = services.matched_supplier(tenant, doc) if rows else None
    body = _bubble(doc)
    body.update({
        "fields": fields, "issues_list": _issues(doc), "lines": lines,
        "totals": {"taxable": f"{taxable_sum:.2f}", "tax": f"{tax_sum:.2f}", "total": f"{total:.2f}",
                   "printed": f"{printed:.2f}", "tds": f"{tds:.2f}",
                   "difference": f"{services.total_difference(total, tds, printed):.2f}" if printed else "0.00"},
        "supplier_match": {"id": supplier.id, "name": supplier.name} if supplier is not None else None,
        "kind": services.suggested_kind(tenant, doc) if rows else KIND_FILE,
        "kinds": services.kinds_on(tenant), "kind_labels": services.KIND_LABELS,
        "note_type": services.note_type(doc), "original_number": services.original_number(doc),
        "page_url": url_for("scan_api.page", doc_id=doc.id, n=0, v=body["thumb"].split("v=")[-1]).replace(
            "/page/0.jpg", "/page/{n}.jpg"),
        "turned": bool((ex.get("turned") or {})),
    })
    return jsonify(body)


@scan_api_bp.route("/bills/<int:doc_id>/approve", methods=["POST"])
@signed_in
def approve(doc_id):
    user = g.scan_user
    doc = _doc(doc_id)
    if not services.can_approve(user, doc):
        return _error("Your owner approves the bills - this one is waiting for them.", 403)
    changes = request.get_json(silent=True) or {}
    form = services.approval_form(user.tenant, user, doc, changes)
    try:
        services.post(user.tenant, user, doc, form)
        db.session.commit()
    except ScanError as exc:
        db.session.rollback()
        return _error(exc.message, 409 if exc.code == "difference" else 422, code=exc.code)
    return jsonify({"bill": _bubble(doc), "me": _me(user)})


@scan_api_bp.route("/bills/<int:doc_id>/discard", methods=["POST"])
@signed_in
def discard(doc_id):
    doc = _doc(doc_id)
    if not services.is_owner(g.scan_user) and doc.uploaded_by_id != g.scan_user.id:
        return _error("Only the owner can discard this bill.", 403)
    try:
        services.discard(g.scan_user, doc)
        db.session.commit()
    except ScanError as exc:
        db.session.rollback()
        return _error(exc.message, 422)
    return jsonify({"bill": _bubble(doc)})


@scan_api_bp.route("/bills/<int:doc_id>/keep", methods=["POST"])
@signed_in
def keep(doc_id):
    doc = _doc(doc_id)
    try:
        services.file_document(g.scan_user, doc, (request.get_json(silent=True) or {}).get("notes") or "")
        db.session.commit()
    except ScanError as exc:
        db.session.rollback()
        return _error(exc.message, 422)
    return jsonify({"bill": _bubble(doc)})


@scan_api_bp.route("/bills/<int:doc_id>/turn", methods=["POST"])
@signed_in
def turn(doc_id):
    doc = _doc(doc_id)
    try:
        services.turn(g.scan_user, doc, (request.get_json(silent=True) or {}).get("deg") or 90)
        db.session.commit()
    except ScanError as exc:
        db.session.rollback()
        return _error(exc.message, 422)
    return jsonify({"bill": _bubble(doc)})


@scan_api_bp.route("/bills/<int:doc_id>/reread", methods=["POST"])
@signed_in
def reread(doc_id):
    doc = _doc(doc_id)
    try:
        services.reread(g.scan_user, doc)
        db.session.commit()
    except ScanError as exc:
        db.session.rollback()
        return _error(exc.message, 422)
    return jsonify({"bill": _bubble(doc)})


# --- pictures -------------------------------------------------------------------------------------

@scan_api_bp.route("/bills/<int:doc_id>/thumb.jpg")
@signed_in
def thumb(doc_id):
    from PIL import Image

    doc = _doc(doc_id)
    try:
        source = doc.preview or services.page_image(doc, 0)
    except Exception:  # noqa: BLE001 - a PDF that can't be drawn yet
        return Response(status=404)
    img = Image.open(io.BytesIO(source)).convert("RGB")
    img.thumbnail((360, 480))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=78, optimize=True)
    return Response(out.getvalue(), mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


@scan_api_bp.route("/bills/<int:doc_id>/page/<int:n>.jpg")
@signed_in
def page(doc_id, n):
    doc = _doc(doc_id)
    try:
        data = services.page_image(doc, n)
    except ScanError:
        return Response(status=404)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})
