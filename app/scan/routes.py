import re
from datetime import date, datetime, time as dtime, timedelta

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.mixins import utcnow
from app.models.scan import (
    DISCARDED, FAILED, FILED, POSTED, READING, REJECTED, SPLIT, TO_CHECK, UPLOADED, ScanDocument,
)
from app.models.user import UserRole
from app.purchases.services import TDS_SECTIONS
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
    # Bills to check run in the order they were uploaded, to be taken one by one.
    order = ScanDocument.id.asc() if tab == "check" else ScanDocument.id.desc()
    in_tab = tenant_query(ScanDocument).filter(ScanDocument.status.in_(TABS[tab][1]))
    filters = _list_filters(request.args)
    found = _apply_filters(in_tab, filters)
    total = found.count()
    docs = found.order_by(order).limit(LIST_LIMIT).all()
    # Waiting to be read - and any read cut off part-way (a restarted server)
    # for more than three minutes, which is read again.
    stale = utcnow() - timedelta(minutes=3)
    pending = [d.id for d in tenant_query(ScanDocument).filter(
        db.or_(ScanDocument.status == UPLOADED,
               db.and_(ScanDocument.status == READING, ScanDocument.updated_at < stale)))
        .order_by(ScanDocument.id).limit(100)]
    return render_template("scan/index.html", scan_tabs=TABS, tab=tab, counts=counts, docs=docs, pending=pending,
                           set_up=services.is_set_up(), used=services.reads_today(tenant.id),
                           limit=services.daily_limit(), labels=CATEGORY_LABELS, filters=filters, total=total,
                           shown_limit=LIST_LIMIT, suppliers=_supplier_options(in_tab), checks=CHECKS, spans=UPLOAD_SPANS,
                           kinds=_kind_options(in_tab), posted_urls={d.id: _posted_url(d) for d in docs},
                           approvers=services.approvers(tenant), approver_choices=services.APPROVERS,
                           party=services.party)


@scan_bp.route("/approvers", methods=["POST"])
@roles_required(ADMIN)
@feature_required("doc_scan")
def set_approvers():
    try:
        services.set_approvers(_tenant(), current_user, request.form.get("approvers", ""))
        db.session.commit()
        flash("Saved - the ARFA Scan app follows it straight away.", "success")
    except ScanError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("scan.index", tab=request.form.get("tab") or None))


# --- the list's search and filters (also done in the page itself, as you type) --------------------

LIST_LIMIT = 1000
CHECKS = {"passed": "All checks passed", "look": "Something to look at", "dup": "Looks already entered"}
UPLOAD_SPANS = {"today": "Today", "7": "Last 7 days", "30": "Last 30 days", "range": "Between dates"}


def _list_filters(args) -> dict:
    def day(key):
        try:
            return date.fromisoformat((args.get(key) or "").strip())
        except ValueError:
            return None
    return {"q": (args.get("q") or "").strip()[:100], "supplier": (args.get("supplier") or "").strip()[:255],
            "check": args.get("check") if args.get("check") in CHECKS else "",
            "kind": (args.get("kind") or "").strip()[:40],
            "uploaded": args.get("uploaded") if args.get("uploaded") in UPLOAD_SPANS else "",
            "from": day("from"), "to": day("to")}


def _ist_start(d: date) -> datetime:
    from app.einvoice.providers.common import IST

    return datetime.combine(d, dtime.min).replace(tzinfo=IST)


