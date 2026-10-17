import json
from datetime import date
from functools import wraps

from flask import Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.decorators import roles_required
from app.einvoice import einvoice_bp, eway, rules
from app.einvoice.providers import PROVIDERS
from app.einvoice.schema import EInvoiceDataError, build, payload_for, validate
from app.einvoice.services import (
    IRN_CANCEL_REASONS, EInvoiceError, GovApiError, call, cancel_irn, credential_for, generate_irn, record_irn,
    save_credential,
)
from app.extensions import db
from app.invoicing.services import InvoiceValidationError
from app.finance.filing import period_bounds
from app.models.einvoice import EWayBill, EWayBillStatus, GovApiCall
from app.models.inventory import StockTransfer
from app.models.invoice import Invoice, InvoiceStatus
from app.models.note import CreditDebitNote, NoteStatus
from app.models.sales import SalesDocument
from app.models.tenant import Gstin
from app.models.user import UserRole
from app.notes.services import NoteValidationError
from app.utils.audit import record_audit
from app.utils.entitlements import locked_for_client
from app.utils.features import current_tenant_has, feature_required
from app.utils.indian_states import STATE_NAME_BY_CODE
from app.utils.tenant_scope import assert_owns, tenant_query

ADMIN = UserRole.CLIENT_ADMIN
DOC_MODELS = {"invoice": Invoice, "note": CreditDebitNote}
SOURCE_MODELS = {"invoice": Invoice, "sales_document": SalesDocument, "stock_transfer": StockTransfer}


