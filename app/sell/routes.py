"""New sale: the one form (app/sell/decide.py decides what it makes).

    GET  /sell/new             the form
    POST /sell/new/plan.json   what saving would make, for the sentence above
                               the button and the stock beside each line
    POST /sell/new             save: the invoice and/or order(s), all or nothing
    GET  /sell/sale/<ref>      what one sale made, with links
    GET  /sell/invoice/<id>.pdf  the invoice, for the branch login that made it

The owner, and a branch login where the warehouses pick orders (the same
rule as the Sales orders screen)."""

from datetime import date
from io import BytesIO

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.invoicing.forms import InvoiceHeaderForm
from app.invoicing.routes import header_form_choices, resolve_customer_id
from app.invoicing.services import InvoiceValidationError, parse_line_arrays
from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.invoice_series import DOCUMENT_TYPE_LABELS, DocumentType
from app.models.sales import CHALLAN_PURPOSE_LABELS, ChallanPurpose
from app.models.user import UserRole
from app.sales.services import SalesError
from app.sell import decide, sell_bp
from app.utils.audit import record_audit
from app.utils.tenant_scope import tenant_query
from app.warehouse import dispatch, notify

ADMIN, STAFF = UserRole.CLIENT_ADMIN, UserRole.STAFF


def _allowed() -> None:
    if current_user.is_staff and not decide.can_order(current_user.tenant, current_user):
        abort(403)


def _mode() -> str:
    mode = request.form.get("sale_mode") or decide.SALE
    if current_user.is_staff or mode not in decide.MODES:
        return decide.SALE                       # quotes and challans are the owner's
    if mode != decide.SALE and not current_user.tenant.has_feature("sales_documents"):
        return decide.SALE
    return mode


def _sale_lines() -> list[decide.SaleLine]:
    """The form's rows, each with where it comes from and any serial numbers.
    Blank rows are skipped the way the invoice form skips them."""
    f = request.form
    descriptions = f.getlist("line_description[]")
    columns = [f.getlist(k) for k in ("line_hsn[]", "line_qty[]", "line_rate[]", "line_discount[]",
                                      "line_gst_rate[]", "line_product_id[]", "line_unit[]", "line_incl[]")]
    sources, serials = f.getlist("line_from[]"), f.getlist("line_serials[]")
    out = []
    for i, description in enumerate(descriptions):
        if not (description or "").strip():
            continue
        line = parse_line_arrays([description], *[[c[i]] if i < len(c) else [] for c in columns])[0]
        typed = serials[i] if i < len(serials) else ""
        line.serials = [s.strip() for s in typed.replace(",", "\n").replace(";", "\n").split("\n") if s.strip()][:200]
        out.append(decide.SaleLine(line, (sources[i] if i < len(sources) else "") or decide.AUTO))
    return out


def _here():
    return decide.seller_location(current_user.tenant, current_user, request.values.get("selling_from"))


def _pincode() -> str | None:
    if request.form.get("ship_elsewhere"):
        return request.form.get("ship_pincode")
    customer_id = request.form.get("customer_id")
    if customer_id and customer_id.isdigit():
        customer = tenant_query(Customer).filter_by(id=int(customer_id)).first()
        return customer.pincode if customer else None
    return None


def _plan() -> decide.Plan:
    return decide.decide(current_user.tenant, current_user, _here(), _sale_lines(), mode=_mode(),
                         together=bool(request.form.get("together")),
                         finance=bool(request.form.get("is_finance_sale")), pincode=_pincode())


def _qty(value) -> str:
    return f"{value.normalize():f}" if value is not None else ""


@sell_bp.route("/new/plan.json", methods=["POST"])
@roles_required(ADMIN, STAFF)
def plan_json():
    _allowed()
    try:
        plan = _plan()
    except (decide.SellError, InvoiceValidationError) as exc:
        db.session.rollback()
        return jsonify({"ok": False, "sentence": "", "button": "", "problems": [exc.message], "lines": []})
    db.session.rollback()               # a branch's location may have been made on the way: nothing to keep
    return jsonify({
        "ok": not plan.problems, "sentence": plan.sentence, "button": plan.button, "problems": plan.problems,
        "here": plan.here.name if plan.here is not None else None,
        "lines": [{"here": None if n.here is None else _qty(n.here),
                   "elsewhere": [{"name": name, "free": _qty(free)} for name, free in n.elsewhere],
                   "here_qty": _qty(n.here_qty), "order_qty": _qty(n.order_qty), "waiting_qty": _qty(n.waiting_qty),
                   "needs_serials": n.needs_serials} for n in plan.notes],
    })


def _finance() -> dict:
    from app.sales.routes import _finance_form

    return _finance_form()


def _ship_to() -> dict | None:
    if not request.form.get("ship_elsewhere"):
        return None
    from app.sales.services import SHIP_TO_KEYS

    return {k: request.form.get(f"ship_{k}", "") for k in SHIP_TO_KEYS}


def _products():
    from app.models.product import Product

    return (tenant_query(Product).filter_by(is_active=True).filter(Product.has_variants.is_(False))
            .order_by(Product.name).all())


