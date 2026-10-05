from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.auth.decorators import tenant_user_required
from app.extensions import db
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus
from app.models.product import Product
from app.pos import pos_bp
from app.pos.receipt import DEFAULT_PAGE_FORMAT, render_receipt_pdf
from app.pos.services import (
    CartInput,
    POSValidationError,
    checkout,
    discard_held_bill,
    hold_cart,
    parse_cart_lines,
    refund_bill,
    z_report,
)
from app.utils.audit import record_audit
from app.utils.tenant_scope import assert_owns, tenant_query


def _cart_from_request() -> CartInput:
    lines = parse_cart_lines(
        request.form.getlist("line_description[]"),
        request.form.getlist("line_hsn[]"),
        request.form.getlist("line_qty[]"),
        request.form.getlist("line_rate[]"),
        request.form.getlist("line_gst_rate[]"),
        request.form.getlist("line_product_id[]"),
        request.form.getlist("line_unit[]"),
    )
    return CartInput(lines=lines)


@pos_bp.route("/")
@tenant_user_required
def terminal():
    tenant = current_user.tenant
    products = tenant_query(Product).filter_by(is_active=True).order_by(Product.name).all()
    held_bills = (
        tenant_query(POSBill)
        .filter_by(status=POSBillStatus.HELD)
        .order_by(POSBill.created_at.desc())
        .all()
    )

    resume_bill = None
    resume_id = request.args.get("resume")
    if resume_id:
        resume_bill = tenant_query(POSBill).filter_by(id=resume_id, status=POSBillStatus.HELD).first()
        if resume_bill:
            db.session.delete(resume_bill)
            db.session.commit()
            held_bills = [hb for hb in held_bills if hb.id != resume_bill.id]

    return render_template(
        "pos/terminal.html", products=products, held_bills=held_bills, resume_bill=resume_bill
    )


@pos_bp.route("/hold", methods=["POST"])
@tenant_user_required
def hold_bill():
    try:
        cart = _cart_from_request()
        bill = hold_cart(current_user.tenant, current_user, cart, request.form.get("label"))
        db.session.commit()
        flash("Bill held.", "success")
    except POSValidationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("pos.terminal"))


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
    try:
        cart = _cart_from_request()
        try:
            payment_mode = PaymentMode(request.form.get("payment_mode"))
        except ValueError:
            raise POSValidationError("Select a payment mode.")

        bill = checkout(current_user.tenant, current_user, cart, payment_mode)
        record_audit(
            current_user,
            "pos_bill_completed",
            tenant_id=current_user.tenant_id,
            entity_type="pos_bill",
            entity_id=bill.id,
            details={"bill_number": bill.bill_number, "amount": str(bill.grand_total)},
        )
        db.session.commit()
        if wants_json:
            return jsonify({"bill_id": bill.id, "bill_number": bill.bill_number})
        return redirect(url_for("pos.view_receipt", bill_id=bill.id))
    except POSValidationError as exc:
        db.session.rollback()
        if wants_json:
            return jsonify({"error": exc.message}), 400
        flash(exc.message, "error")
        return redirect(url_for("pos.terminal"))


@pos_bp.route("/bills/<int:bill_id>/receipt")
@tenant_user_required
def view_receipt(bill_id):
    bill = tenant_query(POSBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    return render_template("pos/receipt.html", bill=bill)


@pos_bp.route("/bills/<int:bill_id>/receipt.pdf")
@tenant_user_required
def receipt_pdf(bill_id):
    bill = tenant_query(POSBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    page_format = request.args.get("format", DEFAULT_PAGE_FORMAT)
    pdf_bytes = render_receipt_pdf(bill, page_format)
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"{(bill.bill_number or 'receipt').replace('/', '-')}.pdf",
    )


@pos_bp.route("/bills/<int:bill_id>/refund", methods=["POST"])
@tenant_user_required
def refund_bill_route(bill_id):
    bill = tenant_query(POSBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    redirect_date = request.form.get("redirect_date") or None
    try:
        amount = Decimal(request.form.get("amount", "0"))
    except InvalidOperation:
        amount = Decimal("0")
    try:
        refund_bill(bill, current_user, amount, request.form.get("reason", ""))
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


@pos_bp.route("/bills")
@tenant_user_required
def day_book():
    on_date = _parse_date(request.args.get("date")) or date.today()
    start = datetime.combine(on_date, datetime.min.time())
    end = datetime.combine(on_date, datetime.max.time())
    bills = (
        tenant_query(POSBill)
        .filter(
            POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
            POSBill.completed_at >= start,
            POSBill.completed_at <= end,
        )
        .order_by(POSBill.completed_at.desc())
        .all()
    )
    return render_template("pos/day_book.html", bills=bills, on_date=on_date)


@pos_bp.route("/z-report")
@tenant_user_required
def z_report_view():
    on_date = _parse_date(request.args.get("date")) or date.today()
    report = z_report(current_user.tenant_id, on_date)
    return render_template("pos/z_report.html", report=report, PaymentMode=PaymentMode)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
