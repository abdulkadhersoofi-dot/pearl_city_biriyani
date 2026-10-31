from datetime import timedelta

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.mixins import utcnow
from app.models.scan import (
    DISCARDED, FAILED, FILED, POSTED, READING, REJECTED, SPLIT, TO_CHECK, UPLOADED, ScanDocument,
)
from app.models.user import UserRole
from app.scan import scan_bp, services
from app.scan.services import CATEGORY_LABELS, KIND_EXPENSE, KIND_FILE, KIND_PURCHASE, ScanError
from app.utils.features import feature_required
from app.utils.tenant_scope import assert_owns, tenant_query

ADMIN = UserRole.CLIENT_ADMIN
TABS = {
    "check": ("To check", (TO_CHECK,)),
    "reading": ("Reading", (UPLOADED, READING, FAILED)),
    "done": ("Done", (POSTED, FILED)),
    "rejected": ("Rejected", (REJECTED,)),
}


def _tenant():
    return current_user.tenant


def _doc(doc_id) -> ScanDocument:
    doc = tenant_query(ScanDocument).filter_by(id=doc_id).first_or_404()
    assert_owns(doc)
    return doc


def _wants_json():
    return request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest" or \
        request.accept_mimetypes.best == "application/json"


def doc_json(doc: ScanDocument) -> dict:
    ex = doc.extracted or {}
    return {
        "id": doc.id, "status": doc.status, "title": doc.title, "filename": doc.filename,
        "category": doc.category, "category_label": CATEGORY_LABELS.get(doc.category or "", doc.category or ""),
        "supplier": ex.get("supplier_name"), "invoice_number": ex.get("invoice_number"),
        "invoice_date": ex.get("invoice_date"), "total": ex.get("invoice_value"),
        "check": (doc.validation or {}).get("status"), "message": doc.message, "duplicate_of": doc.duplicate_of,
        "review_url": url_for("scan.review", doc_id=doc.id), "parent_id": doc.parent_id,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


# --- the page --------------------------------------------------------------------------------

@scan_bp.route("/")
@roles_required(ADMIN)
@feature_required("doc_scan")
def index():
    tenant = _tenant()
    tab = request.args.get("tab") if request.args.get("tab") in TABS else None
    counts = {key: tenant_query(ScanDocument).filter(ScanDocument.status.in_(statuses)).count()
              for key, (_, statuses) in TABS.items()}
    if tab is None:
        tab = "check" if counts["check"] or not counts["reading"] else "reading"
    docs = (tenant_query(ScanDocument).filter(ScanDocument.status.in_(TABS[tab][1]))
            .order_by(ScanDocument.id.desc()).limit(200).all())
    # Waiting to be read - and any read cut off part-way (a restarted server)
    # for more than three minutes, which is read again.
    stale = utcnow() - timedelta(minutes=3)
    pending = [d.id for d in tenant_query(ScanDocument).filter(
        db.or_(ScanDocument.status == UPLOADED,
               db.and_(ScanDocument.status == READING, ScanDocument.updated_at < stale)))
        .order_by(ScanDocument.id).limit(100)]
    return render_template("scan/index.html", scan_tabs=TABS, tab=tab, counts=counts, docs=docs, pending=pending,
                           set_up=services.is_set_up(), used=services.reads_today(tenant.id),
                           limit=services.daily_limit(), labels=CATEGORY_LABELS)


# --- JSON: upload, read, list (the page's script, and a phone app) --------------------------------

@scan_bp.route("/upload", methods=["POST"])
@roles_required(ADMIN)
@feature_required("doc_scan")
def upload():
    file = request.files.get("file")
    if file is None:
        return jsonify({"error": "Choose a photo or PDF."}), 400
    try:
        doc, existing = services.upload(_tenant(), current_user, file.filename or "bill", file.read())
        db.session.commit()
    except ScanError as exc:
        db.session.rollback()
        return jsonify({"error": exc.message}), 400
    body = doc_json(doc)
    body["existing"] = existing
    if existing:
        body["note"] = "This exact file was uploaded before."
    return jsonify(body), 200 if existing else 201


@scan_bp.route("/<int:doc_id>/read", methods=["POST"])
@roles_required(ADMIN)
@feature_required("doc_scan")
def read(doc_id):
    doc = _doc(doc_id)
    try:
        docs = services.read_document(_tenant(), current_user, doc)
    except ScanError as exc:
        db.session.rollback()
        return jsonify({"error": exc.message, **doc_json(doc)}), 409
    if len(docs) == 1 and docs[0].id == doc.id:
        return jsonify(doc_json(doc))
    return jsonify({**doc_json(doc), "split": [doc_json(d) for d in docs]})


@scan_bp.route("/documents.json")
@roles_required(ADMIN)
@feature_required("doc_scan")
def documents_json():
    tab = request.args.get("tab", "check")
    statuses = TABS.get(tab, TABS["check"])[1]
    docs = (tenant_query(ScanDocument).filter(ScanDocument.status.in_(statuses))
            .order_by(ScanDocument.id.desc()).limit(200).all())
    return jsonify({"documents": [doc_json(d) for d in docs]})


@scan_bp.route("/<int:doc_id>.json")
@roles_required(ADMIN)
@feature_required("doc_scan")
def document_json(doc_id):
    doc = _doc(doc_id)
    return jsonify({**doc_json(doc), "extracted": doc.extracted, "validation": doc.validation})


# --- the pictures ----------------------------------------------------------------------------

@scan_bp.route("/<int:doc_id>/file")
@roles_required(ADMIN)
@feature_required("doc_scan")
def file(doc_id):
    doc = _doc(doc_id)
    ext = "pdf" if doc.is_pdf else "jpg"
    return Response(doc.file_data, mimetype=doc.content_type,
                    headers={"Content-Disposition": f'inline; filename="bill-{doc.id}.{ext}"',
                             "Cache-Control": "private, max-age=3600"})


@scan_bp.route("/<int:doc_id>/preview.jpg")
@roles_required(ADMIN)
@feature_required("doc_scan")
def preview(doc_id):
    doc = _doc(doc_id)
    if doc.preview:
        data = doc.preview
    elif not doc.is_pdf:
        data = doc.file_data
    else:
        abort(404)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


# --- checking and posting ----------------------------------------------------------------------

def _review_context(doc, form=None):
    from app.expenses.services import ensure_categories
    from app.models.expense import PAYMENT_MODE_LABELS
    from app.models.inventory import StockLocation
    from app.models.purchase import Supplier
    from app.models.tenant import Gstin

    tenant = _tenant()
    ex = doc.extracted or {}
    return {
        "doc": doc, "ex": ex, "v": doc.validation or {}, "form": form,
        "kind": (form or {}).get("kind") or services.suggested_kind(tenant, doc),
        "lines": _form_lines(form) or services.line_rows(doc), "main_rate": services.main_gst_rate(doc),
        "supplier": services.matched_supplier(tenant, doc),
        "suppliers": Supplier.query.filter_by(tenant_id=tenant.id, is_active=True).order_by(Supplier.name).all(),
        "gstins": tenant.gstins.filter(Gstin.is_active.is_(True)).order_by(Gstin.is_primary.desc(), Gstin.id).all(),
        "locations": StockLocation.query.filter_by(tenant_id=tenant.id, is_active=True)
        .order_by(StockLocation.is_default.desc(), StockLocation.name).all(),
        "categories": ensure_categories(tenant.id) if tenant.has_feature("expenses") else [],
        "payment_modes": PAYMENT_MODE_LABELS, "labels": CATEGORY_LABELS,
        "purchases_on": tenant.has_feature("purchases"), "expenses_on": tenant.has_feature("expenses"),
        "gstin_ok": services.gstin_ok, "posted_url": _posted_url(doc),
    }


def _form_lines(form):
    """The lines as the user left them, when a post was refused."""
    if not form or not hasattr(form, "getlist"):
        return None
    keys = {"description": "line_description[]", "hsn": "line_hsn[]", "qty": "line_qty[]", "unit": "line_unit[]",
            "rate": "line_rate[]", "discount": "line_discount[]", "gst_rate": "line_gst_rate[]"}
    columns = {k: form.getlist(v) for k, v in keys.items()}
    count = len(columns["description"])
    rows = [{k: (columns[k][i] if i < len(columns[k]) else "") for k in keys} for i in range(count)]
    return rows or None


def _posted_url(doc):
    if doc.posted_type == "purchase_bill":
        return url_for("purchases.view_bill", bill_id=doc.posted_id)
    if doc.posted_type == "expense":
        return url_for("expenses.view_expense", expense_id=doc.posted_id)
    return None


@scan_bp.route("/<int:doc_id>")
@roles_required(ADMIN)
@feature_required("doc_scan")
def review(doc_id):
    doc = _doc(doc_id)
    if doc.status == SPLIT:
        flash(doc.message or "This file was split into separate invoices.", "info")
        return redirect(url_for("scan.index"))
    return render_template("scan/review.html", **_review_context(doc))


@scan_bp.route("/<int:doc_id>/post", methods=["POST"])
@roles_required(ADMIN)
@feature_required("doc_scan")
def post(doc_id):
    doc = _doc(doc_id)
    kind = request.form.get("kind")
    try:
        if kind == KIND_PURCHASE:
            services.post_purchase(_tenant(), current_user, doc, request.form)
        elif kind == KIND_EXPENSE:
            services.post_expense(_tenant(), current_user, doc, request.form)
        elif kind == KIND_FILE:
            services.file_document(current_user, doc, request.form.get("notes", ""))
        else:
            raise ScanError("Choose what this bill is.")
        db.session.commit()
    except ScanError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        doc = _doc(doc_id)
        return render_template("scan/review.html", **_review_context(doc, request.form)), 422
    flash({KIND_PURCHASE: "Posted as a purchase bill.", KIND_EXPENSE: "Posted as an expense.",
           KIND_FILE: "Kept on file."}[kind], "success")
    return redirect(_next_url(doc))


def _next_url(doc):
    """Straight on to the next bill waiting to be checked."""
    nxt = (tenant_query(ScanDocument).filter(ScanDocument.status == TO_CHECK, ScanDocument.id != doc.id)
           .order_by(ScanDocument.id).first())
    return url_for("scan.review", doc_id=nxt.id) if nxt else url_for("scan.index", tab="done")


@scan_bp.route("/<int:doc_id>/discard", methods=["POST"])
@roles_required(ADMIN)
@feature_required("doc_scan")
def discard(doc_id):
    doc = _doc(doc_id)
    try:
        services.discard(current_user, doc)
        db.session.commit()
        flash("Discarded.", "success")
    except ScanError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(_next_url(doc) if doc.status == DISCARDED else url_for("scan.review", doc_id=doc.id))


@scan_bp.route("/<int:doc_id>/reread", methods=["POST"])
@roles_required(ADMIN)
@feature_required("doc_scan")
def reread(doc_id):
    doc = _doc(doc_id)
    try:
        services.reread(current_user, doc)
        db.session.commit()
        flash("It will be read again now.", "info")
    except ScanError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("scan.index", tab="reading"))