@sell_bp.route("/new", methods=["GET", "POST"])
@roles_required(ADMIN, STAFF)
def new_sale():
    _allowed()
    tenant = current_user.tenant
    form = InvoiceHeaderForm()
    customers, _branches = header_form_choices(form, tenant)
    from app.sales.routes import _convertible_choices, _customer_addresses

    _convertible_choices(form)
    form.invoice_date.label.text = "Date"
    form.document_type.label.text = "Billed as"
    if form.validate_on_submit():
        try:
            plan = _plan()
            purpose = request.form.get("challan_purpose") or None
            header = decide.Header(
                gstin_id=form.gstin_id.data, customer_id=resolve_customer_id(form, tenant),
                place_of_supply_state_code=form.place_of_supply_state_code.data, on=form.invoice_date.data,
                document_type=DocumentType(form.document_type.data), notes=form.notes.data,
                terms=request.form.get("terms"), ship_to=_ship_to(),
                valid_until=_date(request.form.get("valid_until")),
                challan_purpose=ChallanPurpose(purpose) if purpose in {p.value for p in ChallanPurpose} else None,
                finance=_finance())
            result = decide.save(tenant, current_user, header, plan)
            for made in result.everything:
                is_invoice = isinstance(made, Invoice)
                record_audit(current_user, "invoice_created" if is_invoice else "sales_document_created",
                             tenant_id=tenant.id, entity_type="invoice" if is_invoice else "sales_document",
                             entity_id=made.id,
                             details={"number": made.invoice_number if is_invoice else made.number,
                                      "via": "new_sale", **({"sale_ref": result.ref} if result.ref else {})})
            db.session.commit()
            notify.send_pending_quietly()
            flash(_made_message(result), "success")
            if result.invoice is not None and result.invoice.customer_id and current_user.is_client_admin:
                from app.accounts.receivables import credit_warning

                warning = credit_warning(tenant, result.invoice.customer)
                if warning:
                    flash(warning, "warning")
            return redirect(_after(result))
        except (decide.SellError, InvoiceValidationError, SalesError) as exc:
            db.session.rollback()
            flash(exc.message, "error")
    here = _here()
    db.session.rollback()
    warehouses = [w for w in dispatch.warehouses(tenant.id) if here is None or w.id != here.id] \
        if decide.can_order(tenant, current_user) and dispatch.is_on(tenant) else []
    from app.warehouse.emi import active_financiers

    return render_template(
        "sell/new_sale.html", form=form, products=_products(), customers=customers, branches=[],
        duplicate_from=None, prefill_lines=None, source_doc=None, source_run=None,
        here=here, places=decide.selling_places(tenant) if current_user.is_client_admin else [],
        warehouses=warehouses, can_order=decide.can_order(tenant, current_user),
        dispatch_on=dispatch.is_on(tenant), purposes=CHALLAN_PURPOSE_LABELS,
        modes_on=current_user.is_client_admin and tenant.has_feature("sales_documents"),
        financiers=active_financiers(tenant.id) if dispatch.is_on(tenant) else [],
        customer_addresses=_customer_addresses() if dispatch.is_on(tenant) else {},
        serials_on=tenant.has_feature("serial_imei") and tenant.has_feature("inventory"))


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _name(made) -> str:
    if isinstance(made, Invoice):
        return f"{DOCUMENT_TYPE_LABELS.get(made.document_type, 'Invoice')} {made.invoice_number}"
    where = f" to {made.dispatch_location.name}" if made.dispatch_location is not None else \
        (" (waiting for stock)" if made.awaiting_stock else "")
    return f"{made.label} {made.number}{where}"


def _made_message(result: decide.Result) -> str:
    return "Saved: " + ", ".join(_name(m) for m in result.everything) + "."


def _after(result: decide.Result) -> str:
    made = result.everything
    if len(made) > 1 or current_user.is_staff and result.invoice is not None:
        ref = result.ref
        if ref is None:                       # a branch's single invoice: give it a reference to open it by
            ref = result.invoice.sale_ref = decide.new_ref()
            db.session.commit()
        return url_for("sell.sale", ref=ref)
    if result.invoice is not None:
        return url_for("invoicing.view_invoice", invoice_id=result.invoice.id)
    return url_for("sales.view_document", doc_id=made[0].id)


def _mine(rows):
    """A branch login sees only what it made itself."""
    return [r for r in rows if not current_user.is_staff or r.created_by_id == current_user.id]


@sell_bp.route("/sale/<ref>")
@roles_required(ADMIN, STAFF)
def sale(ref):
    """Everything one sale made: the invoice for what was here, the order(s)
    for the rest, and where each one stands."""
    _allowed()
    found = decide.siblings(current_user.tenant_id, ref)
    invoices, documents = _mine(found["invoices"]), _mine(found["documents"])
    if not invoices and not documents:
        abort(404)
    return render_template("sell/sale.html", ref=ref, invoices=invoices, documents=documents)


@sell_bp.route("/invoice/<int:invoice_id>.pdf")
@roles_required(ADMIN, STAFF)
def invoice_pdf(invoice_id):
    """The invoice a sale made - a branch login may open the ones it made."""
    _allowed()
    from app.invoicing.pdf import render_invoice_pdf

    invoice = tenant_query(Invoice).filter_by(id=invoice_id).first_or_404()
    if current_user.is_staff and (invoice.created_by_id != current_user.id or not invoice.sale_ref):
        abort(404)
    return send_file(BytesIO(render_invoice_pdf(invoice)), mimetype="application/pdf", as_attachment=False,
                     download_name=f"{invoice.invoice_number.replace('/', '-')}.pdf")
