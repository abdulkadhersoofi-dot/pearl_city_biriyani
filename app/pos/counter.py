"""Counter routes added in Phase 10: cashier PIN switch (feature
"cashier_pin") and the offline bill queue (feature "offline_pos",
app.pos.offline)."""

from flask import abort, jsonify, request
from flask_login import current_user

from app.auth.decorators import tenant_user_required
from app.extensions import db
from app.pos import cashiers, pos_bp
from app.utils.audit import record_audit
from app.utils.features import feature_required


@pos_bp.route("/cashier/unlock", methods=["POST"])
@tenant_user_required
@feature_required("cashier_pin")
def cashier_unlock():
    tenant = current_user.tenant
    try:
        cashier = cashiers.unlock(tenant, current_user, (request.form.get("pin") or "").strip())
    except cashiers.CashierError as exc:
        record_audit(current_user, "cashier_pin_failed", tenant_id=tenant.id)
        db.session.commit()
        return jsonify({"error": exc.message, "wait": cashiers.wait_seconds()}), 400
    record_audit(current_user, "cashier_signed_in", tenant_id=tenant.id, entity_type="cashier", entity_id=cashier.id)
    db.session.commit()
    return jsonify({"cashier": {"id": cashier.id, "name": cashier.name}})


@pos_bp.route("/cashier/lock", methods=["POST"])
@tenant_user_required
@feature_required("cashier_pin")
def cashier_lock():
    cashier_id = request.form.get("cashier_id")
    cashiers.lock()
    record_audit(current_user, "cashier_locked", tenant_id=current_user.tenant_id,
                 details={"cashier_id": cashier_id} if cashier_id else None)
    db.session.commit()
    return jsonify({"locked": True})


# --- offline billing (feature "offline_pos") ------------------------------------------------

def _offline_billing(view):
    """Offline bills come from a counter with offline billing on
    ("offline_pos"), or from the ARFA Counter app on a paired phone - the
    app always bills offline. Anyone else: 404, as for a module that's off."""
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        from app.pos.offline import phone_bills_offline
        from app.utils.features import current_tenant_has

        if not (current_tenant_has("offline_pos") or phone_bills_offline()):
            abort(404)
        return view(*args, **kwargs)

    return wrapped


def _device_from_request(tenant):
    from app.pos import offline

    return offline.authenticate(tenant, current_user, request.headers.get("X-Device-Id"),
                                request.headers.get("X-Device-Token"))


@pos_bp.route("/offline/register", methods=["POST"])
@tenant_user_required
@feature_required("offline_pos")
def offline_register():
    """A counter device asks for its own offline series (once)."""
    from app.pos import offline

    tenant = current_user.tenant
    data = request.get_json(silent=True) or {}
    try:
        device, token = offline.register_device(tenant, current_user, data.get("name"), data.get("platform"))
        state = offline.device_state(tenant, device)
    except offline.OfflineError as exc:
        db.session.rollback()
        return jsonify({"error": exc.message}), 400
    record_audit(current_user, "counter_device_registered", tenant_id=tenant.id, entity_type="counter_device",
                 entity_id=device.id, details={"code": device.code, "platform": device.platform})
    db.session.commit()
    return jsonify({**state, "token": token})


@pos_bp.route("/offline/state")
@tenant_user_required
@_offline_billing
def offline_state():
    """The device's next number, and a fresh CSRF token for sending bills
    (the cached page's token may have expired while offline)."""
    from flask_wtf.csrf import generate_csrf

    from app.pos import offline

    tenant = current_user.tenant
    try:
        device = _device_from_request(tenant)
        state = offline.device_state(tenant, device)
    except offline.OfflineError as exc:
        db.session.rollback()
        return jsonify({"error": exc.message, "unregistered": True}), 403
    db.session.commit()
    return jsonify({**state, "csrf_token": generate_csrf()})


@pos_bp.route("/offline/sync", methods=["POST"])
@tenant_user_required
@_offline_billing
def offline_sync():
    """The device's queued bills, oldest first. Each comes back synced,
    failed (kept on the server for the Client Admin) or rejected (not even
    recorded - the device keeps it and shows why)."""
    from app.models.platform import OfflineBillStatus
    from app.pos import offline

    tenant = current_user.tenant
    try:
        device = _device_from_request(tenant)
    except offline.OfflineError as exc:
        db.session.rollback()
        return jsonify({"error": exc.message, "unregistered": True}), 403
    bills = (request.get_json(silent=True) or {}).get("bills") or []
    results = []
    for payload in bills[:50]:
        if not isinstance(payload, dict):
            continue
        try:
            record, new = offline.sync_bill(tenant, current_user, device, payload)
        except offline.OfflineError as exc:
            db.session.rollback()
            results.append({"uuid": payload.get("uuid"), "status": "rejected", "error": exc.message})
            continue
        if not new:
            pass
        elif record.status == OfflineBillStatus.SYNCED and record.pos_bill_id:
            record_audit(current_user, "pos_bill_synced_offline", tenant_id=tenant.id, entity_type="pos_bill",
                         entity_id=record.pos_bill_id,
                         details={"bill_number": record.bill_number, "device": device.code,
                                  **({"total_mismatch": True} if record.total_mismatch else {})})
        elif record.status == OfflineBillStatus.FAILED:
            record_audit(current_user, "offline_bill_failed", tenant_id=tenant.id, entity_type="offline_bill",
                         entity_id=record.id, details={"bill_number": record.bill_number, "error": record.error})
        db.session.commit()
        results.append({"uuid": record.client_uuid, "status": record.status.value, "bill_number": record.bill_number,
                         "bill_id": record.pos_bill_id, "error": record.error})
    state = offline.device_state(tenant, device)
    db.session.commit()
    return jsonify({"results": results, "next_number": state["next_number"], "fy": state["fy"]})


