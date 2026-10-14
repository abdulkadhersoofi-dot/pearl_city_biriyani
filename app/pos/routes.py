import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import Response, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.auth.decorators import tenant_user_required
from app.extensions import db
from app.models.cash_session import CashSession
from app.models.customer import Customer
from app.models.mixins import utcnow
from app.models.pos_bill import PAYMENT_MODE_LABELS, PaymentMode, POSBill, POSBillStatus
from app.models.pos_return import POSReturn, POSReturnStatus
from app.models.price_list import PriceList
from app.models.product import Product
from app.models.tenant import RegistrationType, Tenant
from app.pos import pos_bp
from app.pos.catalog import build_category_tree, favourite_products
from app.pos.receipt import DEFAULT_PAGE_FORMAT, render_receipt_pdf, render_return_pdf
from app.pos.returns import create_return, return_modes_for, returnable_lines, void_return
from app.pos.services import (
    CartInput,
    PaymentInput,
    POSValidationError,
    checkout,
    discard_held_bill,
    enabled_payment_modes,
    hold_cart,
    parse_bill_discount,
    parse_cart_lines,
    refund_bill,
    void_bill,
    z_report,
)
from app.pos.sessions import (
    add_movement,
    close_session,
    current_session,
    day_close_on,
    open_session,
    require_open_session,
    session_activity,
)
from app.pos.share import load_share_token, qr_svg, share_token, upi_uri, whatsapp_link
from app.pos.stock import branch_stock_status
from app.utils.audit import record_audit
from app.utils.features import current_tenant_has, feature_required
from app.utils.gst import GSTIN_REGEX, normalize_gstin
from app.utils.indian_states import STATE_NAME_BY_CODE
from app.utils.permissions import max_discount_percent, permission_required, user_can
from app.utils.tenant_scope import assert_owns, tenant_query


def _int_or_none(value):
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _cart_from_request() -> CartInput:
    lines = parse_cart_lines(
        request.form.getlist("line_description[]"),
        request.form.getlist("line_hsn[]"),
        request.form.getlist("line_qty[]"),
        request.form.getlist("line_rate[]"),
        request.form.getlist("line_gst_rate[]"),
        request.form.getlist("line_product_id[]"),
        request.form.getlist("line_unit[]"),
        request.form.getlist("line_incl[]"),
        request.form.getlist("line_discount[]"),
        request.form.getlist("line_discount_kind[]"),
        request.form.getlist("line_discount_value[]"),
        serials=request.form.getlist("line_serials[]"),
    )
    kind, value = parse_bill_discount(request.form.get("bill_discount_kind"), request.form.get("bill_discount_value"))
    return CartInput(
        lines=lines,
        customer_id=_int_or_none(request.form.get("customer_id")),
        bill_discount_kind=kind,
        bill_discount_value=value,
        price_list_id=_int_or_none(request.form.get("price_list_id")),
    )


def _money(value, label):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        raise POSValidationError(f"{label} must be a number.")


def _payments_from_request():
    """The original single `payment_mode` field (whole bill one way), or a
    split: pay_mode[] / pay_amount[] / pay_ref[] / pay_return[]."""
    modes = request.form.getlist("pay_mode[]")
    if not modes:
        try:
            return PaymentMode(request.form.get("payment_mode"))
        except ValueError:
            raise POSValidationError("Select a payment mode.")
    amounts = request.form.getlist("pay_amount[]")
    refs = request.form.getlist("pay_ref[]")
    returns = request.form.getlist("pay_return[]")
    payments = []
    for i, mode in enumerate(modes):
        try:
            mode = PaymentMode(mode)
        except ValueError:
            raise POSValidationError("Select a payment mode.")
        payments.append(
            PaymentInput(
                mode=mode,
                amount=_money(amounts[i] if i < len(amounts) else None, "Payment amount"),
                reference=refs[i] if i < len(refs) else None,
                return_number=returns[i] if i < len(returns) else None,
            )
        )
    return payments


def _product_price_lists(tenant):
    if not tenant.has_feature("multi_price_list"):
        return []
    lists = tenant_query(PriceList).filter_by(is_active=True).order_by(PriceList.name).all()
    return [{"id": pl.id, "name": pl.name, "prices": {str(k): float(v) for k, v in pl.price_map().items()}} for pl in lists]


