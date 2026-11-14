"""The website side of warehouse dispatch.

  /warehouse/                     Ready to invoice - the warehouse computer's
                                  home (a branch login bound to a godown) and,
                                  for the owner, every warehouse's queue
  /warehouse/queue.json           what the queue page polls (toast + sound)
  /warehouse/orders/<id>          one order: picks, photos, invoice, slip
  /warehouse/orders/<id>/invoice  make the invoice from the scanned serials
  /warehouse/orders/<id>/release  supervisor: back to the pipeline
  /warehouse/orders/<id>/reassign supervisor: hand to another picker
  /warehouse/orders/<id>/slip.pdf delivery slip with serials per line
  /warehouse/logins               the ARFA Warehouse app logins (owner)
  /warehouse/locations/<id>       a location's warehouse settings
  /warehouse/free.json            free quantity per warehouse (order form)
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from weasyprint import HTML

from app.auth.decorators import roles_required
from app.extensions import db
from app.inventory import ledger
from app.models.inventory import LocationKind, StockLocation
from app.models.invoice import Invoice
from app.models.mixins import utcnow
from app.models.sales import SalesDocument, SalesDocumentKind, SalesDocumentStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.models.warehouse import (
    DISPATCH_STATUS_LABELS, PICK_RESULT_LABELS, DispatchStatus, SerialPick, WarehouseLogin, WarehouseSession,
)
from app.utils.audit import record_audit
from app.utils.entitlements import brand_logo_id, limit_message, limit_meter, may_add
from app.utils.features import feature_required
from app.utils.tenant_scope import tenant_query
from app.utils.uploads import image_data_uri
from app.warehouse import dispatch, notify, warehouse_bp

ADMIN = UserRole.CLIENT_ADMIN
STAFF = UserRole.STAFF
LOGIN_ID = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
MIN_PASSWORD = 8


def _audit(action, entity_type=None, entity_id=None, **details):
    record_audit(current_user, action, tenant_id=current_user.tenant_id, entity_type=entity_type,
                 entity_id=entity_id, details=details)


def computer_location(user) -> StockLocation | None:
    """The warehouse a branch login is the computer of (a godown bound to it)."""
    if user is None or not getattr(user, "is_staff", False):
        return None
    return StockLocation.query.filter_by(tenant_id=user.tenant_id, branch_user_id=user.id,
                                         kind=LocationKind.GODOWN, is_active=True).first()


def _scope() -> StockLocation | None:
    """None = the owner, every warehouse; else the one warehouse this
    computer works for. Anyone else is turned away."""
    if current_user.is_client_admin:
        return None
    location = computer_location(current_user)
    if location is None:
        abort(403)
    return location


def _order(doc_id) -> SalesDocument:
    doc = tenant_query(SalesDocument).filter_by(id=doc_id, kind=SalesDocumentKind.SALES_ORDER).first_or_404()
    if doc.dispatch_status is None:
        abort(404)
    location = _scope()
    if location is not None and doc.dispatch_location_id != location.id:
        abort(404)
    return doc


def business_code(tenant: Tenant) -> str:
    """What the app's sign-in asks for as the business code."""
    return tenant.login_slug or str(tenant.id)


# --- Ready to invoice ---------------------------------------------------------------------------

def _queue_rows(location: StockLocation | None, warehouse_id: int | None):
    query = tenant_query(SalesDocument).filter(SalesDocument.kind == SalesDocumentKind.SALES_ORDER,
                                               SalesDocument.status == SalesDocumentStatus.OPEN,
                                               SalesDocument.dispatch_status.isnot(None))
    if location is not None:
        query = query.filter(SalesDocument.dispatch_location_id == location.id)
    elif warehouse_id:
        query = query.filter(SalesDocument.dispatch_location_id == warehouse_id)
    rows = query.order_by(SalesDocument.picked_confirmed_at.desc().nullslast(), SalesDocument.id).all()
    # An EMI sale, once locked, is invoiced at the showroom - not here.
    ready = [d for d in rows if d.dispatch_status == DispatchStatus.PICKED and not d.is_finance_sale]
    waiting = [d for d in rows if d.dispatch_status in (DispatchStatus.TO_PICK, DispatchStatus.PICKING)]
    waiting.sort(key=lambda d: (d.priority != "high", d.created_at or utcnow()))
    return ready, waiting