@pos_bp.route("/sw.js")
def service_worker():
    """The POS service worker, served from /pos/ so it may control the POS
    pages (a script under /static/ couldn't). Only registered by the
    terminal when offline billing is on."""
    from flask import current_app, make_response, send_from_directory

    response = make_response(send_from_directory(current_app.static_folder, "js/pos_sw.js",
                                                 mimetype="application/javascript", max_age=0))
    response.headers["Service-Worker-Allowed"] = "/pos/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@pos_bp.route("/bills/<int:bill_id>/receipt.json")
@tenant_user_required
def receipt_json(bill_id):
    """The receipt as a text document for direct ESC/POS printing (the
    Android app); same original / DUPLICATE COPY rule as the PDF."""
    from app.models.mixins import utcnow
    from app.models.pos_bill import POSBill
    from app.pos.receipt_doc import receipt_doc
    from app.utils.tenant_scope import assert_owns, tenant_query

    bill = tenant_query(POSBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    original = False
    if request.args.get("original") == "1" and bill.original_printed_at is None and bill.bill_number:
        bill.original_printed_at = utcnow()
        db.session.commit()
        original = True
    return jsonify(receipt_doc(bill, current_user.tenant.display_name, None if original else "DUPLICATE COPY"))


# --- the Client Admin's view of devices and offline bills ------------------------------------

@pos_bp.route("/offline", methods=["GET", "POST"])
@tenant_user_required
def offline_bills():
    """The owner's review of bills made offline - by a counter with offline
    billing on, or by an ARFA Counter phone (which always can)."""
    from flask import abort, flash, redirect, render_template, url_for

    from app.models.platform import CounterDevice, OfflineBill, OfflineBillStatus
    from app.pos import offline
    from app.utils.tenant_scope import tenant_query

    if not offline.offline_review_on(current_user.tenant):
        abort(404)
    if not current_user.is_client_admin:
        abort(403)
    tenant = current_user.tenant
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action in ("device_off", "device_on"):
                device = tenant_query(CounterDevice).filter_by(id=request.form.get("device_id", type=int)).first_or_404()
                device.is_active = action == "device_on"
                record_audit(current_user, f"counter_{action}", tenant_id=tenant.id, entity_type="counter_device",
                             entity_id=device.id)
                message = f"{device.name} ({device.code}) {'can bill offline again' if device.is_active else 'is switched off for offline billing'}."
            else:
                record = tenant_query(OfflineBill).filter_by(id=request.form.get("record_id", type=int)).first_or_404()
                if action == "retry":
                    offline.retry(tenant, current_user, record)
                    record_audit(current_user, "offline_bill_retried", tenant_id=tenant.id, entity_type="offline_bill",
                                 entity_id=record.id, details={"status": record.status.value})
                    message = (f"{record.bill_number} is on the books." if record.status == OfflineBillStatus.SYNCED
                               else f"{record.bill_number} still can't be booked: {record.error}")
                elif action == "void":
                    offline.void_record(tenant, current_user, record, request.form.get("reason"))
                    record_audit(current_user, "offline_bill_cancelled", tenant_id=tenant.id,
                                 entity_type="offline_bill", entity_id=record.id,
                                 details={"bill_number": record.bill_number, "reason": record.resolution_note})
                    message = f"{record.bill_number} is recorded as a cancelled bill."
                else:
                    abort(400)
        except offline.OfflineError as exc:
            db.session.rollback()
            flash(exc.message, "error")
            return redirect(url_for("pos.offline_bills"))
        db.session.commit()
        flash(message, "success")
        return redirect(url_for("pos.offline_bills"))
    devices = tenant_query(CounterDevice).order_by(CounterDevice.code).all()
    attention = (tenant_query(OfflineBill)
                 .filter((OfflineBill.status == OfflineBillStatus.FAILED) | OfflineBill.total_mismatch.is_(True))
                 .order_by(OfflineBill.billed_at).all())
    recent = tenant_query(OfflineBill).order_by(OfflineBill.created_at.desc()).limit(50).all()
    return render_template("pos/offline.html", devices=devices, attention=attention, recent=recent)