def _apply_filters(query, f: dict):
    ex = ScanDocument.extracted
    text = lambda key: ex[key].as_string()  # noqa: E731
    for word in f["q"].split():
        like = f"%{word}%"
        digits = re.sub(r"[,\s₹]|rs\.?", "", word, flags=re.I)
        options = [ScanDocument.filename.ilike(like), text("supplier_name").ilike(like),
                   text("supplier_gstin").ilike(like), text("invoice_number").ilike(like),
                   text("invoice_date").ilike(like)]
        if re.fullmatch(r"\d+(\.\d+)?", digits or ""):
            options += [text("invoice_value").like(f"%{digits}%"), text("taxable_value").like(f"%{digits}%")]
        query = query.filter(db.or_(*options))
    if f["supplier"]:
        query = query.filter(db.or_(text("supplier_gstin") == f["supplier"], text("supplier_name") == f["supplier"]))
    if f["check"] == "passed":
        query = query.filter(ScanDocument.validation["status"].as_string() == "verified",
                             ScanDocument.duplicate_of.is_(None))
    elif f["check"] == "look":
        query = query.filter(db.or_(ScanDocument.validation["status"].as_string() != "verified",
                                    ScanDocument.validation.is_(None)))
    elif f["check"] == "dup":
        query = query.filter(ScanDocument.duplicate_of.isnot(None))
    if f["kind"]:
        query = query.filter(ScanDocument.category == f["kind"])
    today = date.today()
    start = {"today": today, "7": today - timedelta(days=6), "30": today - timedelta(days=29)}.get(f["uploaded"])
    end = None
    if f["uploaded"] == "range":
        start, end = f["from"], f["to"]
    if start:
        query = query.filter(ScanDocument.created_at >= _ist_start(start))
    if end:
        query = query.filter(ScanDocument.created_at < _ist_start(end + timedelta(days=1)))
    return query