def _deliver_rows(location: StockLocation | None, warehouse_id: int | None):
    """Invoiced (here, or an EMI sale at the showroom), waiting for the delivery check."""
    query = tenant_query(SalesDocument).filter(SalesDocument.kind == SalesDocumentKind.SALES_ORDER,
                                               SalesDocument.converted_invoice_id.isnot(None),
                                               SalesDocument.dispatch_status.in_((DispatchStatus.PICKED,
                                                                                  DispatchStatus.INVOICED)))
    if location is not None:
        query = query.filter(SalesDocument.dispatch_location_id == location.id)
    elif warehouse_id:
        query = query.filter(SalesDocument.dispatch_location_id == warehouse_id)
    return [d for d in query.order_by(SalesDocument.dispatch_activity_at).all() if dispatch.to_deliver(d)]


@warehouse_bp.route("/")
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def queue():
    location = _scope()
    dispatch.auto_release(current_user.tenant_id, location.id if location else None)
    notify.commit_and_push()
    warehouse_id = request.args.get("w", type=int)
    ready, waiting = _queue_rows(location, warehouse_id)
    progress = {d.id: dispatch.line_progress(d) for d in waiting}
    deliver = _deliver_rows(location, warehouse_id)
    swaps = [s for d in deliver for s in dispatch.pending_swaps(d)]
    recent = (tenant_query(SalesDocument).filter(SalesDocument.dispatch_status == DispatchStatus.DELIVERED)
              .filter(SalesDocument.dispatch_location_id == location.id if location else True)
              .order_by(SalesDocument.dispatch_activity_at.desc()).limit(15).all())
    logins = (tenant_query(WarehouseLogin).filter_by(is_active=True).order_by(WarehouseLogin.name).all())
    return render_template("warehouse/queue.html", ready=ready, waiting=waiting, progress=progress, recent=recent,
                           deliver=deliver, swaps=swaps,
                           location=location, warehouses=dispatch.warehouses(current_user.tenant_id),
                           warehouse_id=warehouse_id, logins=logins, labels=DISPATCH_STATUS_LABELS,
                           idle=dispatch.idle_minutes, release_after=dispatch.IDLE_RELEASE_MINUTES,
                           today=date.today())


@warehouse_bp.route("/queue.json")
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def queue_json():
    location = _scope()
    ready, waiting = _queue_rows(location, request.args.get("w", type=int))
    return jsonify({"ready": [{"id": d.id, "number": d.number, "customer": d.customer_snapshot.get("name"),
                               "warehouse": d.dispatch_location.name if d.dispatch_location else ""}
                              for d in ready],
                    "waiting": len(waiting)})


@warehouse_bp.route("/orders/<int:doc_id>")
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def order(doc_id):
    doc = _order(doc_id)
    attempts = (SerialPick.query.filter_by(tenant_id=doc.tenant_id, sales_document_id=doc.id)
                .order_by(SerialPick.id.desc()).limit(200).all())
    logins = [l for l in tenant_query(WarehouseLogin).filter_by(is_active=True).order_by(WarehouseLogin.name)
              if doc.dispatch_location_id in l.location_ids]
    from app.models.warehouse import DeliveryScan, SerialSwap

    deliveries = (DeliveryScan.query.filter_by(tenant_id=doc.tenant_id, sales_document_id=doc.id)
                  .order_by(DeliveryScan.id.desc()).limit(100).all())
    swaps = SerialSwap.query.filter_by(tenant_id=doc.tenant_id, sales_document_id=doc.id).order_by(SerialSwap.id).all()
    return render_template("warehouse/order.html", doc=doc, progress=dispatch.line_progress(doc),
                           attempts=attempts, result_labels=PICK_RESULT_LABELS, labels=DISPATCH_STATUS_LABELS,
                           logins=logins, idle=dispatch.idle_minutes(doc), today=date.today(),
                           eway=_eway_hint(doc), deliveries=deliveries, swaps=swaps,
                           expected=dispatch.invoiced_units(doc), checked=dispatch.delivered_ids(doc),
                           may_decide=_may_decide(doc))


