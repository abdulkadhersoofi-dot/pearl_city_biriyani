"""Platform billing screens: the Ultra Admin raises, sends and marks paid
the firm's subscription invoices; each client (its Client Admin) and each
Auditor office sees only its own."""

from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.platform import PlatformInvoice, PlatformInvoiceStatus, PlatformProfile
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.subscriptions import services, subscriptions_bp
from app.utils.audit import record_audit
from app.utils.indian_states import STATE_NAME_BY_CODE


def _subject(kind: str, subject_id: int):
    if kind == "tenant":
        return Tenant.query.get_or_404(subject_id)
    if kind == "auditor":
        return User.query.filter(User.id == subject_id, User.role.in_(services.AUDITOR_ROLES)).first_or_404()
    abort(404)


def _back(subject):
    if isinstance(subject, Tenant):
        return redirect(url_for("tenants.detail", tenant_id=subject.id))
    return redirect(url_for("tenants.auditor_detail", auditor_id=subject.id))


@subscriptions_bp.route("/")
@roles_required(UserRole.SUPER_ADMIN)
def index():
    query = PlatformInvoice.query
    kind, subject_id = request.args.get("kind"), request.args.get("subject_id", type=int)
    subject = None
    if kind and subject_id:
        subject = _subject(kind, subject_id)
        query = query.filter(PlatformInvoice.tenant_id == subject.id if kind == "tenant"
                             else PlatformInvoice.auditor_id == subject.id)
    status = request.args.get("status")
    if status in {s.value for s in PlatformInvoiceStatus}:
        query = query.filter(PlatformInvoice.status == PlatformInvoiceStatus(status))
    invoices = query.order_by(PlatformInvoice.invoice_date.desc(), PlatformInvoice.id.desc()).limit(200).all()
    unpaid = sum((i.grand_total for i in PlatformInvoice.query.filter_by(status=PlatformInvoiceStatus.ISSUED)),
                 Decimal("0"))
    return render_template("subscriptions/index.html", invoices=invoices, due=services.due_subjects(date.today()),
                           profile=services.profile(), subject=subject, status=status, unpaid=unpaid)


@subscriptions_bp.route("/raise", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def raise_due():
    try:
        made = services.raise_due(current_user)
    except services.SubscriptionError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("subscriptions.index"))
    db.session.commit()
    flash(f"{len(made)} invoice{'s' if len(made) != 1 else ''} raised." if made else "Nothing is due yet.", "success")
    return redirect(url_for("subscriptions.index"))


