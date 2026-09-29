from datetime import timedelta

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.auth import auth_bp
from app.auth.forms import ChangePasswordForm, ForgotPasswordForm, LoginForm, ResetPasswordForm
from app.auth.mail import send_otp_email
from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.mixins import utcnow
from app.models.user import PasswordResetOTP, User
from app.utils.audit import record_audit


def _recent_failed_logins(email: str, window_minutes: int) -> int:
    since = utcnow() - timedelta(minutes=window_minutes)
    recent = AuditLog.query.filter(
        AuditLog.action == "login_failed", AuditLog.created_at >= since
    ).all()
    return sum(1 for row in recent if (row.details or {}).get("email") == email)


def _post_login_redirect(user: User):
    if user.must_change_password:
        return redirect(url_for("auth.change_password"))
    if user.is_super_admin:
        return redirect(url_for("tenants.directory"))
    if user.is_client_admin:
        return redirect(url_for("invoicing.list_invoices"))
    return redirect(url_for("pos.terminal"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return _post_login_redirect(current_user)

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        limit = current_app.config["LOGIN_RATE_LIMIT_ATTEMPTS"]
        window = current_app.config["LOGIN_RATE_LIMIT_WINDOW_MINUTES"]
        if _recent_failed_logins(email, window) >= limit:
            flash("Too many attempts. Please try again in a few minutes.", "error")
            return render_template("auth/login.html", form=form)

        user = User.query.filter_by(email=email).first()
        if user and user.is_active and user.check_password(form.password.data):
            login_user(user, remember=False)
            user.last_login_at = utcnow()
            record_audit(user, "login_success", tenant_id=user.tenant_id)
            db.session.commit()
            return _post_login_redirect(user)

        record_audit(None, "login_failed", details={"email": email})
        db.session.commit()
        flash("Incorrect email or password.", "error")

    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout")
@login_required
def logout():
    record_audit(current_user, "logout", tenant_id=current_user.tenant_id)
    db.session.commit()
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter_by(email=email, is_active=True).first()
        # Always show the same message, whether or not the email exists,
        # so the form can't be used to enumerate registered users.
        if user:
            otp, code = PasswordResetOTP.issue(
                user,
                length=current_app.config["OTP_LENGTH"],
                expiry_minutes=current_app.config["OTP_EXPIRY_MINUTES"],
            )
            db.session.commit()
            # Return value deliberately ignored: showing a different message
            # on send failure would let this form be used to tell a
            # registered email apart from an unregistered one.
            send_otp_email(user.email, code)
        flash("If that email is registered, a one-time code has been sent.", "info")
        return redirect(url_for("auth.reset_password", email=email))

    return render_template("auth/forgot_password.html", form=form)


@auth_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    email = request.args.get("email", "").strip().lower()
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=email).first()
        otp = (
            PasswordResetOTP.query.filter_by(user_id=user.id if user else None)
            .order_by(PasswordResetOTP.created_at.desc())
            .first()
            if user
            else None
        )

        max_attempts = current_app.config["OTP_MAX_ATTEMPTS"]
        if not user or not otp or not otp.is_usable(max_attempts):
            flash("That code is invalid or has expired. Request a new one.", "error")
            return redirect(url_for("auth.forgot_password"))

        otp.attempts += 1
        if not otp.verify(form.otp.data.strip()):
            db.session.commit()
            flash("Incorrect code.", "error")
            return render_template("auth/reset_password.html", form=form, email=email)

        otp.consumed_at = utcnow()
        user.set_password(form.new_password.data)
        user.must_change_password = False
        record_audit(user, "password_reset_via_otp", tenant_id=user.tenant_id)
        db.session.commit()
        flash("Password updated. Please sign in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", form=form, email=email)


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("Current password is incorrect.", "error")
        else:
            current_user.set_password(form.new_password.data)
            current_user.must_change_password = False
            record_audit(current_user, "password_changed", tenant_id=current_user.tenant_id)
            db.session.commit()
            flash("Password updated.", "success")
            return _post_login_redirect(current_user)

    return render_template("auth/change_password.html", form=form)
