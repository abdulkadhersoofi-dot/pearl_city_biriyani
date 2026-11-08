from datetime import date
from io import BytesIO

from flask import abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from weasyprint import HTML

from app.auth.decorators import roles_required
from app.extensions import db
from app.invoicing.forms import InvoiceHeaderForm
from app.invoicing.routes import header_form_choices, posted_lines, resolve_customer_id
from app.invoicing.services import InvoiceValidationError
from app.models.expense import FREQUENCY_LABELS, Frequency
from app.models.invoice_series import DOCUMENT_TYPE_LABELS, DocumentType
from app.models.product import Product
from app.models.sales import (
    CHALLAN_PURPOSE_LABELS,
    SALES_DOCUMENT_LABELS,
    ChallanPurpose,
    RecurringInvoice,
    RecurringInvoiceRun,
    RecurringRunStatus,
    SalesDocument,
    SalesDocumentKind,
    SalesDocumentStatus,
)
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.sales import sales_bp, services
from app.sales.services import SalesError
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.tenant_scope import assert_owns, tenant_query
from app.utils.entitlements import brand_logo_id
from app.utils.uploads import image_data_uri
from app.models.warehouse import CHOICE_REASONS
from app.warehouse import dispatch, notify

ADMIN = UserRole.CLIENT_ADMIN
STAFF = UserRole.STAFF
KINDS = {k.value: k for k in SalesDocumentKind}


def _kind(value) -> SalesDocumentKind:
    kind = KINDS.get(value or "quotation")
    if kind is None:
        abort(404)
    return kind


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _audit(action, entity_type, obj, **details):
    record_audit(current_user, action, tenant_id=current_user.tenant_id, entity_type=entity_type, entity_id=obj.id,
                 details=details)


def _products():
    return (tenant_query(Product).filter_by(is_active=True).filter(Product.has_variants.is_(False))
            .order_by(Product.name).all())


def _branch_orders_on() -> bool:
    """Branch logins make sales orders when warehouse dispatch is on - the
    salesperson picks the warehouse, the warehouse picks the units."""
    return dispatch.is_on(current_user.tenant)


def _allow(kind: SalesDocumentKind) -> None:
    if current_user.is_staff and not (kind == SalesDocumentKind.SALES_ORDER and _branch_orders_on()):
        abort(403)


def _doc(doc_id) -> SalesDocument:
    doc = tenant_query(SalesDocument).filter_by(id=doc_id).first_or_404()
    assert_owns(doc)
    if current_user.is_staff:
        _allow(doc.kind)
        if doc.created_by_id != current_user.id:
            abort(404)
    return doc


def _finance_form() -> dict:
    """An EMI sale's fields from the order form (checked in the service)."""
    from decimal import Decimal, InvalidOperation

    if not request.form.get("is_finance_sale"):
        return {}

    def money(key):
        raw = (request.form.get(key) or "").replace(",", "").strip()
        try:
            return Decimal(raw) if raw else None
        except InvalidOperation:
            raise SalesError("The finance amount and down payment must be numbers.")

    return {"is_finance_sale": True, "financier_id": request.form.get("financier_id", type=int),
            "loan_ref": request.form.get("loan_ref"), "finance_amount": money("finance_amount"),
            "down_payment": money("down_payment")}


def _ship_form() -> dict:
    """The delivery address and the split choice from the order form."""
    if not dispatch.is_on(current_user.tenant):
        return {}
    ship = {k: request.form.get(f"ship_{k}", "") for k in services.SHIP_TO_KEYS}
    return {"ship_to": ship if request.form.get("ship_elsewhere") else None, "split": bool(request.form.get("split"))}


def _customer_addresses() -> dict:
    """For the form: each customer's address, to fill "deliver to" and ask
    for the nearest warehouse when the customer is picked."""
    from app.models.customer import Customer

    return {str(c.id): {"name": c.name, "address_line1": c.address_line1 or "", "address_line2": c.address_line2 or "",
                        "city": c.city or "", "state_code": c.state_code or "", "pincode": c.pincode or "",
                        "phone": c.phone or ""}
            for c in tenant_query(Customer).filter_by(is_active=True).all()}