@subscriptions_bp.route("/raise/<kind>/<int:subject_id>", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def raise_one(kind, subject_id):
    subject = _subject(kind, subject_id)
    try:
        invoice = services.create_invoice(subject, current_user)
    except services.SubscriptionError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return _back(subject)
    db.session.commit()
    flash(f"{invoice.number} raised for Rs. {invoice.grand_total:.2f}.", "success")
    return redirect(url_for("subscriptions.view", invoice_id=invoice.id))


@subscriptions_bp.route("/fee/<kind>/<int:subject_id>", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def set_fee(kind, subject_id):
    subject = _subject(kind, subject_id)
    raw = (request.form.get("fee") or "").strip()
    try:
        fee = Decimal(raw).quantize(Decimal("0.01")) if raw else None
    except InvalidOperation:
        fee = Decimal("-1")
    if fee is not None and fee < 0:
        flash("Enter the fee as an amount in rupees.", "error")
        return _back(subject)
    subject.subscription_fee = fee
    record_audit(current_user, "subscription_fee_set", tenant_id=subject.id if kind == "tenant" else None,
                 entity_type=kind, entity_id=subject.id, details={"fee": str(fee) if fee is not None else None})
    db.session.commit()
    flash("Subscription fee saved." if fee else "No subscription invoice will be raised.", "success")
    return _back(subject)


@subscriptions_bp.route("/profile", methods=["GET", "POST"])
@roles_required(UserRole.SUPER_ADMIN)
def profile():
    prof = services.profile_or_default()
    errors = []
    if request.method == "POST":
        f = request.form
        gstin = (f.get("gstin") or "").strip().upper()
        state = (f.get("state_code") or "").strip()
        prefix = (f.get("invoice_prefix") or "SUB").strip().upper()
        try:
            rate = Decimal(f.get("gst_rate") or "0")
        except InvalidOperation:
            rate = Decimal("-1")
        if not (f.get("legal_name") or "").strip():
            errors.append("Enter the firm's legal name.")
        if state not in STATE_NAME_BY_CODE:
            errors.append("Pick the firm's state.")
        if gstin:
            import re

            from app.utils.gst import GSTIN_REGEX

            if not re.match(GSTIN_REGEX, gstin):
                errors.append("That GSTIN doesn't look right.")
            elif gstin[:2] != state:
                errors.append("The GSTIN's first two digits must be the state's code.")
        if not prefix.isalnum() or len(prefix) > 4:
            errors.append("The invoice prefix is up to 4 letters or digits (the number must stay within 16 characters).")
        if rate < 0 or rate > 28:
            errors.append("Enter the GST rate as a % between 0 and 28.")
        if not errors:
            if prof.id is None:
                db.session.add(prof)
            prof.legal_name = f.get("legal_name").strip()
            prof.gstin = gstin or None
            prof.state_code = state
            prof.address = (f.get("address") or "").strip() or None
            prof.email = (f.get("email") or "").strip() or None
            prof.phone = (f.get("phone") or "").strip() or None
            prof.upi_vpa = (f.get("upi_vpa") or "").strip() or None
            prof.upi_payee_name = (f.get("upi_payee_name") or "").strip() or None
            prof.sac_code = (f.get("sac_code") or "").strip() or services.gst_config.PLATFORM_SERVICE_SAC
            prof.gst_rate = rate
            prof.days_before_due = max(0, min(f.get("days_before_due", type=int) or 0, 31))
            prof.invoice_prefix = prefix
            record_audit(current_user, "platform_profile_saved", details={"gstin": prof.gstin, "state": state})
            db.session.commit()
            flash("Billing profile saved.", "success")
            return redirect(url_for("subscriptions.index"))
    return render_template("subscriptions/profile.html", profile=prof, errors=errors, states=STATE_NAME_BY_CODE)


def _invoice_for_viewer(invoice_id: int) -> PlatformInvoice:
    """The Ultra Admin sees every invoice; a Client Admin only their
    client's, an Auditor / Sub-Auditor only their own office's."""
    invoice = PlatformInvoice.query.get_or_404(invoice_id)
    if current_user.is_super_admin:
        return invoice
    if current_user.is_client_admin and invoice.tenant_id and invoice.tenant_id == current_user.tenant_id:
        return invoice
    if (current_user.is_auditor or current_user.is_sub_auditor) and invoice.auditor_id == current_user.id:
        return invoice
    abort(404)


@subscriptions_bp.route("/<int:invoice_id>")
@login_required
def view(invoice_id):
    invoice = _invoice_for_viewer(invoice_id)
    link = services.upi_link(invoice)
    qr = None
    if link:
        from app.pos.share import qr_svg_data_uri

        qr = qr_svg_data_uri(link, scale=5)
    return render_template("subscriptions/view.html", invoice=invoice, upi_link=link, upi_qr=qr)


@subscriptions_bp.route("/<int:invoice_id>.pdf")
@login_required
def pdf(invoice_id):
    invoice = _invoice_for_viewer(invoice_id)
    return send_file(BytesIO(services.render_pdf(invoice)), mimetype="application/pdf", as_attachment=False,
                     download_name=f"{invoice.number.replace('/', '-')}.pdf")


@subscriptions_bp.route("/<int:invoice_id>/paid", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def mark_paid(invoice_id):
    invoice = PlatformInvoice.query.get_or_404(invoice_id)
    try:
        result = services.mark_paid(invoice, current_user, request.form.get("reference"))
    except services.SubscriptionError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("subscriptions.view", invoice_id=invoice.id))
    db.session.commit()
    subject = services.subject_of(invoice)
    if result == "already_paid":
        flash(f"{invoice.number} was already marked paid. Nothing more to do.", "info")
    elif result == "advanced":
        flash(f"{invoice.number} paid - {invoice.subscriber_name}'s next renewal is due "
              f"{subject.next_billing_due.strftime('%d %b %Y')}.", "success")
    else:
        flash(f"{invoice.number} paid. {invoice.subscriber_name}'s renewal had already moved on, so it stays "
              f"{subject.next_billing_due.strftime('%d %b %Y') if subject.next_billing_due else '-'}.", "success")
    return redirect(url_for("subscriptions.view", invoice_id=invoice.id))


@subscriptions_bp.route("/<int:invoice_id>/void", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def void(invoice_id):
    invoice = PlatformInvoice.query.get_or_404(invoice_id)
    try:
        services.void(invoice, current_user, request.form.get("reason"))
    except services.SubscriptionError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("subscriptions.view", invoice_id=invoice.id))
    db.session.commit()
    flash(f"{invoice.number} voided - it stays on record with the reason.", "success")
    return redirect(url_for("subscriptions.view", invoice_id=invoice.id))


@subscriptions_bp.route("/mine")
@login_required
def mine():
    """A client's (Client Admin) or an Auditor office's own subscription invoices."""
    if current_user.is_client_admin:
        query = PlatformInvoice.query.filter(PlatformInvoice.tenant_id == current_user.tenant_id)
    elif current_user.is_auditor or current_user.is_sub_auditor:
        query = PlatformInvoice.query.filter(PlatformInvoice.auditor_id == current_user.id)
    else:
        abort(404)
    invoices = query.order_by(PlatformInvoice.invoice_date.desc(), PlatformInvoice.id.desc()).all()
    if current_user.is_client_admin:
        from app.settings.sections import section_info, section_menu

        return render_template("subscriptions/mine.html", invoices=invoices, section=section_info("subscription"),
                               sections=section_menu("subscription"))
    return render_template("subscriptions/mine.html", invoices=invoices)
