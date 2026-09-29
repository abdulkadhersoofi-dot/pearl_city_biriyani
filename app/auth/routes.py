from datetime import timedelta

from flask import current_app, flash, redirect, render_template, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.auth import auth_bp
from app.auth.forms import ChangePasswordForm, LoginForm
from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.mixins import utcnow
from app.models.user import User
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