def _warehouse_context(doc=None):
    """The Warehouse picker: the warehouses, with free quantities for the
    items on the form (filled in by /warehouse/free.json as lines change)."""
    if not dispatch.is_on(current_user.tenant):
        return {}
    chosen = request.form.get("dispatch_location_id") if request.method == "POST" else None
    if not chosen:
        default = doc.dispatch_location if doc is not None else dispatch.default_warehouse(current_user.tenant_id,
                                                                                            current_user)
        chosen = str(default.id) if default is not None else ""
    from app.warehouse.emi import active_financiers

    return {"warehouses": dispatch.warehouses(current_user.tenant_id), "chosen_warehouse": chosen,
            "financiers": active_financiers(current_user.tenant_id),
            "customer_addresses": _customer_addresses() if doc is None else {}}


def _convertible_choices(form):
    allowed = {DocumentType(v) for v, _ in form.document_type.choices}
    form.document_type.choices = [(dt.value, DOCUMENT_TYPE_LABELS[dt]) for dt in services.CONVERTIBLE_TYPES
                                  if dt in allowed]


# --- quotations, proforma invoices, sales orders, delivery challans ---------------------

@sales_bp.route("/documents")
@roles_required(ADMIN, STAFF)
@feature_required("sales_documents")
def documents():
    kind = _kind(request.args.get("kind") or ("sales_order" if current_user.is_staff else None))
    _allow(kind)
    status = request.args.get("status", "open")
    query = tenant_query(SalesDocument).filter_by(kind=kind)
    if current_user.is_staff:
        query = query.filter_by(created_by_id=current_user.id)
    if status in {s.value for s in SalesDocumentStatus}:
        query = query.filter_by(status=SalesDocumentStatus(status))
    rows = query.order_by(SalesDocument.doc_date.desc(), SalesDocument.id.desc()).limit(300).all()
    return render_template("sales/documents.html", rows=rows, kind=kind, status=status, labels=SALES_DOCUMENT_LABELS,
                           kinds=list(SalesDocumentKind), purposes=CHALLAN_PURPOSE_LABELS, today=date.today())


@sales_bp.route("/documents/new", methods=["GET", "POST"])
@roles_required(ADMIN, STAFF)
@feature_required("sales_documents")
def new_document():
    tenant = current_user.tenant
    kind = _kind(request.values.get("kind"))
    _allow(kind)
    form = InvoiceHeaderForm()
    customers, branches = header_form_choices(form, tenant)
    _convertible_choices(form)
    form.invoice_date.label.text = "Date"
    form.document_type.label.text = "Invoiced as" if kind == SalesDocumentKind.DELIVERY_CHALLAN else "Will be invoiced as"
    prefill = None
    if request.method == "GET" and request.args.get("duplicate_from"):
        source = tenant_query(SalesDocument).filter_by(id=request.args["duplicate_from"]).first()
        if source:
            form.gstin_id.data = source.gstin_id
            form.document_type.data = source.invoice_document_type.value
            form.customer_id.data = str(source.customer_id)
            form.place_of_supply_state_code.data = source.place_of_supply_state_code
            form.notes.data = source.notes
            prefill = source.lines
    if form.validate_on_submit():
        try:
            purpose = request.form.get("challan_purpose") or None
            doc = services.create_sales_document(tenant, current_user, services.SalesDocInput(
                kind=kind, gstin_id=form.gstin_id.data, customer_id=resolve_customer_id(form, tenant),
                place_of_supply_state_code=form.place_of_supply_state_code.data, doc_date=form.invoice_date.data,
                invoice_document_type=DocumentType(form.document_type.data), lines=posted_lines(),
                valid_until=_date(request.form.get("valid_until")),
                challan_purpose=ChallanPurpose(purpose) if purpose in {p.value for p in ChallanPurpose} else None,
                notes=form.notes.data, terms=request.form.get("terms"),
                dispatch_location_id=request.form.get("dispatch_location_id") or None,
                **_finance_form(), **_ship_form(),
            ))
            _audit("sales_document_created", "sales_document", doc, number=doc.number, kind=kind.value,
                   total=str(doc.grand_total))
            db.session.commit()
            notify.send_pending_quietly()
            parts = getattr(doc, "split_parts", None)
            if parts and len(parts) > 1:
                flash(f"{doc.label} split across warehouses: " + ", ".join(
                    f"{p.number} to {p.dispatch_location.name}" for p in parts) + ".", "success")
            elif doc.dispatch_location is not None:
                flash(f"{doc.label} {doc.number} saved and sent to {doc.dispatch_location.name}.", "success")
            else:
                flash(f"{doc.label} {doc.number} saved.", "success")
            return redirect(url_for("sales.view_document", doc_id=doc.id))
        except (SalesError, InvoiceValidationError) as exc:
            db.session.rollback()
            flash(exc.message, "error")
    return render_template("sales/doc_form.html", form=form, kind=kind, label=SALES_DOCUMENT_LABELS[kind],
                           products=_products(), customers=customers, branches=[], duplicate_from=None,
                           prefill_lines=prefill, purposes=CHALLAN_PURPOSE_LABELS, source_doc=None, source_run=None,
                           **(_warehouse_context() if kind == SalesDocumentKind.SALES_ORDER else {}))