def _supplier_options(in_tab) -> list[tuple[str, str]]:
    """(value, label) of every supplier among the tab's bills."""
    ex = ScanDocument.extracted
    rows = in_tab.with_entities(ex["supplier_name"].as_string(), ex["supplier_gstin"].as_string()).distinct().all()
    seen, out = set(), []
    for name, gstin in rows:
        value = (gstin or name or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append((value, f"{(name or '').strip() or gstin}{' · ' + gstin if gstin and name else ''}"))
    return sorted(out, key=lambda o: o[1].lower())


def _kind_options(in_tab) -> list[tuple[str, str]]:
    cats = sorted({c for (c,) in in_tab.with_entities(ScanDocument.category).distinct() if c})
    return [(c, CATEGORY_LABELS.get(c, c)) for c in cats]


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


@scan_bp.route("/<int:doc_id>/page/<int:n>.jpg")
@roles_required(ADMIN)
@feature_required("doc_scan")
def page(doc_id, n):
    """One page, upright, as the reader saw it - what the review screen
    zooms into when a value is pointed at."""
    doc = _doc(doc_id)
    try:
        data = services.page_image(doc, n)
    except ScanError:
        abort(404)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# --- checking and posting ----------------------------------------------------------------------

def _review_context(doc, form=None):
    from app.expenses.services import ensure_categories
    from app.models.expense import PAYMENT_MODE_LABELS
    from app.models.inventory import StockLocation
    from app.models.purchase import Supplier
    from app.models.tenant import Gstin

    tenant = _tenant()
    ex = doc.extracted or {}
    supplier, supplier_options = services.supplier_choices(tenant, doc)
    customer, customer_options = services.customer_choices(tenant, doc)
    original_invoice, invoice_options = services.invoice_choices(tenant, doc, customer)
    original_bill, bill_options = services.bill_choices(tenant, doc, supplier)
    categories = ensure_categories(tenant.id) if tenant.has_feature("expenses") else []
    # The standard categories are made the first time they're needed: keep
    # them, or the one picked here wouldn't exist when the bill is posted.
    db.session.commit()
    queue = [d.id for d in tenant_query(ScanDocument).filter(ScanDocument.status == TO_CHECK)
             .order_by(ScanDocument.id).with_entities(ScanDocument.id)]
    position = queue.index(doc.id) if doc.id in queue else None
    gstins = tenant.gstins.filter(Gstin.is_active.is_(True)).order_by(Gstin.is_primary.desc(), Gstin.id).all()
    return {
        "doc": doc, "ex": ex, "v": doc.validation or {}, "form": form,
        "kind": (form or {}).get("kind") or services.suggested_kind(tenant, doc),
        "lines": services.item_choices(tenant, _form_lines(form) or services.line_rows(doc)),
        "main_rate": services.main_gst_rate(doc),
        "supplier": supplier, "supplier_options": supplier_options,
        "customer": customer, "customer_options": customer_options,
        "original_invoice": original_invoice, "invoice_options": invoice_options,
        "original_bill": original_bill, "bill_options": bill_options,
        "kinds": services.kinds_on(tenant), "kind_labels": services.KIND_LABELS,
        "note_type": (form or {}).get("note_type") or services.note_type(doc),
        "original_number": services.original_number(doc),
        "gstins": gstins, "tenant": tenant,
        "queue": queue, "position": position,
        "category": services.guess_category(doc, categories),
        "locations": StockLocation.query.filter_by(tenant_id=tenant.id, is_active=True)
        .order_by(StockLocation.is_default.desc(), StockLocation.name).all(),
        "categories": categories,
        "payment_modes": PAYMENT_MODE_LABELS, "labels": CATEGORY_LABELS,
        "purchases_on": tenant.has_feature("purchases"), "expenses_on": tenant.has_feature("expenses"),
        "gstin_ok": services.gstin_ok, "posted_url": _posted_url(doc), "party": services.party,
        "detail_fields": services.DETAIL_FIELDS, "details": _form_details(form) or services.detail_values(doc),
        "tds": services.tds_read(doc) if form is None else (form.get("tds_amount", ""), form.get("tds_section", "")),
        "tds_sections": TDS_SECTIONS,
    }


def _form_details(form):
    if not form or not hasattr(form, "getlist"):
        return None
    keys = [k for k, _ in services.DETAIL_FIELDS] + ["discount"]
    return {k: form.get(f"detail_{k}", "") for k in keys}


def _form_lines(form):
    """The lines as the user left them, when a post was refused."""
    if not form or not hasattr(form, "getlist"):
        return None
    keys = {"description": "line_description[]", "hsn": "line_hsn[]", "qty": "line_qty[]", "unit": "line_unit[]",
            "rate": "line_rate[]", "discount": "line_discount[]", "gst_rate": "line_gst_rate[]",
            "product_id": "line_product_id[]", "serials": "line_serials[]", "item_code": "line_item_code[]",
            "batch": "line_batch[]", "mfg": "line_mfg[]", "expiry": "line_expiry[]", "mrp": "line_mrp[]"}
    columns = {k: form.getlist(v) for k, v in keys.items()}
    flats = form.getlist("line_discount_flat[]")
    count = len(columns["description"])
    rows = [{k: (columns[k][i] if i < len(columns[k]) else "") for k in keys} for i in range(count)]
    for i, row in enumerate(rows):
        flat = (flats[i] if i < len(flats) else "").strip()
        row["disc_type"] = "amt" if flat and flat not in ("0", "0.00") else "pct"
        if row["disc_type"] == "amt":
            row["discount"] = flat
    return rows or None


def _posted_url(doc):
    endpoints = {"purchase_bill": ("purchases.view_bill", "bill_id"),
                 "expense": ("expenses.view_expense", "expense_id"),
                 "invoice": ("invoicing.view_invoice", "invoice_id"),
                 "credit_note": ("notes.view_note", "note_id"),
                 "supplier_note": ("purchases.view_supplier_note", "note_id")}
    if doc.posted_type in endpoints and doc.posted_id:
        endpoint, arg = endpoints[doc.posted_type]
        return url_for(endpoint, **{arg: doc.posted_id})
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
        services.post(_tenant(), current_user, doc, request.form)
        db.session.commit()
    except ScanError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        doc = _doc(doc_id)
        return render_template("scan/review.html", **_review_context(doc, request.form)), 422
    flash({KIND_PURCHASE: "Posted as a purchase bill.", KIND_EXPENSE: "Posted as an expense.",
           services.KIND_SALE: "Entered as a sales invoice.", services.KIND_SALES_NOTE: "Entered as a note to the customer.",
           services.KIND_SUPPLIER_NOTE: "Entered as the supplier's note.", KIND_FILE: "Kept on file."}.get(kind, "Done."),
          "success")
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


@scan_bp.route("/<int:doc_id>/turn", methods=["POST"])
@roles_required(ADMIN)
@feature_required("doc_scan")
def turn(doc_id):
    doc = _doc(doc_id)
    try:
        services.turn(current_user, doc, request.form.get("deg", type=int) or 90)
        db.session.commit()
    except ScanError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("scan.review", doc_id=doc.id))


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
