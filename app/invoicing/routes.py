from flask import abort, flash, redirect, render_template, request, send_file, url_for
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
from app.models.user import UserRole
from app.utils.audit import record_audit
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
    form.gstin_id.choices = [(g.id, g.display_label) for g in tenant.gstins.filter_by(is_active=True)]
    form.document_type.choices = _tenant_document_type_choices()
    customers = tenant_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    form.customer_id.choices = [("new", "+ New customer")] + [(str(c.id), c.name) for c in customers]

    duplicate_from = None
    if request.method == "GET" and request.args.get("duplicate_from"):
        duplicate_from = tenant_query(Invoice).filter_by(id=request.args["duplicate_from"]).first()
        if duplicate_from:
            form.gstin_id.data = duplicate_from.gstin_id
            form.document_type.data = duplicate_from.document_type.value
            form.customer_id.data = str(duplicate_from.customer_id)
            form.place_of_supply_state_code.data = duplicate_from.place_of_supply_state_code
            form.notes.data = duplicate_from.notes

    if form.validate_on_submit():
        try:
            customer_id = form.customer_id.data
            if customer_id == "new":
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
                customer_id = customer.id
            else:
                customer_id = int(customer_id)

            lines = parse_line_arrays(
                request.form.getlist("line_description[]"),
                request.form.getlist("line_hsn[]"),
                request.form.getlist("line_qty[]"),
                request.form.getlist("line_rate[]"),
                request.form.getlist("line_discount[]"),
                request.form.getlist("line_gst_rate[]"),
                request.form.getlist("line_product_id[]"),
                request.form.getlist("line_unit[]"),
            )

            invoice = create_invoice(
                tenant,
                current_user,
                InvoiceInput(
                    gstin_id=form.gstin_id.data,
                    document_type=DocumentType(form.document_type.data),
                    customer_id=customer_id,
                    place_of_supply_state_code=form.place_of_supply_state_code.data,
                    invoice_date=form.invoice_date.data,
                    notes=form.notes.data,
                    lines=lines,
                ),
            )
            record_audit(
                current_user,
                "invoice_created",
                tenant_id=tenant.id,
                entity_type="invoice",
                entity_id=invoice.id,
                details={"invoice_number": invoice.invoice_number},
            )
            db.session.commit()
            flash(f"Invoice {invoice.invoice_number} created.", "success")
            return redirect(url_for("invoicing.view_invoice", invoice_id=invoice.id))
        except InvoiceValidationError as exc:
            db.session.rollback()
            flash(exc.message, "error")

    products = tenant_query(Product).filter_by(is_active=True).order_by(Product.name).all()
    return render_template(
        "invoicing/form.html", form=form, products=products, customers=customers, duplicate_from=duplicate_from
    )


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
    except InvoiceValidationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("invoicing.view_invoice", invoice_id=invoice.id))