def _may_decide(doc: SalesDocument) -> bool:
    """A swap is approved by the owner, or by the showroom login that made the order."""
    return current_user.is_client_admin or doc.created_by_id == current_user.id


@warehouse_bp.route("/swaps/<int:swap_id>", methods=["POST"])
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def decide_swap(swap_id):
    from app.models.warehouse import SerialSwap

    swap = tenant_query(SerialSwap).filter_by(id=swap_id).first_or_404()
    doc = tenant_query(SalesDocument).filter_by(id=swap.sales_document_id).first_or_404()
    if not _may_decide(doc):
        abort(403)
    approve = request.form.get("decision") == "approve"
    try:
        dispatch.decide_swap(swap, current_user, approve, request.form.get("note", ""))
    except dispatch.DispatchError as exc:
        flash(exc.message, "error")
        return redirect(request.referrer or url_for("warehouse.queue"))
    _audit("warehouse_swap_" + ("approved" if approve else "rejected"), "sales_document", doc.id, number=doc.number,
           old=swap.old_serial.serial_no, new=swap.new_serial.serial_no, reason=swap.reason)
    notify.refresh(doc, doc.dispatch_location_id, "swap")
    notify.commit_and_push()
    flash(f"Swap {'approved - the invoice stays, the delivery slip shows' if approve else 'rejected -'} "
          f"{swap.old_serial.serial_no} → {swap.new_serial.serial_no}.", "success")
    return redirect(request.referrer or url_for("warehouse.queue"))


def _eway_hint(doc: SalesDocument) -> bool:
    """Will (or does) this order's invoice want an e-way bill - the existing rule."""
    from app.einvoice import eway, rules

    if not current_user.tenant.has_feature("eway_bill"):
        return False
    invoice = doc.converted_invoice
    if invoice is not None:
        return eway.offer(current_user.tenant, invoice)
    return rules.eway_bill_needed(doc.grand_total)


@warehouse_bp.route("/orders/<int:doc_id>/invoice", methods=["POST"])
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def make_invoice(doc_id):
    from app.sales import services as sales_services

    doc = _order(doc_id)
    if doc.is_finance_sale:
        flash("An EMI sale is invoiced at the showroom, with the serials locked here.", "error")
        return redirect(url_for("warehouse.order", doc_id=doc.id))
    try:
        when = date.fromisoformat(request.form.get("invoice_date") or date.today().isoformat())
    except ValueError:
        when = date.today()
    try:
        invoice = sales_services.convert_to_invoice(current_user.tenant, current_user, doc, when)
    except sales_services.SalesError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("warehouse.order", doc_id=doc.id))
    record_audit(current_user, "invoice_created", tenant_id=doc.tenant_id, entity_type="invoice", entity_id=invoice.id,
                 details={"invoice_number": invoice.invoice_number, "from_document": doc.number,
                          "warehouse": doc.dispatch_location.name})
    _audit("sales_document_converted", "sales_document", doc.id, number=doc.number, invoice=invoice.invoice_number)
    db.session.commit()
    flash(f"Invoice {invoice.invoice_number} made from {doc.number} - print it with the delivery slip.", "success")
    return redirect(url_for("warehouse.order", doc_id=doc.id))


