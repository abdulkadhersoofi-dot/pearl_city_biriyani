from datetime import date

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from io import BytesIO

from app.auth.decorators import roles_required
from app.extensions import db
from app.invoicing import invoicing_bp
from app.invoicing.forms import InvoiceHeaderForm
from app.invoicing.pdf import render_invoice_pdf
from app.invoicing.services import (
    InvoiceInput,
    InvoiceValidationError,
    create_invoice,
    parse_line_arrays,
    void_invoice,
)
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_series import DOCUMENT_TYPE_LABELS, DocumentType
from app.models.product import Product
from app.models.tenant import Gstin
from app.models.user import BranchType, User, UserRole
from app.utils import numbering
from app.utils.audit import record_audit
from app.utils.financial_year import financial_year_for
from app.utils.numbering import NumberingError, check_typed_number
from app.utils.gst import allowed_document_types
from app.utils.indian_states import STATE_NAME_BY_CODE
from app.utils.tenant_scope import assert_owns, tenant_query

# Order matters: this is also the dropdown's order, so Tax Invoice - the
# common case - lands first/selected-by-default rather than whatever
# order a set() happened to iterate in.
INVOICING_DOCUMENT_TYPES = [
    DocumentType.TAX_INVOICE,
    DocumentType.BILL_OF_SUPPLY,
    DocumentType.EXPORT_INVOICE_IGST,
    DocumentType.EXPORT_INVOICE_LUT,
    DocumentType.RCM_INVOICE,
]


def _tenant_document_type_choices():
    allowed = allowed_document_types(current_user.tenant.registration_type)
    return [(dt.value, DOCUMENT_TYPE_LABELS[dt]) for dt in INVOICING_DOCUMENT_TYPES if dt in allowed]


def resolve_customer_id(form, tenant) -> int:
    """The picked customer, or a new one made from the form's fields."""
    customer_id = form.customer_id.data
    if not customer_id:
        raise InvoiceValidationError("Select a customer or a branch to bill.")
    if customer_id != "new":
        return int(customer_id)
    if not form.new_customer_name.data:
        raise InvoiceValidationError("Enter the new customer's name.")
    customer = Customer(
        tenant_id=tenant.id,
        name=form.new_customer_name.data.strip(),
        gstin=form.new_customer_gstin.data,  # already normalized/validated by the form field
        address_line1=form.new_customer_address.data,
        state_code=form.new_customer_state_code.data or form.place_of_supply_state_code.data,
    )
    customer.state_name = STATE_NAME_BY_CODE.get(customer.state_code, "")
    db.session.add(customer)
    db.session.flush()
    return customer.id


def posted_lines():
    return parse_line_arrays(
        request.form.getlist("line_description[]"),
        request.form.getlist("line_hsn[]"),
        request.form.getlist("line_qty[]"),
        request.form.getlist("line_rate[]"),
        request.form.getlist("line_discount[]"),
        request.form.getlist("line_gst_rate[]"),
        request.form.getlist("line_product_id[]"),
        request.form.getlist("line_unit[]"),
        request.form.getlist("line_incl[]"),
    )


def header_form_choices(form, tenant):
    form.gstin_id.choices = [(g.id, g.display_label) for g in tenant.gstins.filter_by(is_active=True)]
    form.document_type.choices = _tenant_document_type_choices()
    if request.method == "POST" and request.form.get("branch_user_id"):
        # Billing a branch: the document type is the branch's
        # (services.branch_document_type), not the hidden picker's.
        form.document_type.validate_choice = False
    customers = tenant_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    form.customer_id.choices = [("new", "+ New customer")] + [(str(c.id), c.name) for c in customers]
    branches = tenant_query(User).filter_by(role=UserRole.STAFF, is_active=True).order_by(User.name).all()
    form.branch_user_id.choices = [("", "")] + [(str(b.id), b.name) for b in branches]
    return customers, branches


def _linked_source():
    """A quotation / order / challan or a recurring-invoice draft this
    invoice is being made from ("Edit & invoice") - only with those
    modules on, and only the client's own."""
    tenant = current_user.tenant
    source = request.values.get("from_document") if tenant.has_feature("sales_documents") else None
    run = request.values.get("from_run") if tenant.has_feature("recurring_invoices") else None
    from app.models.sales import RecurringInvoiceRun, SalesDocument

    doc = tenant_query(SalesDocument).filter_by(id=source).first() if source and source.isdigit() else None
    run = tenant_query(RecurringInvoiceRun).filter_by(id=run).first() if run and run.isdigit() else None
    return doc, run


@invoicing_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
def list_invoices():
    status = request.args.get("status", "")
    search = request.args.get("q", "").strip()
    query = tenant_query(Invoice)
    if status:
        query = query.filter(Invoice.status == InvoiceStatus(status))
    if search:
        query = query.filter(Invoice.invoice_number.ilike(f"%{search}%"))
    invoices = query.order_by(Invoice.invoice_date.desc(), Invoice.id.desc()).limit(200).all()
    return render_template(
        "invoicing/list.html",
        invoices=invoices,
        status=status,
        search=search,
        InvoiceStatus=InvoiceStatus,
        DOCUMENT_TYPE_LABELS=DOCUMENT_TYPE_LABELS,
    )


