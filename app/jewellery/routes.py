from datetime import date
from io import BytesIO

from flask import flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.auth.decorators import roles_required, tenant_user_required
from app.extensions import db
from app.jewellery import jewellery_bp, services
from app.models.business import OldGoldPurchase, OldGoldStatus
from app.models.customer import Customer
from app.models.product import Product
from app.models.user import UserRole
from app.pos.receipt import _render_pdf
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.gst_config import JEWELLERY_JOB_WORK_GST_RATE, JEWELLERY_JOB_WORK_SAC, METAL_PURITIES
from app.utils.jewellery import JewelleryError
from app.utils.tenant_scope import assert_owns, tenant_query

FLAG = "jewellery"
JOB_WORK_ITEM = "Making charges (job work on customer's gold)"


@jewellery_bp.route("/rates", methods=["GET", "POST"])
@tenant_user_required
@feature_required(FLAG)
def rates():
    tenant = current_user.tenant
    if request.method == "POST":
        if not current_user.is_client_admin:
            flash("Only the owner's login can set the day's rates.", "error")
            return redirect(url_for("jewellery.rates"))
        saved = 0
        try:
            for metal, grades in METAL_PURITIES.items():
                for purity in grades:
                    value = (request.form.get(f"rate_{metal}_{purity}") or "").strip()
                    if value:
                        row = services.set_rate(tenant, current_user, metal, purity, value)
                        saved += 1
                        record_audit(current_user, "metal_rate_set", tenant_id=tenant.id,
                                     details={"metal": metal, "purity": purity, "rate": str(row.rate_per_gram)})
            db.session.commit()
            flash(f"{saved} rate{'s' if saved != 1 else ''} set for today." if saved else "Nothing entered.",
                  "success" if saved else "warning")
        except JewelleryError as exc:
            db.session.rollback()
            flash(exc.message, "error")
        return redirect(url_for("jewellery.rates"))
    job_item = tenant_query(Product).filter_by(name=JOB_WORK_ITEM, is_active=True).first()
    return render_template("jewellery/rates.html", board=services.rate_board(tenant), today=date.today(),
                           job_item=job_item, job_rate=JEWELLERY_JOB_WORK_GST_RATE, job_sac=JEWELLERY_JOB_WORK_SAC)


@jewellery_bp.route("/job-work-item", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required(FLAG)
def job_work_item():
    """The service item for making charges on the customer's own gold - SAC
    9988 at 5% (gst_config), billed on its own line."""
    if tenant_query(Product).filter_by(name=JOB_WORK_ITEM, is_active=True).first() is None:
        db.session.add(Product(tenant_id=current_user.tenant_id, name=JOB_WORK_ITEM, hsn_or_sac_code=JEWELLERY_JOB_WORK_SAC,
                               gst_rate=JEWELLERY_JOB_WORK_GST_RATE, is_service=True, unit="NOS", sale_price=0,
                               price_includes_tax=False))
        record_audit(current_user, "job_work_item_created", tenant_id=current_user.tenant_id)
        db.session.commit()
        flash("Job-work making charge item added - bill it with the agreed making charge as the price.", "success")
    return redirect(url_for("jewellery.rates"))


@jewellery_bp.route("/old-gold")
@tenant_user_required
@feature_required(FLAG)
def old_gold_list():
    vouchers = tenant_query(OldGoldPurchase).order_by(OldGoldPurchase.id.desc()).limit(300).all()
    return render_template("jewellery/old_gold_list.html", vouchers=vouchers)


@jewellery_bp.route("/old-gold/new", methods=["GET", "POST"])
@tenant_user_required
@feature_required(FLAG)
def old_gold_new():
    tenant = current_user.tenant
    if request.method == "POST":
        try:
            voucher = services.create_old_gold(tenant, current_user, request.form)
            record_audit(current_user, "old_gold_purchased", tenant_id=tenant.id, entity_type="old_gold_purchase",
                         entity_id=voucher.id, details={"number": voucher.number, "value": str(voucher.value)})
            db.session.commit()
            flash(f"Voucher {voucher.number} for Rs. {voucher.value:.2f} - use it as 'Old gold' at the counter.", "success")
            return redirect(url_for("jewellery.old_gold_view", voucher_id=voucher.id))
        except JewelleryError as exc:
            db.session.rollback()
            flash(exc.message, "error")
    return render_template("jewellery/old_gold_form.html", metals=METAL_PURITIES, board=services.rate_board(tenant),
                           customers=tenant_query(Customer).filter_by(is_active=True).order_by(Customer.name).all(),
                           form=request.form)


def _voucher(voucher_id):
    voucher = tenant_query(OldGoldPurchase).filter_by(id=voucher_id).first_or_404()
    assert_owns(voucher)
    return voucher


@jewellery_bp.route("/old-gold/<int:voucher_id>")
@tenant_user_required
@feature_required(FLAG)
def old_gold_view(voucher_id):
    return render_template("jewellery/old_gold_view.html", v=_voucher(voucher_id))


@jewellery_bp.route("/old-gold/<int:voucher_id>.pdf")
@tenant_user_required
@feature_required(FLAG)
def old_gold_pdf(voucher_id):
    voucher = _voucher(voucher_id)
    fmt = request.args.get("format") or current_user.tenant.default_receipt_format
    pdf = _render_pdf("jewellery/old_gold_print.html", current_user.tenant, {"v": voucher}, 6, fmt)
    return send_file(BytesIO(pdf), mimetype="application/pdf", download_name=f"{voucher.number.replace('/', '-')}.pdf")


@jewellery_bp.route("/old-gold/<int:voucher_id>/void", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required(FLAG)
def old_gold_void(voucher_id):
    voucher = _voucher(voucher_id)
    try:
        services.void_old_gold(voucher, current_user, request.form.get("reason", ""))
        record_audit(current_user, "old_gold_voided", tenant_id=voucher.tenant_id, entity_type="old_gold_purchase",
                     entity_id=voucher.id, details={"reason": voucher.void_reason})
        db.session.commit()
        flash(f"Voucher {voucher.number} voided.", "success")
    except JewelleryError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("jewellery.old_gold_view", voucher_id=voucher.id))


@jewellery_bp.route("/old-gold/lookup")
@tenant_user_required
@feature_required(FLAG)
def old_gold_lookup():
    voucher = tenant_query(OldGoldPurchase).filter_by(number=(request.args.get("number") or "").strip()).first()
    if voucher is None:
        return jsonify({"error": "No old gold voucher with that number."}), 404
    if voucher.status == OldGoldStatus.VOID:
        return jsonify({"error": "That voucher was voided."}), 400
    if voucher.balance <= 0:
        return jsonify({"error": "That voucher's value is already used."}), 400
    return jsonify({"number": voucher.number, "balance": float(voucher.balance), "seller": voucher.seller_name})
