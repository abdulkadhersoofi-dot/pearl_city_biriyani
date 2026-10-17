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

ADMIN = UserRole.CLIENT_ADMIN
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


def _doc(doc_id) -> SalesDocument:
    doc = tenant_query(SalesDocument).filter_by(id=doc_id).first_or_404()
    assert_owns(doc)
    return doc


def _convertible_choices(form):
    allowed = {DocumentType(v) for v, _ in form.document_type.choices}
    form.document_type.choices = [(dt.value, DOCUMENT_TYPE_LABELS[dt]) for dt in services.CONVERTIBLE_TYPES
                                  if dt in allowed]


# --- quotations, proforma invoices, sales orders, delivery challans ---------------------

@sales_bp.route("/documents")
@roles_required(ADMIN)
@feature_required("sales_documents")
def documents():
    kind = _kind(request.args.get("kind"))
    status = request.args.get("status", "open")
    query = tenant_query(SalesDocument).filter_by(kind=kind)
    if status in {s.value for s in SalesDocumentStatus}:
        query = query.filter_by(status=SalesDocumentStatus(status))
    rows = query.order_by(SalesDocument.doc_date.desc(), SalesDocument.id.desc()).limit(300).all()
    return render_template("sales/documents.html", rows=rows, kind=kind, status=status, labels=SALES_DOCUMENT_LABELS,
                           kinds=list(SalesDocumentKind), purposes=CHALLAN_PURPOSE_LABELS, today=date.today())


@sales_bp.route("/documents/new", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("sales_documents")
def new_document():
    tenant = current_user.tenant
    kind = _kind(request.values.get("kind"))
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
            ))
            _audit("sales_document_created", "sales_document", doc, number=doc.number, kind=kind.value,
                   total=str(doc.grand_total))
            db.session.commit()
            flash(f"{doc.label} {doc.number} saved.", "success")
            return redirect(url_for("sales.view_document", doc_id=doc.id))
        except (SalesError, InvoiceValidationError) as exc:
            db.session.rollback()
            flash(exc.message, "error")
    return render_template("sales/doc_form.html", form=form, kind=kind, label=SALES_DOCUMENT_LABELS[kind],
                           products=_products(), customers=customers, branches=[], duplicate_from=None,
                           prefill_lines=prefill, purposes=CHALLAN_PURPOSE_LABELS, source_doc=None, source_run=None)


@sales_bp.route("/documents/<int:doc_id>")
@roles_required(ADMIN)
@feature_required("sales_documents")
def view_document(doc_id):
    doc = _doc(doc_id)
    return render_template("sales/document_view.html", doc=doc, purposes=CHALLAN_PURPOSE_LABELS,
                           doc_type_labels=DOCUMENT_TYPE_LABELS, today=date.today())


@sales_bp.route("/documents/<int:doc_id>/pdf")
@roles_required(ADMIN)
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
@roles_required(ADMIN)
@feature_required("sales_documents")
def cancel_document(doc_id):
    doc = _doc(doc_id)
    try:
        services.cancel_document(doc, current_user, request.form.get("reason", ""))
        _audit("sales_document_cancelled", "sales_document", doc, number=doc.number, reason=doc.cancel_reason)
        db.session.commit()
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