@invoicing_bp.route("/new", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def new_invoice():
    tenant = current_user.tenant
    form = InvoiceHeaderForm()
    customers, branches = header_form_choices(form, tenant)
    source_doc, source_run = _linked_source()
    prefill_lines = None
    if request.method == "GET" and (source_doc or source_run):
        if source_doc:
            form.gstin_id.data = source_doc.gstin_id
            form.document_type.data = source_doc.invoice_document_type.value
            form.customer_id.data = str(source_doc.customer_id)
            form.place_of_supply_state_code.data = source_doc.place_of_supply_state_code
            form.notes.data = source_doc.notes
            prefill_lines = source_doc.lines
        else:
            rec = source_run.recurring
            form.gstin_id.data = rec.gstin_id
            form.document_type.data = rec.document_type.value
            form.customer_id.data = str(rec.customer_id)
            form.place_of_supply_state_code.data = rec.place_of_supply_state_code
            form.notes.data = rec.notes
            form.invoice_date.data = source_run.due_date
            prefill_lines = rec.lines

    duplicate_from = None
    if request.method == "GET" and request.args.get("duplicate_from"):
        duplicate_from = tenant_query(Invoice).filter_by(id=request.args["duplicate_from"]).first()
        if duplicate_from:
            form.gstin_id.data = duplicate_from.gstin_id
            form.document_type.data = duplicate_from.document_type.value
            if duplicate_from.branch_user_id:
                form.branch_user_id.data = str(duplicate_from.branch_user_id)
            else:
                form.customer_id.data = str(duplicate_from.customer_id)
            form.place_of_supply_state_code.data = duplicate_from.place_of_supply_state_code
            form.notes.data = duplicate_from.notes

    if form.validate_on_submit():
        try:
            branch_id = form.branch_user_id.data or None
            customer_id = None if branch_id else resolve_customer_id(form, tenant)
            lines = posted_lines()
            number = typed_number()

            invoice = create_invoice(
                tenant,
                current_user,
                InvoiceInput(
                    gstin_id=form.gstin_id.data,
                    document_type=DocumentType(form.document_type.data),
                    customer_id=customer_id,
                    branch_user_id=int(branch_id) if branch_id else None,
                    place_of_supply_state_code=form.place_of_supply_state_code.data,
                    invoice_date=form.invoice_date.data,
                    notes=form.notes.data,
                    lines=lines,
                    number=number,
                ),
            )
            if tenant.has_feature("wholesale") and request.form.get("salesman_id"):
                from app.models.business import Salesman

                salesman = tenant_query(Salesman).filter_by(id=request.form.get("salesman_id", type=int) or 0,
                                                            is_active=True).first()
                if salesman is None:
                    raise InvoiceValidationError("Pick a valid salesman.")
                invoice.salesman_id = salesman.id
            if number:
                record_audit(current_user, "invoice_number_typed", tenant_id=tenant.id, entity_type="invoice",
                             entity_id=invoice.id, details={"invoice_number": number})
            record_audit(
                current_user,
                "invoice_created",
                tenant_id=tenant.id,
                entity_type="invoice",
                entity_id=invoice.id,
                details={"invoice_number": invoice.invoice_number, "branch_user_id": invoice.branch_user_id,
                         **({"from_document": source_doc.number} if source_doc else {}),
                         **({"from_recurring_run": source_run.id} if source_run else {})},
            )
            if source_doc or source_run:
                from app.sales import services as sales_services

                try:
                    if source_doc:
                        sales_services.mark_converted(source_doc, invoice, current_user)
                    else:
                        sales_services.mark_run_issued(source_run, invoice, current_user)
                except sales_services.SalesError as exc:
                    raise InvoiceValidationError(exc.message)
            db.session.commit()
            flash(f"Invoice {invoice.invoice_number} created.", "success")
            if invoice.customer_id:
                from app.accounts.receivables import credit_warning

                warning = credit_warning(tenant, invoice.customer)
                if warning:
                    flash(warning, "warning")
            return redirect(url_for("invoicing.view_invoice", invoice_id=invoice.id))
        except InvoiceValidationError as exc:
            db.session.rollback()
            flash(exc.message, "error")

    products = (tenant_query(Product).filter_by(is_active=True).filter(Product.has_variants.is_(False))
                .order_by(Product.name).all())
    return render_template(
        "invoicing/form.html",
        form=form,
        products=products,
        customers=customers,
        branches=branches,
        duplicate_from=duplicate_from,
        prefill_lines=prefill_lines,
        source_doc=source_doc,
        source_run=source_run,
        numbering=True,
        typed=request.form.get("invoice_number", "") if request.method == "POST" else "",
    )


def typed_number() -> str | None:
    """The number typed over the suggested one, or None to take the
    series' next (left as suggested, or emptied)."""
    typed = (request.form.get("invoice_number") or "").strip()
    if not typed or typed == (request.form.get("auto_number") or "").strip():
        return None
    try:
        return check_typed_number(typed)
    except NumberingError as exc:
        raise InvoiceValidationError(exc.message)


NUMBERED_TYPES = set(INVOICING_DOCUMENT_TYPES) | {DocumentType.DELIVERY_CHALLAN}


def _series_args():
    """The series a request names: (gstin, document type, financial year)."""
    tenant = current_user.tenant
    values = request.get_json(silent=True) or request.values
    gstin = tenant.gstins.filter_by(id=int(values.get("gstin_id") or 0), is_active=True).first() \
        if str(values.get("gstin_id") or "").isdigit() else None
    try:
        document_type = DocumentType(values.get("document_type") or "")
    except ValueError:
        document_type = None
    try:
        on = date.fromisoformat(values.get("date") or "") if values.get("date") else date.today()
    except ValueError:
        on = date.today()
    if gstin is None or document_type not in NUMBERED_TYPES:
        abort(404)
    return gstin, document_type, financial_year_for(on)


@invoicing_bp.route("/next-number")
@roles_required(UserRole.CLIENT_ADMIN)
def next_number():
    """The number the invoice being made will get (shown on the form; not
    taken until it is saved)."""
    gstin, document_type, fy = _series_args()
    info = numbering.peek(current_user.tenant_id, gstin.id, document_type, fy)
    return jsonify({**info, "label": DOCUMENT_TYPE_LABELS[document_type], "gstin": gstin.gstin or gstin.display_label})


@invoicing_bp.route("/numbering", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def save_numbering():
    """The owner sets a series' prefix and where it carries on from
    (Zoho's "auto-generate with this prefix and next number")."""
    tenant = current_user.tenant
    gstin, document_type, fy = _series_args()
    body = request.get_json(silent=True) or request.form
    series = numbering.get_or_create_series(tenant.id, gstin.id, document_type, fy)
    before = {"prefix": series.prefix, "next": series.next_number}
    prefix = str(body.get("prefix") or "").strip()
    if prefix != series.prefix and numbering.peek(tenant.id, gstin.id, document_type, fy)["prefix_locked"]:
        return jsonify({"error": "This series' prefix is fixed while e-invoicing is on (the IRP's 16 characters)."}), 400
    try:
        wanted = int(str(body.get("next") or "").strip())
    except ValueError:
        return jsonify({"error": "The next number is a whole number from 1."}), 400
    try:
        used = numbering.used_serials(Invoice, tenant.id, Invoice.invoice_number, prefix, fy)
        numbering.set_series(series, prefix, wanted, used)
    except numbering.NumberingError as exc:
        db.session.rollback()
        return jsonify({"error": exc.message}), 400
    record_audit(current_user, "invoice_numbering_changed", tenant_id=tenant.id, entity_type="invoice_series",
                 entity_id=series.id, details={"document_type": document_type.value, "financial_year": fy,
                                               "gstin_id": gstin.id, "before": before,
                                               "after": {"prefix": prefix, "next": wanted}})
    db.session.commit()
    return jsonify(numbering.peek(tenant.id, gstin.id, document_type, fy))


@invoicing_bp.route("/<int:invoice_id>")
@roles_required(UserRole.CLIENT_ADMIN)
def view_invoice(invoice_id):
    invoice = tenant_query(Invoice).filter_by(id=invoice_id).first_or_404()
    assert_owns(invoice)
    return render_template(
        "invoicing/view.html", invoice=invoice, document_label=DOCUMENT_TYPE_LABELS[invoice.document_type]
    )


@invoicing_bp.route("/<int:invoice_id>/pdf")
@roles_required(UserRole.CLIENT_ADMIN)
def invoice_pdf(invoice_id):
    invoice = tenant_query(Invoice).filter_by(id=invoice_id).first_or_404()
    assert_owns(invoice)
    pdf_bytes = render_invoice_pdf(invoice)
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"{invoice.invoice_number.replace('/', '-')}.pdf",
    )


@invoicing_bp.route("/<int:invoice_id>/void", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def void_invoice_route(invoice_id):
    invoice = tenant_query(Invoice).filter_by(id=invoice_id).first_or_404()
    assert_owns(invoice)
    try:
        void_invoice(invoice, current_user, request.form.get("reason", ""))
        record_audit(
            current_user,
            "invoice_voided",
            tenant_id=invoice.tenant_id,
            entity_type="invoice",
            entity_id=invoice.id,
            details={"reason": invoice.void_reason},
        )
        db.session.commit()
        flash(f"Invoice {invoice.invoice_number} voided.", "success")
        if request.form.get("reissue") and current_user.tenant.has_feature("sales_documents"):
            # Void & re-issue: the corrected copy starts as a duplicate of
            # the voided one, with a new number when it is saved.
            return redirect(url_for("invoicing.new_invoice", duplicate_from=invoice.id))
    except InvoiceValidationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("invoicing.view_invoice", invoice_id=invoice.id))