def _default_list_id(tenant):
    if not current_user.is_staff or not current_user.price_list_id or not tenant.has_feature("multi_price_list"):
        return None
    active = tenant_query(PriceList).filter_by(id=current_user.price_list_id, is_active=True).first()
    return active.id if active else None


def _upi_ready(tenant) -> bool:
    return bool(tenant.upi_vpa)


@pos_bp.route("/")
@tenant_user_required
def terminal():
    tenant = current_user.tenant
    # A design with variants isn't sold itself - its sizes/colours are.
    products = (tenant_query(Product).filter_by(is_active=True).filter(Product.has_variants.is_(False))
                .order_by(Product.name).all())
    held_bills = (
        tenant_query(POSBill)
        .filter_by(status=POSBillStatus.HELD)
        .order_by(POSBill.created_at.desc())
        .all()
    )

    # Resuming a held bill: copy it into plain data for the page, render,
    # and only then remove it from the held list. Rendering from the
    # deleted rows (as this used to) crashed whenever a line's item wasn't
    # already loaded - and since the delete was already committed, the
    # held bill was lost along with the page.
    resume_bill = None
    resume_id = _int_or_none(request.args.get("resume"))
    if resume_id:
        resume_bill = tenant_query(POSBill).filter_by(id=resume_id, status=POSBillStatus.HELD).first()
    prefill = _prefill_from_held(resume_bill) if resume_bill else None
    if resume_bill:
        held_bills = [hb for hb in held_bills if hb.id != resume_bill.id]

    stock_status = branch_stock_status(current_user.tenant_id, current_user.id) if current_user.is_staff else []

    category_wise = current_tenant_has("categories")
    category_tree, uncategorized_products = build_category_tree(products) if category_wise else ([], [])
    favourites = favourite_products(tenant.id, products) if current_tenant_has("favourites") else []

    exchange = None
    exchange_number = (request.args.get("exchange") or "").strip()
    if exchange_number:
        ret = tenant_query(POSReturn).filter_by(return_number=exchange_number).first()
        if ret and ret.exchange_balance > 0:
            exchange = {"number": ret.return_number, "balance": float(ret.exchange_balance)}

    last_bill = (
        tenant_query(POSBill)
        .filter(POSBill.created_by_id == current_user.id, POSBill.bill_number.isnot(None))
        .order_by(POSBill.completed_at.desc())
        .first()
    )

    html = render_template(
        "pos/terminal.html",
        products=products,
        held_bills=held_bills,
        prefill=prefill,
        default_receipt_format=tenant.default_receipt_format,
        stock_status=stock_status,
        category_wise_items=category_wise,
        charges_gst=tenant.registration_type == RegistrationType.REGULAR,
        category_tree=category_tree,
        uncategorized_products=uncategorized_products,
        favourites=favourites,
        payment_modes=[{"value": m.value, "label": PAYMENT_MODE_LABELS[m]} for m in enabled_payment_modes(tenant)],
        upi_ready=_upi_ready(tenant),
        discount_cap=max_discount_percent(current_user),
        may_edit_price=user_can(current_user, "edit_price"),
        price_lists=_product_price_lists(tenant),
        default_price_list_id=_default_list_id(tenant),
        day_close=day_close_on(tenant),
        cash_session=current_session(tenant, current_user) if day_close_on(tenant) else None,
        exchange=exchange,
        last_bill=last_bill,
        serial_stock=_serial_stock(tenant, products),
    )
    if resume_bill is not None:
        _remove_held(resume_bill)
    return html


def _serial_stock(tenant, products) -> dict[int, list[str]]:
    """In-stock serial numbers / IMEIs per serial-tracked item, for the
    counter's picker - only with the "serial_imei" module on."""
    if not tenant.has_feature("serial_imei"):
        return {}
    from app.models.inventory import ProductSerial, SerialStatus

    tracked = [p.id for p in products if p.track_serial]
    if not tracked:
        return {}
    stock: dict[int, list[str]] = {pid: [] for pid in tracked}
    for unit in (tenant_query(ProductSerial).filter(ProductSerial.product_id.in_(tracked),
                                                    ProductSerial.status == SerialStatus.IN_STOCK)
                 .order_by(ProductSerial.serial_no).limit(5000)):
        stock[unit.product_id].append(unit.serial_no)
    return stock