@sales_bp.route("/documents/<int:doc_id>")
@roles_required(ADMIN, STAFF)
@feature_required("sales_documents")
def view_document(doc_id):
    doc = _doc(doc_id)
    extra = {}
    if doc.dispatch_status is not None:
        from app.models.warehouse import DISPATCH_STATUS_LABELS

        show_all = request.args.get("all") == "1"
        choices = {}
        if doc.status == SalesDocumentStatus.OPEN and doc.dispatch_status.value == "to_pick" and not doc.claimed_by_id:
            choices = {l.id: dispatch.showroom_choices(doc, l, show_all) for l in doc.lines if dispatch.needs_scan(l)}
        extra = {"progress": dispatch.line_progress(doc), "dispatch_labels": DISPATCH_STATUS_LABELS,
                 "complete": dispatch.is_complete(doc), "choices": choices, "show_all": show_all,
                 "pending_swaps": dispatch.pending_swaps(doc), "may_emi_invoice": _may_emi_invoice(doc),
                 "choice_reasons": CHOICE_REASONS,
                 "split_parts": (tenant_query(SalesDocument).filter_by(split_group_id=doc.split_group_id)
                                 .order_by(SalesDocument.id).all() if doc.split_group_id else []),
                 **_warehouse_context(doc)}
    return render_template("sales/document_view.html", doc=doc, purposes=CHALLAN_PURPOSE_LABELS,
                           doc_type_labels=DOCUMENT_TYPE_LABELS, today=date.today(), **extra)


def _may_emi_invoice(doc) -> bool:
    return (doc.is_finance_sale and doc.status == SalesDocumentStatus.OPEN and doc.dispatch_status is not None
            and doc.dispatch_status.value == "picked")


@sales_bp.route("/documents/<int:doc_id>/dispatch.json")
@roles_required(ADMIN, STAFF)
@feature_required("sales_documents")
def dispatch_state(doc_id):
    """What the order page polls: a change reloads it, so the showroom sees
    "Picking: Ravi (1/2)" and an EMI sale's locked serials as they happen."""
    from flask import jsonify

    doc = _doc(doc_id)
    progress = dispatch.line_progress(doc) if doc.dispatch_status else {}
    picked = sum(p["picked"] for p in progress.values())
    stamp = f"{doc.status.value}|{doc.dispatch_status.value if doc.dispatch_status else ''}|{doc.claimed_by_id}|" \
            f"{picked}|{doc.converted_invoice_id}|{len(dispatch.pending_swaps(doc)) if doc.dispatch_status else 0}"
    return jsonify({"stamp": stamp})


