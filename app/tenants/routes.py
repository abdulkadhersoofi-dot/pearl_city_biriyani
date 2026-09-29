import secrets

from flask import current_app, flash, redirect, render_template, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.auth.mail import send_otp_email
from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.tenant import Gstin, RegistrationType, Tenant
from app.models.user import PasswordResetOTP, User, UserRole
from app.tenants import tenants_bp
from app.tenants.forms import OnboardClientForm
from app.utils.audit import record_audit
from app.utils.indian_states import STATE_NAME_BY_CODE


@tenants_bp.route("/clients")
@roles_required(UserRole.SUPER_ADMIN)
def directory():
    tenants = Tenant.query.order_by(Tenant.legal_name).all()
    return render_template("tenants/directory.html", tenants=tenants)


@tenants_bp.route("/clients/new", methods=["GET", "POST"])
@roles_required(UserRole.SUPER_ADMIN)
def onboard():
    form = OnboardClientForm()
    if form.validate_on_submit():
        existing = User.query.filter_by(email=form.admin_email.data.strip().lower()).first()
        if existing:
            flash("A user with that email already exists.", "error")
            return render_template("tenants/onboard.html", form=form)

        tenant = Tenant(
            legal_name=form.legal_name.data.strip(),
            trade_name=(form.trade_name.data or "").strip() or None,
            registration_type=RegistrationType(form.registration_type.data),
            onboarded_by_id=current_user.id,
        )
        db.session.add(tenant)
        db.session.flush()

        if form.gstin.data:
            gstin = Gstin(
                tenant_id=tenant.id,
                gstin=form.gstin.data.strip().upper(),
                state_code=form.state_code.data,
                state_name=STATE_NAME_BY_CODE.get(form.state_code.data, ""),
                registered_address=form.registered_address.data,
                is_primary=True,
            )
            db.session.add(gstin)

        admin_user = User(
            tenant_id=tenant.id,
            name=form.admin_name.data.strip(),
            email=form.admin_email.data.strip().lower(),
            phone=form.admin_phone.data,
            role=UserRole.CLIENT_ADMIN,
            must_change_password=True,
        )
        admin_user.set_password(secrets.token_urlsafe(32))
        db.session.add(admin_user)
        db.session.flush()

        otp, code = PasswordResetOTP.issue(
            admin_user,
            length=current_app.config["OTP_LENGTH"],
            expiry_minutes=current_app.config["OTP_EXPIRY_MINUTES"],
        )
        record_audit(
            current_user,
            "client_onboarded",
            tenant_id=tenant.id,
            entity_type="tenant",
            entity_id=tenant.id,
        )
        db.session.commit()

        if send_otp_email(admin_user.email, code, purpose="account setup"):
            flash(f"{tenant.legal_name} onboarded. A setup code was sent to {admin_user.email}.", "success")
        else:
            flash(
                f"{tenant.legal_name} onboarded, but the setup email to {admin_user.email} "
                "failed to send. Check the server logs for the code, or use "
                "'Reset password' on the client's detail page to try again.",
                "error",
            )
        return redirect(url_for("tenants.directory"))

    return render_template("tenants/onboard.html", form=form)


@tenants_bp.route("/clients/<int:tenant_id>")
@roles_required(UserRole.SUPER_ADMIN)
def detail(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    record_audit(
        current_user,
        "view_client_data",
        tenant_id=tenant.id,
        entity_type="tenant",
        entity_id=tenant.id,
    )
    db.session.commit()

    invoices = (
        Invoice.query.filter_by(tenant_id=tenant.id)
        .order_by(Invoice.invoice_date.desc())
        .limit(50)
        .all()
    )
    users = User.query.filter_by(tenant_id=tenant.id).all()
    return render_template("tenants/detail.html", tenant=tenant, invoices=invoices, users=users)


@tenants_bp.route("/clients/<int:tenant_id>/deactivate", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def deactivate(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    tenant.is_active = False
    record_audit(
        current_user, "client_deactivated", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id
    )
    db.session.commit()
    flash(f"{tenant.legal_name} deactivated.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/activate", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def activate(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    tenant.is_active = True
    record_audit(
        current_user, "client_activated", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id
    )
    db.session.commit()
    flash(f"{tenant.legal_name} reactivated.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/users/<int:user_id>/reset-password", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def reset_client_password(tenant_id, user_id):
    user = User.query.filter_by(id=user_id, tenant_id=tenant_id).first_or_404()
    otp, code = PasswordResetOTP.issue(
        user,
        length=current_app.config["OTP_LENGTH"],
        expiry_minutes=current_app.config["OTP_EXPIRY_MINUTES"],
    )
    user.must_change_password = True
    record_audit(
        current_user,
        "client_password_reset_initiated_by_firm",
        tenant_id=tenant_id,
        entity_type="user",
        entity_id=user.id,
    )
    db.session.commit()
    if send_otp_email(user.email, code, purpose="password reset"):
        flash(f"A password reset code was sent to {user.email}.", "success")
    else:
        flash(
            f"Could not email {user.email} - check the server logs for the code, or try again.",
            "error",
        )
    return redirect(url_for("tenants.detail", tenant_id=tenant_id))


@tenants_bp.route("/audit-log")
@roles_required(UserRole.SUPER_ADMIN)
def audit_log():
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(200).all()
    return render_template("tenants/audit_log.html", logs=logs)