@warehouse_bp.route("/orders/<int:doc_id>/release", methods=["POST"])
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def release(doc_id):
    doc = _order(doc_id)
    who = doc.claimed_by.name if doc.claimed_by else None
    try:
        dispatch.release(doc, "released by supervisor")
    except dispatch.DispatchError as exc:
        flash(exc.message, "error")
        return redirect(request.referrer or url_for("warehouse.queue"))
    _audit("warehouse_order_released", "sales_document", doc.id, number=doc.number, picker=who)
    notify.commit_and_push()
    flash(f"{doc.number} is back in the pipeline - its scans are undone.", "success")
    return redirect(request.referrer or url_for("warehouse.queue"))


@warehouse_bp.route("/orders/<int:doc_id>/reassign", methods=["POST"])
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def reassign(doc_id):
    doc = _order(doc_id)
    login = tenant_query(WarehouseLogin).filter_by(id=request.form.get("login_id", type=int)).first()
    if login is None:
        flash("Pick who takes it over.", "error")
        return redirect(request.referrer or url_for("warehouse.queue"))
    before = doc.claimed_by.name if doc.claimed_by else None
    try:
        dispatch.reassign(doc, login)
    except dispatch.DispatchError as exc:
        flash(exc.message, "error")
        return redirect(request.referrer or url_for("warehouse.queue"))
    _audit("warehouse_order_reassigned", "sales_document", doc.id, number=doc.number, before=before, after=login.name)
    notify.commit_and_push()
    flash(f"{doc.number} handed to {login.name}.", "success")
    return redirect(request.referrer or url_for("warehouse.queue"))


@warehouse_bp.route("/orders/<int:doc_id>/slip.pdf")
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def slip(doc_id):
    doc = _order(doc_id)
    tenant = db.session.get(Tenant, doc.tenant_id)
    html = render_template("warehouse/slip_pdf.html", doc=doc, progress=dispatch.line_progress(doc),
                           swaps=dispatch.swaps_for_slip(doc),
                           firm_name=tenant.display_name, logo_data_uri=image_data_uri(brand_logo_id(tenant)))
    pdf = HTML(string=html, base_url=current_app.root_path).write_pdf()
    return send_file(BytesIO(pdf), mimetype="application/pdf", as_attachment=False,
                     download_name=f"slip-{doc.number.replace('/', '-')}.pdf")


@warehouse_bp.route("/orders/<int:doc_id>/invoice.pdf")
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def invoice_pdf(doc_id):
    from app.invoicing.pdf import render_invoice_pdf

    doc = _order(doc_id)
    invoice = db.session.get(Invoice, doc.converted_invoice_id) if doc.converted_invoice_id else None
    if invoice is None or invoice.tenant_id != doc.tenant_id:
        abort(404)
    return send_file(BytesIO(render_invoice_pdf(invoice)), mimetype="application/pdf", as_attachment=False,
                     download_name=f"{invoice.invoice_number.replace('/', '-')}.pdf")


@warehouse_bp.route("/photos/<int:pick_id>")
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def pick_photo(pick_id):
    from app.models.media import UploadedImage

    pick = tenant_query(SerialPick).filter_by(id=pick_id).first_or_404()
    _order(pick.sales_document_id)
    image = db.session.get(UploadedImage, pick.photo_id) if pick.photo_id else None
    if image is None:
        abort(404)
    return send_file(BytesIO(image.data), mimetype=image.content_type, max_age=3600)


# --- free quantity per warehouse (the order form) ------------------------------------------------