@sales_bp.route("/documents/<int:doc_id>/showroom-pick", methods=["POST"])
@roles_required(ADMIN, STAFF)
@feature_required("sales_documents")
def showroom_pick(doc_id):
    """The showroom chooses the serials itself (warehouse closed, no signal, a rush)."""
    doc = _doc(doc_id)
    chosen = {l.id: request.form.getlist(f"serial_{l.id}") for l in doc.lines if dispatch.needs_scan(l)}
    try:
        dispatch.showroom_pick(doc, chosen, current_user)
    except dispatch.DispatchError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("sales.view_document", doc_id=doc.id))
    _audit("sales_order_serials_chosen_at_showroom", "sales_document", doc, number=doc.number,
           serials={str(k): v for k, v in chosen.items()})
    notify.commit_and_push()
    flash("Serials held for this order - the warehouse fetches exactly these (it is shown their racks).", "success")
    return redirect(url_for("sales.view_document", doc_id=doc.id))


@sales_bp.route("/documents/<int:doc_id>/emi-invoice", methods=["POST"])
@roles_required(ADMIN, STAFF)
@feature_required("sales_documents")
def emi_invoice(doc_id):
    """An EMI sale, every unit locked at the warehouse: the showroom makes the
    invoice with those serials (read-only), from the warehouse's stock."""
    doc = _doc(doc_id)
    if not _may_emi_invoice(doc):
        flash("The warehouse hasn't locked every unit for this EMI sale yet.", "error")
        return redirect(url_for("sales.view_document", doc_id=doc.id))
    try:
        invoice = services.convert_to_invoice(current_user.tenant, current_user, doc,
                                              _date(request.form.get("invoice_date")) or date.today())
    except SalesError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("sales.view_document", doc_id=doc.id))
    record_audit(current_user, "invoice_created", tenant_id=doc.tenant_id, entity_type="invoice", entity_id=invoice.id,
                 details={"invoice_number": invoice.invoice_number, "from_document": doc.number, "emi": True,
                          "financier": doc.financier.name if doc.financier else None})
    _audit("sales_document_converted", "sales_document", doc, number=doc.number, invoice=invoice.invoice_number)
    notify.commit_and_push()
    flash(f"Invoice {invoice.invoice_number} made with the locked serials - print it for the financier.", "success")
    return redirect(url_for("sales.view_document", doc_id=doc.id))


@sales_bp.route("/documents/<int:doc_id>/invoice.pdf")
@roles_required(ADMIN, STAFF)
@feature_required("sales_documents")
def document_invoice_pdf(doc_id):
    """The order's invoice - for a branch login, which has no invoice pages."""
    from app.invoicing.pdf import render_invoice_pdf
    from app.models.invoice import Invoice

    doc = _doc(doc_id)
    invoice = db.session.get(Invoice, doc.converted_invoice_id) if doc.converted_invoice_id else None
    if invoice is None or invoice.tenant_id != doc.tenant_id:
        abort(404)
    return send_file(BytesIO(render_invoice_pdf(invoice)), mimetype="application/pdf", as_attachment=False,
                     download_name=f"{invoice.invoice_number.replace('/', '-')}.pdf")


@sales_bp.route("/documents/<int:doc_id>/warehouse", methods=["POST"])
@roles_required(ADMIN, STAFF)
@feature_required("sales_documents")
def change_warehouse(doc_id):
    """Ship an open, unclaimed order from another warehouse."""
    doc = _doc(doc_id)
    try:
        location = dispatch.resolve_warehouse(current_user.tenant, request.form.get("dispatch_location_id"))
        old = dispatch.change_warehouse(current_user.tenant, doc, location)
        if old is not None:
            _audit("sales_order_warehouse_changed", "sales_document", doc, number=doc.number, before=old.name,
                   after=location.name)
        notify.commit_and_push()
        flash(f"{doc.number} now ships from {location.name}.", "success")
    except dispatch.DispatchError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("sales.view_document", doc_id=doc.id))


@sales_bp.route("/documents/<int:doc_id>/pdf")
@roles_required(ADMIN, STAFF)
@feature_required("sales_documents")
def document_pdf(doc_id):
    doc = _doc(doc_id)
    tenant = db.session.get(Tenant, doc.tenant_id)
    html = render_template("sales/document_pdf.html", doc=doc, firm_name=tenant.display_name,
                           logo_data_uri=image_data_uri(brand_logo_id(tenant)), purposes=CHALLAN_PURPOSE_LABELS,
                           doc_type_labels=DOCUMENT_TYPE_LABELS)
    pdf = HTML(string=html, base_url=current_app.root_path).write_pdf()
    return send_file(BytesIO(pdf), mimetype="application/pdf", as_attachment=False,
                     download_name=f"{doc.number.replace('/', '-')}.pdf")


