from datetime import timedelta

from flask import current_app, flash, redirect, render_template, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.auth import auth_bp
from app.auth.forms import ChangePasswordForm, LoginForm
from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.mixins import utcnow
from app.models.tenant import Tenant
from app.models.user import User
from app.utils.audit import record_audit
from app.utils.billing import advance_billing_cycle, tenant_access_status


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
        return redirect(url_for("tenants.auditors_directory"))
    if user.is_admin_hierarchy:
        return redirect(url_for("tenants.directory"))
    if user.is_client_admin:
        return redirect(url_for("invoicing.list_invoices"))
    return redirect(url_for("pos.terminal"))


def _login_view(login_tenant=None):
    if current_user.is_authenticated:
        return _post_login_redirect(current_user)

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        limit = current_app.config["LOGIN_RATE_LIMIT_ATTEMPTS"]
        window = current_app.config["LOGIN_RATE_LIMIT_WINDOW_MINUTES"]
        if _recent_failed_logins(email, window) >= limit:
            flash("Too many attempts. Please try again in a few minutes.", "error")
            return render_template("auth/login.html", form=form, login_tenant=login_tenant)

        user = User.query.filter_by(email=email).first()
        if user and user.is_active and user.check_password(form.password.data):
            if user.tenant_id is not None:
                status = tenant_access_status(user.tenant)
                if status.blocked:
                    record_audit(
                        user, "login_blocked_tenant_access", tenant_id=user.tenant_id,
                        details={"reason": status.reason},
                    )
                    db.session.commit()
                    flash(status.message, "error")
                    return render_template("auth/login.html", form=form, login_tenant=login_tenant)

            login_user(user, remember=False)
            user.last_login_at = utcnow()
            record_audit(user, "login_success", tenant_id=user.tenant_id)
            db.session.commit()
            return _post_login_redirect(user)

        record_audit(None, "login_failed", details={"email": email})
        db.session.commit()
        flash("Incorrect email or password.", "error")

    return render_template("auth/login.html", form=form, login_tenant=login_tenant)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    return _login_view()


@auth_bp.route("/login/<slug>", methods=["GET", "POST"])
def tenant_login(slug):
    # Each client's own branded entry point (logo + color from Settings),
    # distinct from the firm's generic /auth/login - same form, same
    # credential check, just themed for this one tenant.
    tenant = Tenant.query.filter_by(login_slug=slug, is_active=True).first_or_404()
    return _login_view(login_tenant=tenant)


@auth_bp.route("/logout")
@login_required
def logout():
    record_audit(current_user, "logout", tenant_id=current_user.tenant_id)
    db.session.commit()
    logout_user()
    session.pop("impersonator_id", None)
    return redirect(url_for("auth.login"))


@auth_bp.route("/stop-impersonating")
@login_required
def stop_impersonating():
    # Whoever is signed in right now - the tenant user (or auditor-
    # hierarchy account) an admin switched into via tenants.act_as /
    # tenants.act_as_auditor - switches back to that admin. Needs no role
    # check: only those two routes ever set this session key, and only
    # after verifying the caller was allowed to.
    admin_id = session.pop("impersonator_id", None)
    if not admin_id:
        return redirect(url_for("auth.login"))

    acting_as_tenant_id = current_user.tenant_id
    acting_as_id = current_user.id
    admin = User.query.get(admin_id)
    if not admin or not admin.is_admin_hierarchy:
        logout_user()
        flash("Could not return to the admin console - sign in again.", "error")
        return redirect(url_for("auth.login"))

    record_audit(admin, "impersonation_stopped", tenant_id=acting_as_tenant_id, entity_type="user", entity_id=acting_as_id)
    logout_user()
    login_user(admin, remember=False)
    db.session.commit()
    return _post_login_redirect(admin)


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

            # The billing cycle's anchor date - deliberately not set at
            # onboarding or verification, only here, on the Client Admin's
            # first successful login after changing the initial password
            # the Ultra Admin set for them. Never for Staff/branch logins,
            # and never re-stamped on a later password change.
            if current_user.is_client_admin and current_user.tenant and current_user.tenant.cycle_anchor_date is None:
                tenant = current_user.tenant
                tenant.cycle_anchor_date = utcnow().date()
                tenant.next_billing_due = advance_billing_cycle(tenant, tenant.cycle_anchor_date)

            record_audit(current_user, "password_changed", tenant_id=current_user.tenant_id)
            db.session.commit()
            flash("Password updated.", "success")
            return _post_login_redirect(current_user)

    return render_template("auth/change_password.html", form=form)