@warehouse_bp.route("/suggest.json")
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def suggest_json():
    """The order form's Warehouse picker: every warehouse with its distance to
    the delivery pincode and what it has free, ARFA's default and why, and a
    split plan when none has it all."""
    from types import SimpleNamespace

    from app.models.product import Product
    from app.warehouse import nearest

    ids = request.args.getlist("p")
    qtys = request.args.getlist("q")
    lines = []
    for i, pid in enumerate(ids[:60]):
        if not pid.isdigit():
            continue
        try:
            qty = Decimal(qtys[i]) if i < len(qtys) and qtys[i] else Decimal("1")
        except (InvalidOperation, ValueError):
            qty = Decimal("1")
        lines.append(SimpleNamespace(product_id=int(pid), qty=max(qty, Decimal("0"))))
    owned = {p.id for p in tenant_query(Product).filter(Product.id.in_([l.product_id for l in lines] or [0]))}
    lines = [l for l in lines if l.product_id in owned]
    home = ledger.location_for_user(current_user.tenant_id, current_user)
    suggestion = nearest.suggest_warehouse(current_user.tenant, request.args.get("pincode", ""), lines,
                                           fallback=home.default_dispatch_location,
                                           exclude_doc_id=request.args.get("exclude", type=int))
    db.session.rollback()          # location_for_user may have made the branch's location: nothing to keep here
    out = nearest.to_json(suggestion)
    names = {p.id: p.name for p in tenant_query(Product).filter(Product.id.in_(list(owned) or [0]))}
    out["products"] = [{"id": pid, "name": names[pid]} for pid in dict.fromkeys(l.product_id for l in lines)
                       if str(pid) in (out["warehouses"][0]["free"] if out["warehouses"] else {})]
    return jsonify(out)


@warehouse_bp.route("/free.json")
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def free_json():
    from app.models.product import Product

    ids = [int(p) for p in request.args.getlist("p") if p.isdigit()][:60]
    products = tenant_query(Product).filter(Product.id.in_(ids)).all() if ids else []
    ids = [p.id for p in products if ledger.sells_from_stock(p)]
    rows = dispatch.free_by_warehouse(current_user.tenant_id, ids, request.args.get("exclude", type=int))
    return jsonify({
        "products": [{"id": p.id, "name": p.name} for p in products if p.id in ids],
        "warehouses": [{"id": r["location"].id, "name": r["location"].name,
                        "free": {pid: float(v["free"]) for pid, v in r["items"].items()}} for r in rows],
    })


# --- the app logins (owner) ------------------------------------------------------------------------

def _login(login_id) -> WarehouseLogin:
    return tenant_query(WarehouseLogin).filter_by(id=login_id).first_or_404()


def _chosen_locations() -> list[StockLocation]:
    ids = [int(v) for v in request.form.getlist("location_ids") if v.isdigit()]
    found = [w for w in dispatch.warehouses(current_user.tenant_id) if w.id in ids]
    if not found:
        raise ValueError("Tick the warehouse(s) this person works in.")
    return found


def _check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"The password needs at least {MIN_PASSWORD} characters.")


def revoke_sessions(login: WarehouseLogin, reason: str) -> int:
    n = 0
    for session in login.sessions.filter(WarehouseSession.revoked_at.is_(None)).all():
        session.revoked_at = utcnow()
        session.revoke_reason = reason[:60]
        n += 1
    return n


@warehouse_bp.route("/logins", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def logins():
    tenant = current_user.tenant
    if request.method == "POST":
        try:
            name = (request.form.get("name") or "").strip()[:100]
            login_id = (request.form.get("login_id") or "").strip()
            if not name:
                raise ValueError("Give the person's name.")
            if not LOGIN_ID.match(login_id):
                raise ValueError("The login id is 3-40 letters, digits, dots or dashes (a mobile number works).")
            if tenant_query(WarehouseLogin).filter(db.func.lower(WarehouseLogin.login_id) == login_id.lower()).first():
                raise ValueError(f"There's already a login {login_id}.")
            password = request.form.get("password") or ""
            _check_password(password)
            locations = _chosen_locations()
            if not may_add(tenant, "warehouse_logins", current_user):
                raise ValueError(limit_message(tenant, "warehouse_logins"))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("warehouse.logins"))
        login = WarehouseLogin(tenant_id=tenant.id, name=name, login_id=login_id,
                               mobile=(request.form.get("mobile") or "").strip()[:15] or None,
                               manual_serial=bool(request.form.get("manual_serial")), created_by_id=current_user.id)
        login.set_password(password)
        login.locations = locations
        db.session.add(login)
        db.session.flush()
        _audit("warehouse_login_created", "warehouse_login", login.id, name=name, login_id=login_id,
               warehouses=[w.name for w in locations])
        db.session.commit()
        flash(f"{name} can now sign in to the ARFA Warehouse app with business code {business_code(tenant)}, "
              f"login id {login_id} and the password you set.", "success")
        return redirect(url_for("warehouse.logins"))
    rows = tenant_query(WarehouseLogin).order_by(WarehouseLogin.is_active.desc(), WarehouseLogin.name).all()
    sessions = {l.id: l.sessions.filter(WarehouseSession.revoked_at.is_(None))
                .order_by(WarehouseSession.last_seen_at.desc().nullslast()).all() for l in rows}
    return render_template("warehouse/logins.html", rows=rows, sessions=sessions,
                           warehouses=dispatch.warehouses(tenant.id), code=business_code(tenant),
                           meter=limit_meter(tenant, "warehouse_logins"), min_password=MIN_PASSWORD)