@sales_bp.route("/documents/<int:doc_id>/convert", methods=["POST"])
@roles_required(ADMIN)
@feature_required("sales_documents")
def convert_document(doc_id):
    doc = _doc(doc_id)
    try:
        invoice = services.convert_to_invoice(current_user.tenant, current_user, doc,
                                              _date(request.form.get("invoice_date")) or date.today())
        record_audit(current_user, "invoice_created", tenant_id=doc.tenant_id, entity_type="invoice",
                     entity_id=invoice.id, details={"invoice_number": invoice.invoice_number, "from_document": doc.number})
        _audit("sales_document_converted", "sales_document", doc, number=doc.number, invoice=invoice.invoice_number)
        db.session.commit()
        flash(f"Invoice {invoice.invoice_number} issued from {doc.number}.", "success")
        from app.accounts.receivables import credit_warning

        warning = credit_warning(current_user.tenant, invoice.customer)
        if warning:
            flash(warning, "warning")
        return redirect(url_for("invoicing.view_invoice", invoice_id=invoice.id))
    except SalesError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("sales.view_document", doc_id=doc.id))


@sales_bp.route("/documents/<int:doc_id>/cancel", methods=["POST"])
@roles_required(ADMIN, STAFF)
@feature_required("sales_documents")
def cancel_document(doc_id):
    doc = _doc(doc_id)
    try:
        services.cancel_document(doc, current_user, request.form.get("reason", ""))
        _audit("sales_document_cancelled", "sales_document", doc, number=doc.number, reason=doc.cancel_reason)
        notify.commit_and_push()
        flash(f"{doc.number} cancelled.", "success")
    except SalesError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("sales.view_document", doc_id=doc.id))


@sales_bp.route("/documents/<int:doc_id>/returned", methods=["POST"])
@roles_required(ADMIN)
@feature_required("sales_documents")
def document_returned(doc_id):
    doc = _doc(doc_id)
    try:
        services.mark_returned(doc, current_user, request.form.get("note", ""))
        _audit("delivery_challan_returned", "sales_document", doc, number=doc.number, note=doc.cancel_reason)
        db.session.commit()
        flash(f"{doc.number} marked as returned.", "success")
    except SalesError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("sales.view_document", doc_id=doc.id))


# --- recurring invoices ----------------------------------------------------------------

def _recurring(recurring_id) -> RecurringInvoice:
    rec = tenant_query(RecurringInvoice).filter_by(id=recurring_id).first_or_404()
    assert_owns(rec)
    return rec


def _run(run_id) -> RecurringInvoiceRun:
    run = tenant_query(RecurringInvoiceRun).filter_by(id=run_id).first_or_404()
    assert_owns(run)
    return run


@sales_bp.route("/recurring")
@roles_required(ADMIN)
@feature_required("recurring_invoices")
def recurring():
    tenant = current_user.tenant
    made = services.generate_due_runs(tenant)
    db.session.commit()
    pending = (tenant_query(RecurringInvoiceRun).filter_by(status=RecurringRunStatus.PENDING)
               .order_by(RecurringInvoiceRun.due_date).all())
    rows = tenant_query(RecurringInvoice).order_by(RecurringInvoice.is_active.desc(), RecurringInvoice.name).all()
    recent = (tenant_query(RecurringInvoiceRun).filter(RecurringInvoiceRun.status != RecurringRunStatus.PENDING)
              .order_by(RecurringInvoiceRun.closed_at.desc()).limit(20).all())
    return render_template("sales/recurring.html", rows=rows, pending=pending, recent=recent, new_runs=len(made),
                           frequencies=FREQUENCY_LABELS, today=date.today())