def either_feature(view):
    """e-Invoice settings serve both modules - 404 only when both are off."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not (current_tenant_has("einvoice") or current_tenant_has("eway_bill")):
            abort(404)
        return view(*args, **kwargs)

    return wrapped


def _tenant():
    return current_user.tenant


def _gstins():
    return (tenant_query(Gstin).filter_by(is_active=True).filter(Gstin.gstin.isnot(None))
            .order_by(Gstin.is_primary.desc(), Gstin.id).all())


def _doc(kind, doc_id):
    model = DOC_MODELS.get(kind) or abort(404)
    doc = tenant_query(model).filter_by(id=doc_id).first_or_404()
    assert_owns(doc)
    return doc


def _doc_url(doc):
    if isinstance(doc, Invoice):
        return url_for("invoicing.view_invoice", invoice_id=doc.id)
    return url_for("notes.view_note", note_id=doc.id)


def _source(source_type, source_id):
    model = SOURCE_MODELS.get(source_type) or abort(404)
    source = tenant_query(model).filter_by(id=source_id).first_or_404()
    assert_owns(source)
    return source


def source_url(source_type, source_id):
    if source_type == "invoice":
        return url_for("invoicing.view_invoice", invoice_id=source_id)
    if source_type == "sales_document":
        return url_for("sales.view_document", doc_id=source_id)
    return url_for("inventory.view_transfer", transfer_id=source_id)


def _flash_api(exc: EInvoiceError):
    if isinstance(exc, GovApiError):
        detail = f" (Details: {exc.code + ' - ' if exc.code else ''}{exc.original}; ref {exc.request_id[:8]})"
        flash(exc.message + detail, "error")
    else:
        flash(exc.message, "error")


def _json_download(data, filename):
    return Response(json.dumps(data, indent=2, ensure_ascii=False), mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _period():
    period = request.args.get("period") or date.today().strftime("%Y-%m")
    try:
        start, end = period_bounds(period)
    except (ValueError, TypeError):
        abort(404)
    return period, start, end


# --- the e-Invoice desk ----------------------------------------------------------------

def _pending(tenant, start, end, gstin_id=None):
    """Documents in the period that still need an IRN, each with its problems."""
    invoices = tenant_query(Invoice).filter(Invoice.status == InvoiceStatus.ISSUED, Invoice.irn.is_(None),
                                            Invoice.invoice_date >= start, Invoice.invoice_date <= end,
                                            Invoice.document_type.in_(rules.B2B_TYPES.keys()))
    notes = tenant_query(CreditDebitNote).filter(CreditDebitNote.status == NoteStatus.ISSUED,
                                                 CreditDebitNote.irn.is_(None), CreditDebitNote.note_date >= start,
                                                 CreditDebitNote.note_date <= end)
    if gstin_id:
        invoices = invoices.filter(Invoice.gstin_id == gstin_id)
        notes = notes.filter(CreditDebitNote.gstin_id == gstin_id)
    rows = []
    for doc in list(invoices.order_by(Invoice.invoice_date, Invoice.id)) + list(
            notes.order_by(CreditDebitNote.note_date, CreditDebitNote.id)):
        if not rules.needs_irn(tenant, doc):
            continue
        try:
            payload = build(tenant, doc)
            problems = validate(payload)
        except EInvoiceDataError as exc:
            payload, problems = None, exc.problems
        rows.append({"doc": doc, "kind": "invoice" if isinstance(doc, Invoice) else "note", "payload": payload,
                     "problems": problems, "warning": rules.deadline_warning(tenant, doc)})
    return rows


@einvoice_bp.route("/")
@roles_required(ADMIN)
@either_feature
def index():
    tenant = _tenant()
    if not tenant.has_feature("einvoice"):
        return redirect(url_for("einvoice.eway_bills"))
    period, start, end = _period()
    gstin_id = request.args.get("gstin_id", type=int)
    rows = _pending(tenant, start, end, gstin_id) if rules.tenant_in_scope(tenant) else []
    calls = (tenant_query(GovApiCall).order_by(GovApiCall.created_at.desc()).limit(15).all())
    return render_template("einvoice/index.html", rows=rows, period=period, gstins=_gstins(), gstin_id=gstin_id,
                           in_scope=rules.tenant_in_scope(tenant), calls=calls,
                           has_login={g.id: credential_for(tenant.id, g.id) is not None for g in _gstins()})


@einvoice_bp.route("/bulk.json")
@roles_required(ADMIN)
@feature_required("einvoice")
def bulk_json():
    """Every ready document of the period, as one file for the IRP's
    offline / bulk-upload tool (an array of INV-01 objects)."""
    tenant = _tenant()
    period, start, end = _period()
    gstin_id = request.args.get("gstin_id", type=int)
    ready = [r["payload"] for r in _pending(tenant, start, end, gstin_id) if not r["problems"]]
    if not ready:
        flash("Nothing ready to upload for that period.", "warning")
        return redirect(url_for("einvoice.index", period=period, gstin_id=gstin_id or ""))
    record_audit(current_user, "einvoice_json_downloaded", tenant_id=tenant.id,
                 details={"period": period, "documents": len(ready)})
    db.session.commit()
    return _json_download(ready, f"einvoice_{period}.json")


@einvoice_bp.route("/<any(invoice, note):kind>/<int:doc_id>.json")
@roles_required(ADMIN)
@feature_required("einvoice")
def document_json(kind, doc_id):
    doc = _doc(kind, doc_id)
    try:
        payload = payload_for(_tenant(), doc)
    except EInvoiceDataError as exc:
        flash("Can't make the e-Invoice JSON yet: " + " ".join(exc.problems), "error")
        return redirect(_doc_url(doc))
    number = (doc.invoice_number if kind == "invoice" else doc.note_number).replace("/", "-")
    return _json_download([payload], f"einvoice_{number}.json")


@einvoice_bp.route("/<any(invoice, note):kind>/<int:doc_id>/irn", methods=["POST"])
@roles_required(ADMIN)
@feature_required("einvoice")
def generate(kind, doc_id):
    doc = _doc(kind, doc_id)
    try:
        result = generate_irn(_tenant(), current_user, doc)
        db.session.commit()
        flash(f"IRN generated - Ack No {result.ack_no}.", "success")
    except EInvoiceDataError as exc:
        db.session.rollback()
        flash("Fix these first: " + " ".join(exc.problems), "error")
    except EInvoiceError as exc:
        db.session.rollback()
        _flash_api(exc)
    return redirect(_doc_url(doc))


@einvoice_bp.route("/<any(invoice, note):kind>/<int:doc_id>/irn/record", methods=["POST"])
@roles_required(ADMIN)
@feature_required("einvoice")
def record(kind, doc_id):
    doc = _doc(kind, doc_id)
    f = request.form
    try:
        record_irn(_tenant(), current_user, doc, f.get("irn"), f.get("ack_no"), f.get("ack_date"), f.get("signed_qr"))
        db.session.commit()
        flash("IRN saved.", "success")
    except EInvoiceError as exc:
        db.session.rollback()
        _flash_api(exc)
    return redirect(_doc_url(doc))


@einvoice_bp.route("/<any(invoice, note):kind>/<int:doc_id>/irn/cancel", methods=["POST"])
@roles_required(ADMIN)
@feature_required("einvoice")
def cancel(kind, doc_id):
    doc = _doc(kind, doc_id)
    f = request.form
    try:
        cancel_irn(_tenant(), current_user, doc, f.get("reason_code", ""), f.get("remark", ""),
                   on_portal=bool(f.get("on_portal")))
        db.session.commit()
        flash("IRN cancelled and the document voided.", "success")
    except EInvoiceError as exc:
        db.session.rollback()
        _flash_api(exc)
    except (InvoiceValidationError, NoteValidationError) as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(_doc_url(doc))


@einvoice_bp.route("/log")
@roles_required(ADMIN)
@either_feature
def log():
    calls = tenant_query(GovApiCall).order_by(GovApiCall.created_at.desc()).limit(300).all()
    return render_template("einvoice/log.html", calls=calls)


# --- settings ----------------------------------------------------------------------------

@einvoice_bp.route("/settings", methods=["GET", "POST"])
@roles_required(ADMIN)
@either_feature
def settings():
    tenant = _tenant()
    gstins = _gstins()
    if request.method == "POST" and locked_for_client(tenant, "gst_portal"):
        abort(403)
    if request.method == "POST":
        gstin = next((g for g in gstins if g.id == request.form.get("gstin_id", type=int)), None) or abort(404)
        action = request.form.get("action")
        if action == "address":
            pin = (request.form.get("pincode") or "").strip()
            if pin and not eway.PIN.match(pin):
                flash("A PIN code is 6 digits.", "error")
                return redirect(url_for("einvoice.settings"))
            gstin.registered_address = (request.form.get("registered_address") or "").strip() or None
            gstin.city = (request.form.get("city") or "").strip()[:100] or None
            gstin.pincode = pin or None
            record_audit(current_user, "gstin_address_updated", tenant_id=tenant.id, entity_type="gstin",
                         entity_id=gstin.id)
            db.session.commit()
            flash(f"Address saved for {gstin.gstin}.", "success")
        elif action == "login":
            try:
                save_credential(tenant.id, gstin, request.form.get("provider", ""), request.form, current_user)
                db.session.commit()
                flash(f"Login saved for {gstin.gstin} (stored encrypted). Use 'Test login' to check it.", "success")
            except EInvoiceError as exc:
                db.session.rollback()
                flash(exc.message, "error")
        elif action == "test":
            cred = credential_for(tenant.id, gstin.id)
            if cred is None:
                flash("Save a login first.", "error")
            else:
                try:
                    call(cred, current_user, "authenticate", None, "gstin", gstin.id)
                    db.session.commit()
                    flash("Logged in successfully.", "success")
                except EInvoiceError as exc:
                    db.session.rollback()
                    _flash_api(exc)
        elif action == "remove":
            cred = credential_for(tenant.id, gstin.id)
            if cred is not None:
                cred.is_active = False
                cred.session = None
                record_audit(current_user, "einvoice_credential_removed", tenant_id=tenant.id, entity_type="gstin",
                             entity_id=gstin.id)
                db.session.commit()
                flash("Login removed - the JSON download still works.", "success")
        return redirect(url_for("einvoice.settings"))
    creds = {g.id: credential_for(tenant.id, g.id) for g in gstins}
    return render_template("einvoice/settings.html", gstins=gstins, creds=creds, providers=PROVIDERS,
                           in_scope=rules.tenant_in_scope(tenant), gst_locked=locked_for_client(tenant, "gst_portal"))


# --- e-way bills ---------------------------------------------------------------------------

@einvoice_bp.route("/eway-bills")
@roles_required(ADMIN)
@feature_required("eway_bill")
def eway_bills():
    status = request.args.get("status", "")
    query = tenant_query(EWayBill)
    if status in {s.value for s in EWayBillStatus}:
        query = query.filter(EWayBill.status == EWayBillStatus(status))
    bills = query.order_by(EWayBill.created_at.desc()).limit(300).all()
    return render_template("einvoice/eway_list.html", bills=bills, status=status, source_url=source_url,
                           local=eway.local, now=eway.utcnow())


@einvoice_bp.route("/eway-bills/drafts.json")
@roles_required(ADMIN)
@feature_required("eway_bill")
def eway_drafts_json():
    drafts = tenant_query(EWayBill).filter(EWayBill.status == EWayBillStatus.DRAFT).order_by(EWayBill.id).all()
    ready = [b for b in drafts if not eway.problems(b)]
    if not ready:
        flash("No draft is complete enough to upload.", "warning")
        return redirect(url_for("einvoice.eway_bills"))
    return _json_download(eway.bulk_json(ready), f"ewaybills_{date.today().isoformat()}.json")


@einvoice_bp.route("/eway-bills/new/<any(invoice, sales_document, stock_transfer):source_type>/<int:source_id>",
                   methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("eway_bill")
def eway_new(source_type, source_id):
    tenant = _tenant()
    source = _source(source_type, source_id)
    if not eway.eligible(source) or not source.gstin or not source.gstin.gstin:
        flash("An e-way bill can only be made for a live document from a registered GSTIN.", "error")
        return redirect(source_url(source_type, source_id))
    existing = eway.active_for(tenant.id, source)
    if existing is not None:
        return redirect(url_for("einvoice.eway_view", ewb_id=existing.id))
    ewb = eway.draft_from(tenant, source, current_user)
    if request.method == "POST":
        try:
            eway.apply_form(ewb, request.form)
            db.session.add(ewb)
            db.session.flush()
            record_audit(current_user, "ewb_draft_created", tenant_id=tenant.id, entity_type="eway_bill",
                         entity_id=ewb.id, details={"source": f"{source_type}:{source_id}"})
            db.session.commit()
            flash("e-Way bill saved as a draft.", "success")
            return redirect(url_for("einvoice.eway_view", ewb_id=ewb.id))
        except EInvoiceError as exc:
            db.session.rollback()
            flash(exc.message, "error")
    return _eway_form(ewb, source, editing=True)


def _eway_form(ewb, source=None, editing=False):
    return render_template(
        "einvoice/eway_view.html", ewb=ewb, editing=editing, problems=eway.problems(ewb) if ewb.id else [],
        value=ewb.total_invoice_value, threshold_needed=rules.eway_bill_needed(ewb.total_invoice_value),
        source_link=source_url(ewb.source_type, ewb.source_id), sub_supply=eway.SUB_SUPPLY,
        trans_modes=eway.TRANS_MODES, vehicle_types=eway.VEHICLE_TYPES, cancel_reasons=eway.CANCEL_REASONS,
        part_b_reasons=eway.PART_B_REASONS, doc_types=eway.DOC_TYPE_LABELS, states=STATE_NAME_BY_CODE,
        has_login=credential_for(ewb.tenant_id, ewb.gstin_id) is not None, by_irn=bool(eway._irn_of(ewb)),
        can_cancel=rules.can_cancel_ewb(ewb), local=eway.local, now=eway.utcnow(),
        days=rules.eway_validity_days(ewb.distance_km or 1, ewb.vehicle_type == "O"),
    )


def _ewb(ewb_id):
    ewb = tenant_query(EWayBill).filter_by(id=ewb_id).first_or_404()
    assert_owns(ewb)
    return ewb


@einvoice_bp.route("/eway-bills/<int:ewb_id>", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("eway_bill")
def eway_view(ewb_id):
    ewb = _ewb(ewb_id)
    editing = request.args.get("edit") == "1" and ewb.status == EWayBillStatus.DRAFT
    if request.method == "POST":
        try:
            eway.apply_form(ewb, request.form)
            record_audit(current_user, "ewb_draft_updated", tenant_id=ewb.tenant_id, entity_type="eway_bill",
                         entity_id=ewb.id)
            db.session.commit()
            flash("Draft saved.", "success")
            return redirect(url_for("einvoice.eway_view", ewb_id=ewb.id))
        except EInvoiceError as exc:
            db.session.rollback()
            flash(exc.message, "error")
            editing = True
    return _eway_form(ewb, editing=editing)


@einvoice_bp.route("/eway-bills/<int:ewb_id>.json")
@roles_required(ADMIN)
@feature_required("eway_bill")
def eway_json(ewb_id):
    ewb = _ewb(ewb_id)
    found = eway.problems(ewb)
    if found:
        flash("Fix these first: " + " ".join(found), "error")
        return redirect(url_for("einvoice.eway_view", ewb_id=ewb.id))
    return _json_download(eway.bulk_json([ewb]), f"ewaybill_{ewb.document_number.replace('/', '-')}.json")


def _eway_action(ewb_id, fn, success):
    ewb = _ewb(ewb_id)
    try:
        fn(ewb)
        db.session.commit()
        flash(success, "success")
    except EInvoiceError as exc:
        db.session.rollback()
        _flash_api(exc)
    return redirect(url_for("einvoice.eway_view", ewb_id=ewb_id))


@einvoice_bp.route("/eway-bills/<int:ewb_id>/generate", methods=["POST"])
@roles_required(ADMIN)
@feature_required("eway_bill")
def eway_generate(ewb_id):
    return _eway_action(ewb_id, lambda e: eway.generate(_tenant(), current_user, e), "e-Way bill generated.")


@einvoice_bp.route("/eway-bills/<int:ewb_id>/record", methods=["POST"])
@roles_required(ADMIN)
@feature_required("eway_bill")
def eway_record(ewb_id):
    f = request.form
    return _eway_action(ewb_id, lambda e: eway.record(current_user, e, f.get("ewb_no"), f.get("ewb_date")),
                        "e-Way bill number saved.")


@einvoice_bp.route("/eway-bills/<int:ewb_id>/part-b", methods=["POST"])
@roles_required(ADMIN)
@feature_required("eway_bill")
def eway_part_b(ewb_id):
    f = request.form
    return _eway_action(ewb_id, lambda e: eway.update_part_b(_tenant(), current_user, e, f,
                                                             on_portal=bool(f.get("on_portal"))),
                        "Vehicle updated.")


@einvoice_bp.route("/eway-bills/<int:ewb_id>/cancel", methods=["POST"])
@roles_required(ADMIN)
@feature_required("eway_bill")
def eway_cancel(ewb_id):
    f = request.form
    return _eway_action(ewb_id, lambda e: eway.cancel(_tenant(), current_user, e, f.get("reason_code", ""),
                                                      f.get("remark", ""), on_portal=bool(f.get("on_portal"))),
                        "e-Way bill cancelled.")


# --- the panels other pages include ------------------------------------------------------

def irn_panel(doc):
    """What the invoice / note page shows about its IRN (None = nothing)."""
    tenant = current_user.tenant if current_user.is_authenticated else None
    if tenant is None or not tenant.has_feature("einvoice"):
        return None
    kind = "invoice" if isinstance(doc, Invoice) else "note"
    panel = {"kind": kind, "needs": rules.needs_irn(tenant, doc), "warning": None, "problems": [],
             "can_cancel": rules.can_cancel_irn(doc), "reasons": IRN_CANCEL_REASONS,
             "has_login": credential_for(doc.tenant_id, doc.gstin_id) is not None,
             "credit_note_url": None}
    if panel["needs"]:
        panel["warning"] = rules.deadline_warning(tenant, doc)
        try:
            panel["problems"] = validate(build(tenant, doc))
        except EInvoiceDataError as exc:
            panel["problems"] = exc.problems
    if doc.irn and not doc.irn_cancelled_at and not panel["can_cancel"] and kind == "invoice" and doc.customer_id:
        panel["credit_note_url"] = url_for("notes.new_note", invoice_id=doc.id, type="credit")
    return panel


def eway_panel(source):
    tenant = current_user.tenant if current_user.is_authenticated else None
    if tenant is None or not tenant.has_feature("eway_bill"):
        return None
    existing = (EWayBill.query.filter_by(tenant_id=tenant.id, source_type=eway.source_type_of(source),
                                         source_id=source.id).order_by(EWayBill.id.desc()).all())
    if not existing and not eway.offer(tenant, source):
        return None
    return {"bills": existing, "offer": eway.offer(tenant, source) and eway.active_for(tenant.id, source) is None,
            "new_url": url_for("einvoice.eway_new", source_type=eway.source_type_of(source), source_id=source.id),
            "value": eway.source_value(source), "local": eway.local}