@warehouse_bp.route("/logins/<int:login_id>/edit", methods=["POST"])
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def edit_login(login_id):
    login = _login(login_id)
    try:
        locations = _chosen_locations()
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("warehouse.logins"))
    login.name = (request.form.get("name") or login.name).strip()[:100] or login.name
    login.mobile = (request.form.get("mobile") or "").strip()[:15] or None
    login.manual_serial = bool(request.form.get("manual_serial"))
    login.locations = locations
    _audit("warehouse_login_changed", "warehouse_login", login.id, name=login.name,
           warehouses=[w.name for w in locations], manual_serial=login.manual_serial)
    db.session.commit()
    flash(f"{login.name} saved.", "success")
    return redirect(url_for("warehouse.logins"))


@warehouse_bp.route("/logins/<int:login_id>/password", methods=["POST"])
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def reset_password(login_id):
    login = _login(login_id)
    password = request.form.get("password") or ""
    try:
        _check_password(password)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("warehouse.logins"))
    login.set_password(password)
    login.set_pin(None)
    phones = revoke_sessions(login, "password reset")
    _audit("warehouse_login_password_reset", "warehouse_login", login.id, name=login.name, phones_signed_out=phones)
    db.session.commit()
    flash(f"New password set for {login.name}; their phones are signed out and they choose a new PIN.", "success")
    return redirect(url_for("warehouse.logins"))


@warehouse_bp.route("/logins/<int:login_id>/toggle", methods=["POST"])
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def toggle_login(login_id):
    login = _login(login_id)
    if not login.is_active and not may_add(current_user.tenant, "warehouse_logins", current_user):
        flash(limit_message(current_user.tenant, "warehouse_logins"), "error")
        return redirect(url_for("warehouse.logins"))
    login.is_active = not login.is_active
    if not login.is_active:
        revoke_sessions(login, "login switched off")
        for doc in SalesDocument.query.filter_by(tenant_id=login.tenant_id, claimed_by_id=login.id,
                                                 dispatch_status=DispatchStatus.PICKING).all():
            dispatch.release(doc, "picker switched off")
    _audit("warehouse_login_" + ("activated" if login.is_active else "deactivated"), "warehouse_login", login.id,
           name=login.name)
    notify.commit_and_push()
    flash(f"{login.name} {'can sign in again' if login.is_active else 'is switched off and signed out'}.", "success")
    return redirect(url_for("warehouse.logins"))


@warehouse_bp.route("/sessions/<int:session_id>/revoke", methods=["POST"])
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def revoke_session(session_id):
    session = tenant_query(WarehouseSession).filter_by(id=session_id).first_or_404()
    if session.revoked_at is None:
        session.revoked_at = utcnow()
        session.revoke_reason = "signed out from the website"
        _audit("warehouse_phone_signed_out", "warehouse_session", session.id, login=session.login.name,
               device=session.device_name)
        db.session.commit()
    flash(f"{session.device_name or 'That phone'} is signed out.", "success")
    return redirect(url_for("warehouse.logins"))