def _recurring_form(rec=None):
    tenant = current_user.tenant
    form = InvoiceHeaderForm()
    customers, _ = header_form_choices(form, tenant)
    _convertible_choices(form)
    form.invoice_date.label.text = "First invoice on"
    if request.method == "GET" and rec is not None:
        form.gstin_id.data = rec.gstin_id
        form.document_type.data = rec.document_type.value
        form.customer_id.data = str(rec.customer_id)
        form.place_of_supply_state_code.data = rec.place_of_supply_state_code
        form.invoice_date.data = rec.start_date
        form.notes.data = rec.notes
    if form.validate_on_submit():
        try:
            frequency = request.form.get("frequency", "monthly")
            data = services.RecurringInvoiceInput(
                name=request.form.get("name", ""), gstin_id=form.gstin_id.data,
                customer_id=resolve_customer_id(form, tenant), document_type=DocumentType(form.document_type.data),
                place_of_supply_state_code=form.place_of_supply_state_code.data,
                frequency=Frequency(frequency) if frequency in {f.value for f in Frequency} else Frequency.MONTHLY,
                start_date=form.invoice_date.data, end_date=_date(request.form.get("end_date")),
                lines=posted_lines(), notes=form.notes.data,
            )
            saved = services.save_recurring(tenant, current_user, data, rec)
            _audit("recurring_invoice_saved", "recurring_invoice", saved, name=saved.name,
                   frequency=saved.frequency.value)
            db.session.commit()
            flash(f"Recurring invoice \"{saved.name}\" saved.", "success")
            return redirect(url_for("sales.recurring"))
        except (SalesError, InvoiceValidationError) as exc:
            db.session.rollback()
            flash(exc.message, "error")
    return render_template("sales/recurring_form.html", form=form, rec=rec, products=_products(), customers=customers,
                           branches=[], duplicate_from=None, prefill_lines=rec.lines if rec else None,
                           frequencies=FREQUENCY_LABELS, source_doc=None, source_run=None)


@sales_bp.route("/recurring/new", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("recurring_invoices")
def new_recurring():
    return _recurring_form()


@sales_bp.route("/recurring/<int:recurring_id>/edit", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("recurring_invoices")
def edit_recurring(recurring_id):
    return _recurring_form(_recurring(recurring_id))


@sales_bp.route("/recurring/<int:recurring_id>/toggle", methods=["POST"])
@roles_required(ADMIN)
@feature_required("recurring_invoices")
def toggle_recurring(recurring_id):
    rec = _recurring(recurring_id)
    rec.is_active = not rec.is_active
    _audit("recurring_invoice_" + ("resumed" if rec.is_active else "paused"), "recurring_invoice", rec, name=rec.name)
    db.session.commit()
    flash(f"\"{rec.name}\" {'resumed' if rec.is_active else 'paused'}.", "success")
    return redirect(url_for("sales.recurring"))


@sales_bp.route("/recurring/runs/<int:run_id>/issue", methods=["POST"])
@roles_required(ADMIN)
@feature_required("recurring_invoices")
def issue_run(run_id):
    run = _run(run_id)
    try:
        invoice = services.issue_run(current_user.tenant, current_user, run, _date(request.form.get("invoice_date")))
        record_audit(current_user, "invoice_created", tenant_id=run.tenant_id, entity_type="invoice",
                     entity_id=invoice.id, details={"invoice_number": invoice.invoice_number, "from_recurring_run": run.id})
        db.session.commit()
        flash(f"Invoice {invoice.invoice_number} issued for \"{run.recurring.name}\".", "success")
    except SalesError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("sales.recurring"))


@sales_bp.route("/recurring/runs/<int:run_id>/skip", methods=["POST"])
@roles_required(ADMIN)
@feature_required("recurring_invoices")
def skip_run(run_id):
    run = _run(run_id)
    try:
        services.skip_run(run, current_user, request.form.get("reason", ""))
        _audit("recurring_invoice_skipped", "recurring_invoice_run", run, name=run.recurring.name,
               due=run.due_date.isoformat(), reason=run.skip_reason)
        db.session.commit()
        flash("Skipped.", "success")
    except SalesError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("sales.recurring"))