def _prefill_from_held(bill: POSBill) -> dict:
    """Everything the terminal needs to rebuild a held cart, as plain
    values - nothing that touches the database once the bill is gone."""
    customer = bill.customer
    return {
        "lines": [
            {
                "product_id": line.product_id,
                "description": line.description,
                "hsn": line.hsn_or_sac_code or "",
                "rate": float(line.rate),
                "incl": bool(line.price_includes_tax),
                "gst_rate": float(line.gst_rate),
                "unit": line.unit or "pcs",
                "qty": float(line.qty),
                "loose": bool(line.product.sells_loose) if line.product else False,
                "disc_kind": line.line_discount_kind or "",
                "disc_value": float(line.line_discount_value or 0),
            }
            for line in bill.lines
        ],
        "customer": (
            {"id": customer.id, "name": customer.name, "phone": customer.phone or "", "gstin": customer.gstin or ""}
            if customer else None
        ),
        "bill_disc_kind": bill.bill_discount_kind or "",
        "bill_disc_value": float(bill.bill_discount_value or 0),
        "price_list_id": bill.price_list_id,
    }


def _remove_held(bill: POSBill) -> None:
    """A resumed bill leaves the held list. If another counter resumed or
    discarded it a moment earlier, there's simply nothing left to remove."""
    try:
        db.session.delete(bill)
        db.session.commit()
    except Exception:
        db.session.rollback()


@pos_bp.route("/hold", methods=["POST"])
@tenant_user_required
def hold_bill():
    try:
        cart = _cart_from_request()
        hold_cart(current_user.tenant, current_user, cart, request.form.get("label"))
        db.session.commit()
        flash("Bill held.", "success")
    except POSValidationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("pos.terminal"))
    # "Resume" tapped with items already in the cart: the cart was held
    # first (above) so it isn't lost, and now the chosen bill opens.
    then_resume = _int_or_none(request.form.get("then_resume"))
    return redirect(url_for("pos.terminal", resume=then_resume) if then_resume else url_for("pos.terminal"))


@pos_bp.route("/held/<int:bill_id>/discard", methods=["POST"])
@tenant_user_required
def discard_held(bill_id):
    bill = tenant_query(POSBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    try:
        discard_held_bill(bill)
        db.session.commit()
        flash("Held bill discarded.", "success")
    except POSValidationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("pos.terminal"))


@pos_bp.route("/checkout", methods=["POST"])
@tenant_user_required
def checkout_route():
    # The terminal submits checkout via fetch() with this header set, so it
    # can show the paper-size picker and print directly from the same
    # screen instead of navigating to a separate receipt page first. A
    # plain form POST (no JS) still gets the old redirect-based flow.
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    tenant = current_user.tenant
    try:
        cart = _cart_from_request()
        payment = _payments_from_request()
        cash_tendered = _money(request.form.get("cash_tendered"), "Cash received")
        session = require_open_session(tenant, current_user)
        bill = checkout(tenant, current_user, cart, payment, cash_tendered=cash_tendered, cash_session=session)
        record_audit(
            current_user,
            "pos_bill_completed",
            tenant_id=current_user.tenant_id,
            entity_type="pos_bill",
            entity_id=bill.id,
            details={
                "bill_number": bill.bill_number,
                "amount": str(bill.grand_total),
                "payments": {p.mode.value: str(p.amount) for p in bill.payments},
                **({"discount": str(bill.total_discount)} if bill.total_discount else {}),
            },
        )
        db.session.commit()
        if wants_json:
            response = {
                "bill_id": bill.id,
                "bill_number": bill.bill_number,
                "grand_total": float(bill.grand_total),
                "change_due": float(bill.change_due) if bill.change_due is not None else None,
                "share_url": url_for("pos.share_bill", bill_id=bill.id),
            }
            if current_user.is_staff:
                # The terminal's own stock counter is rendered once at page
                # load - without this, it only reflects what was just sold
                # after a manual reload. Sending the fresh numbers back
                # here lets the page update itself immediately instead.
                response["stock_status"] = _stock_json()
            return jsonify(response)
        return redirect(url_for("pos.view_receipt", bill_id=bill.id))
    except POSValidationError as exc:
        db.session.rollback()
        if wants_json:
            return jsonify({"error": exc.message}), 400
        flash(exc.message, "error")
        return redirect(url_for("pos.terminal"))