# --- a location's warehouse settings ---------------------------------------------------------------

@warehouse_bp.route("/locations/<int:location_id>", methods=["POST"])
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def location_settings(location_id):
    location = tenant_query(StockLocation).filter_by(id=location_id).first_or_404()
    changes = {}
    if location.kind == LocationKind.GODOWN:
        from app.warehouse import nearest

        pincode = nearest.clean_pincode(request.form.get("pincode"))
        if request.form.get("pincode") is not None and request.form.get("pincode", "").strip() and not pincode:
            flash("A warehouse's pincode is 6 digits.", "error")
            return redirect(url_for("inventory.locations"))
        if request.form.get("pincode") is not None and pincode != (location.pincode or ""):
            location.pincode = pincode or None
            changes["pincode"] = pincode
        if "latitude" in request.form:
            try:
                lat = float(request.form["latitude"]) if request.form["latitude"].strip() else None
                lng = float(request.form["longitude"]) if request.form.get("longitude", "").strip() else None
            except ValueError:
                flash("Latitude and longitude are numbers, e.g. 8.7642 and 78.1348.", "error")
                return redirect(url_for("inventory.locations"))
            if (lat is None) != (lng is None) or (lat is not None and not (6 <= lat <= 38 and 68 <= lng <= 98)):
                flash("Give both latitude and longitude, inside India - or leave both empty to use the pincode's.",
                      "error")
                return redirect(url_for("inventory.locations"))
            if (lat, lng) != (location.latitude, location.longitude):
                location.latitude, location.longitude = lat, lng
                changes["point"] = [lat, lng]
        if "service_area" in request.form:
            try:
                codes = nearest.set_service_area(location, request.form.get("service_area", ""))
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "error")
                return redirect(url_for("inventory.locations"))
            changes["service_area"] = codes
        registered = bool(request.form.get("address_registered"))
        if registered != location.address_registered:
            location.address_registered = registered
            changes["address_registered"] = registered
        wanted = request.form.get("computer_user_id", type=int)
        if wanted != location.branch_user_id:
            try:
                _bind_computer(location, wanted)
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "error")
                return redirect(url_for("inventory.locations"))
            changes["warehouse_computer"] = wanted
    else:
        usual = request.form.get("default_dispatch_location_id", type=int)
        if usual and usual not in {w.id for w in dispatch.warehouses(current_user.tenant_id)}:
            usual = None
        if usual != location.default_dispatch_location_id:
            location.default_dispatch_location_id = usual
            changes["usual_warehouse"] = usual
    if changes:
        _audit("stock_location_warehouse_settings", "stock_location", location.id, name=location.name, **changes)
        db.session.commit()
        flash(f"{location.name} saved.", "success")
    return redirect(url_for("inventory.locations"))


def _bind_computer(location: StockLocation, user_id: int | None) -> None:
    """Make a branch login this warehouse's computer: its home becomes this
    warehouse's Ready to invoice. A branch location the login already had
    (made by its counter) must be empty - it's switched off."""
    if user_id is None:
        location.branch_user_id = None
        return
    user = tenant_query(User).filter_by(id=user_id, role=UserRole.STAFF, is_active=True).first()
    if user is None:
        raise ValueError("Pick one of your branch logins.")
    other = StockLocation.query.filter(StockLocation.tenant_id == location.tenant_id,
                                       StockLocation.branch_user_id == user.id,
                                       StockLocation.id != location.id).first()
    if other is not None:
        if other.kind == LocationKind.GODOWN:
            raise ValueError(f"{user.name} is already the computer of {other.name}.")
        if any(q != 0 for q in ledger.stock_on_hand(location.tenant_id, None, other.id).values()):
            raise ValueError(f"{user.name}'s own branch location still holds stock - move it first.")
        other.branch_user_id = None
        other.is_active = False
        db.session.flush()
    location.branch_user_id = user.id


from app.warehouse import emi, receiving, reports  # noqa: E402,F401  (their routes)