def _stock_json():
    return [
        {
            "product_id": s.product_id,
            "name": s.product_name,
            "unit": s.unit,
            "allocated": float(s.allocated),
            "sold": float(s.sold),
        }
        for s in branch_stock_status(current_user.tenant_id, current_user.id)
    ]


@pos_bp.route("/bills/<int:bill_id>/receipt")
@tenant_user_required
def view_receipt(bill_id):
    bill = tenant_query(POSBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    return render_template(
        "pos/receipt.html", bill=bill, default_receipt_format=current_user.tenant.default_receipt_format
    )


@pos_bp.route("/bills/<int:bill_id>/receipt.pdf")
@tenant_user_required
def receipt_pdf(bill_id):
    bill = tenant_query(POSBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    page_format = request.args.get("format", DEFAULT_PAGE_FORMAT)
    # The first print straight after checkout asks for the original; it is
    # handed out once. Every other print is marked DUPLICATE COPY.
    original = False
    if request.args.get("original") == "1" and bill.original_printed_at is None and bill.bill_number:
        bill.original_printed_at = utcnow()
        db.session.commit()
        original = True
    pdf_bytes = render_receipt_pdf(bill, page_format, copy_label=None if original else "DUPLICATE COPY")
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"{(bill.bill_number or 'receipt').replace('/', '-')}.pdf",
    )


@pos_bp.route("/bills/<int:bill_id>/share")
@tenant_user_required
def share_bill(bill_id):
    """A link the customer can open without signing in, and a WhatsApp
    click-to-chat link carrying it."""
    bill = tenant_query(POSBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    if not bill.bill_number:
        abort(404)
    link = url_for("pos.shared_bill_pdf", token=share_token(bill), _external=True)
    tenant = current_user.tenant
    phone = request.args.get("phone") or (bill.customer_snapshot or {}).get("phone")
    text = f"{tenant.display_name} - bill {bill.bill_number}, Rs. {bill.grand_total:.2f}. View: {link}"
    return jsonify({"url": link, "whatsapp": whatsapp_link(phone, text), "phone": phone or ""})


@pos_bp.route("/shared/<token>.pdf")
def shared_bill_pdf(token):
    """Public, no sign-in: only reachable through a signed, expiring link
    from share_bill."""
    loaded = load_share_token(token)
    if loaded is None:
        abort(404)
    tenant_id, bill_id = loaded
    bill = POSBill.query.filter_by(id=bill_id, tenant_id=tenant_id).first()
    tenant = db.session.get(Tenant, tenant_id)
    if bill is None or not bill.bill_number or tenant is None or not tenant.is_active:
        abort(404)
    pdf_bytes = render_receipt_pdf(bill, "a4", copy_label="CUSTOMER COPY")
    return send_file(
        BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False,
        download_name=f"{bill.bill_number.replace('/', '-')}.pdf",
    )


@pos_bp.route("/upi-qr.svg")
@tenant_user_required
def upi_qr():
    tenant = current_user.tenant
    if not _upi_ready(tenant):
        abort(404)
    try:
        amount = Decimal(request.args.get("amount", "0"))
    except InvalidOperation:
        abort(400)
    if amount <= 0:
        abort(400)
    note = (request.args.get("note") or f"{tenant.display_name} bill")[:50]
    svg = qr_svg(upi_uri(tenant.upi_vpa, tenant.upi_payee_name or tenant.display_name, amount, note))
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


# --- customers at the counter ------------------------------------------------

def _customer_json(c: Customer) -> dict:
    return {"id": c.id, "name": c.name, "phone": c.phone or "", "gstin": c.gstin or ""}


@pos_bp.route("/customers/search")
@tenant_user_required
def customer_search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    customers = (
        tenant_query(Customer)
        .filter(Customer.is_active.is_(True))
        .filter(db.or_(Customer.phone.ilike(like), Customer.name.ilike(like), Customer.gstin.ilike(like)))
        .order_by(Customer.name)
        .limit(8)
        .all()
    )
    return jsonify([_customer_json(c) for c in customers])


@pos_bp.route("/customers/quick-add", methods=["POST"])
@tenant_user_required
def customer_quick_add():
    tenant = current_user.tenant
    name = (request.form.get("name") or "").strip()
    phone = "".join(ch for ch in (request.form.get("phone") or "") if ch.isdigit() or ch == "+")[:20]
    raw_gstin = (request.form.get("gstin") or "").strip()
    if not name:
        return jsonify({"error": "Enter the customer's name."}), 400
    if len(name) > 255:
        return jsonify({"error": "That name is too long."}), 400
    gstin = None
    if raw_gstin:
        gstin = normalize_gstin(raw_gstin)
        if not gstin or not re.match(GSTIN_REGEX, gstin):
            return jsonify({"error": "That GSTIN doesn't look right - 15 characters, e.g. 29ABCDE1234F1Z5."}), 400
    if phone:
        existing = tenant_query(Customer).filter_by(phone=phone, is_active=True).first()
        if existing:
            return jsonify(_customer_json(existing))
    home = tenant.gstins.filter_by(is_active=True).first()
    state_code = gstin[:2] if gstin and gstin[:2] in STATE_NAME_BY_CODE else (home.state_code if home else None)
    customer = Customer(
        tenant_id=tenant.id,
        name=name,
        phone=phone or None,
        gstin=gstin,
        state_code=state_code,
        state_name=STATE_NAME_BY_CODE.get(state_code) if state_code else None,
    )
    db.session.add(customer)
    db.session.flush()
    record_audit(current_user, "customer_created", tenant_id=tenant.id, entity_type="customer", entity_id=customer.id,
                 details={"name": name, "from": "pos"})
    db.session.commit()
    return jsonify(_customer_json(customer))


# --- refunds, voids, returns -------------------------------------------------

@pos_bp.route("/bills/<int:bill_id>/refund", methods=["POST"])
@tenant_user_required
@permission_required("refund")
def refund_bill_route(bill_id):
    bill = tenant_query(POSBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    redirect_date = request.form.get("redirect_date") or None
    try:
        amount = Decimal(request.form.get("amount", "0"))
    except InvalidOperation:
        amount = Decimal("0")
    try:
        refund_mode = None
        if request.form.get("refund_mode"):
            try:
                refund_mode = PaymentMode(request.form.get("refund_mode"))
            except ValueError:
                raise POSValidationError("Pick how the refund was paid.")
        session = require_open_session(current_user.tenant, current_user)
        refund_bill(bill, current_user, amount, request.form.get("reason", ""), refund_mode, cash_session=session)
        record_audit(
            current_user,
            "pos_bill_refunded",
            tenant_id=bill.tenant_id,
            entity_type="pos_bill",
            entity_id=bill.id,
            details={"amount": str(amount), "reason": bill.refund_reason},
        )
        db.session.commit()
        flash(f"Rs. {amount} refunded.", "success")
    except POSValidationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("pos.day_book", date=redirect_date))


@pos_bp.route("/bills/<int:bill_id>/void", methods=["POST"])
@tenant_user_required
@permission_required("void")
def void_bill_route(bill_id):
    bill = tenant_query(POSBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    try:
        void_bill(bill, current_user, request.form.get("reason", ""))
        record_audit(current_user, "pos_bill_voided", tenant_id=bill.tenant_id, entity_type="pos_bill",
                     entity_id=bill.id, details={"bill_number": bill.bill_number, "reason": bill.void_reason})
        db.session.commit()
        flash(f"{bill.bill_number} voided.", "success")
    except POSValidationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("pos.day_book", date=request.form.get("redirect_date") or None))


@pos_bp.route("/bills/<int:bill_id>/return", methods=["GET", "POST"])
@tenant_user_required
@permission_required("refund")
def return_items(bill_id):
    bill = tenant_query(POSBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    tenant = current_user.tenant
    if request.method == "POST":
        quantities = {}
        try:
            for key, value in request.form.items():
                if key.startswith("qty_") and value.strip():
                    try:
                        quantities[int(key[4:])] = Decimal(value.strip())
                    except (ValueError, InvalidOperation):
                        raise POSValidationError("Quantities must be numbers.")
            try:
                refund_mode = PaymentMode(request.form.get("refund_mode"))
            except ValueError:
                raise POSValidationError("Pick how the money goes back to the customer.")
            session = require_open_session(tenant, current_user)
            ret = create_return(bill, current_user, quantities, request.form.get("reason", ""), refund_mode, cash_session=session)
            record_audit(current_user, "pos_return_created", tenant_id=tenant.id, entity_type="pos_return",
                         entity_id=ret.id, details={"return_number": ret.return_number, "bill_number": bill.bill_number,
                                                    "amount": str(ret.grand_total), "refund_mode": ret.refund_mode.value})
            db.session.commit()
            flash(f"Return {ret.return_number} saved - Rs. {ret.grand_total:.2f}.", "success")
            return redirect(url_for("pos.view_return", return_id=ret.id))
        except POSValidationError as exc:
            db.session.rollback()
            flash(exc.message, "error")
    return render_template(
        "pos/return_form.html",
        bill=bill,
        lines=returnable_lines(bill),
        modes=[(m.value, PAYMENT_MODE_LABELS[m]) for m in return_modes_for(bill)],
        default_mode=(bill.payments[0].mode.value if len(bill.payments) == 1 else "cash"),
    )


@pos_bp.route("/returns/<int:return_id>")
@tenant_user_required
def view_return(return_id):
    ret = tenant_query(POSReturn).filter_by(id=return_id).first_or_404()
    assert_owns(ret)
    return render_template(
        "pos/return_view.html", ret=ret, labels=PAYMENT_MODE_LABELS,
        default_receipt_format=current_user.tenant.default_receipt_format,
    )


@pos_bp.route("/returns/<int:return_id>/slip.pdf")
@tenant_user_required
def return_pdf(return_id):
    ret = tenant_query(POSReturn).filter_by(id=return_id).first_or_404()
    assert_owns(ret)
    pdf_bytes = render_return_pdf(ret, request.args.get("format", DEFAULT_PAGE_FORMAT))
    return send_file(BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False,
                     download_name=f"{ret.return_number.replace('/', '-')}.pdf")


@pos_bp.route("/returns/<int:return_id>/void", methods=["POST"])
@tenant_user_required
@permission_required("void")
def void_return_route(return_id):
    ret = tenant_query(POSReturn).filter_by(id=return_id).first_or_404()
    assert_owns(ret)
    try:
        void_return(ret, current_user, request.form.get("reason", ""))
        record_audit(current_user, "pos_return_voided", tenant_id=ret.tenant_id, entity_type="pos_return",
                     entity_id=ret.id, details={"return_number": ret.return_number, "reason": ret.void_reason})
        db.session.commit()
        flash(f"{ret.return_number} voided.", "success")
    except POSValidationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("pos.view_return", return_id=ret.id))


# --- day open / close ----------------------------------------------------------

@pos_bp.route("/day/open", methods=["POST"])
@tenant_user_required
@feature_required("day_close")
def day_open():
    try:
        session = open_session(current_user.tenant, current_user, request.form.get("opening_cash", "0"))
        record_audit(current_user, "pos_day_opened", tenant_id=current_user.tenant_id, entity_type="cash_session",
                     entity_id=session.id, details={"opening_cash": str(session.opening_cash)})
        db.session.commit()
        flash("Day opened.", "success")
    except POSValidationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("pos.terminal"))


@pos_bp.route("/day/cash", methods=["POST"])
@tenant_user_required
@feature_required("day_close")
def day_cash():
    try:
        session = require_open_session(current_user.tenant, current_user)
        movement = add_movement(session, current_user, request.form.get("kind"), request.form.get("amount"), request.form.get("reason"))
        record_audit(current_user, "pos_cash_movement", tenant_id=current_user.tenant_id, entity_type="cash_session",
                     entity_id=session.id, details={"kind": movement.kind, "amount": str(movement.amount), "reason": movement.reason})
        db.session.commit()
        flash(f"Cash {'in' if movement.kind == 'in' else 'out'} of Rs. {movement.amount:.2f} recorded.", "success")
    except POSValidationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(request.form.get("next") or url_for("pos.day_close"))


@pos_bp.route("/day/close", methods=["GET", "POST"])
@tenant_user_required
@feature_required("day_close")
def day_close():
    tenant = current_user.tenant
    session = current_session(tenant, current_user)
    if request.method == "POST":
        try:
            if session is None:
                raise POSValidationError("There is no open day on this counter.")
            close_session(session, current_user, request.form.get("counted_cash", ""), request.form.get("note"))
            record_audit(current_user, "pos_day_closed", tenant_id=tenant.id, entity_type="cash_session", entity_id=session.id,
                         details={"expected": str(session.expected_cash), "counted": str(session.counted_cash),
                                  "difference": str(session.difference)})
            db.session.commit()
            flash(f"Day closed. Difference: Rs. {session.difference:.2f}.", "success")
            return redirect(url_for("pos.day_session", session_id=session.id))
        except POSValidationError as exc:
            db.session.rollback()
            flash(exc.message, "error")
    activity = session_activity(session) if session else None
    return render_template("pos/day_close.html", session=session, activity=activity, labels=PAYMENT_MODE_LABELS)


@pos_bp.route("/day/<int:session_id>")
@tenant_user_required
@feature_required("day_close")
def day_session(session_id):
    session = tenant_query(CashSession).filter_by(id=session_id).first_or_404()
    assert_owns(session)
    if session.user_id != current_user.id and not user_can(current_user, "view_reports"):
        abort(403)
    if current_user.is_staff and session.user_id != current_user.id:
        abort(403)
    return render_template("pos/day_close.html", session=session, activity=session_activity(session), labels=PAYMENT_MODE_LABELS)


# --- day-book and Z-report -----------------------------------------------------

@pos_bp.route("/bills")
@tenant_user_required
@permission_required("view_reports")
def day_book():
    on_date = _parse_date(request.args.get("date")) or date.today()
    start = datetime.combine(on_date, datetime.min.time())
    end = datetime.combine(on_date, datetime.max.time())
    bills = (
        tenant_query(POSBill)
        .filter(
            POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED, POSBillStatus.VOIDED]),
            POSBill.completed_at >= start,
            POSBill.completed_at <= end,
        )
        .order_by(POSBill.completed_at.desc())
        .all()
    )
    returns = (
        tenant_query(POSReturn)
        .filter(POSReturn.returned_at >= start, POSReturn.returned_at <= end)
        .order_by(POSReturn.returned_at.desc())
        .all()
    )
    tenant = current_user.tenant
    return render_template(
        "pos/day_book.html",
        bills=bills,
        returns=returns,
        on_date=on_date,
        default_receipt_format=tenant.default_receipt_format,
        refund_modes=[(m.value, PAYMENT_MODE_LABELS[m]) for m in enabled_payment_modes(tenant) if m != PaymentMode.CREDIT],
        labels=PAYMENT_MODE_LABELS,
    )


@pos_bp.route("/z-report")
@tenant_user_required
@permission_required("view_reports")
def z_report_view():
    on_date = _parse_date(request.args.get("date")) or date.today()
    report = z_report(current_user.tenant_id, on_date)
    sessions = []
    if day_close_on(current_user.tenant):
        query = tenant_query(CashSession).filter_by(business_date=on_date)
        if current_user.is_staff:
            query = query.filter_by(user_id=current_user.id)
        sessions = [session_activity(s) for s in query.order_by(CashSession.opened_at).all()]
    return render_template(
        "pos/z_report.html", report=report, PaymentMode=PaymentMode, labels=PAYMENT_MODE_LABELS,
        sessions=sessions, shown_modes=_modes_with_activity(report),
    )


def _modes_with_activity(report):
    """Cash/UPI/Card always (as before), plus any other mode that was used."""
    always = {PaymentMode.CASH, PaymentMode.UPI, PaymentMode.CARD}
    return [
        m for m in PaymentMode
        if m != PaymentMode.MIXED and (m in always or report["by_mode"][m]["count"] or report["refunds_by_mode"][m])
    ]


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
