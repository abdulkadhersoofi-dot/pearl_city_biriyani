import time
from datetime import timedelta

from flask import abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.auth import auth_bp
from app.auth import security
from app.auth.forms import ChangePasswordForm, LoginForm, TwoFactorCodeForm, TwoFactorDisableForm
from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.mixins import utcnow
from app.models.tenant import Tenant
from app.models.user import User
from app.utils.audit import record_audit
from app.utils.billing import advance_billing_cycle, auditor_access_status, tenant_access_status


def _recent_failed_logins(email: str, window_minutes: int) -> int:
    since = utcnow() - timedelta(minutes=window_minutes)
    recent = AuditLog.query.filter(
        AuditLog.action == "login_failed", AuditLog.created_at >= since
    ).all()
    return sum(1 for row in recent if (row.details or {}).get("email") == email)


def _post_login_redirect(user: User):
    if user.must_change_password:
        return redirect(url_for("auth.change_password"))
    if user.is_admin_hierarchy:
        # Every console login lands on Today (console cleanup, stage 2).
        return redirect(url_for("tenants.today"))
    if user.is_client_admin:
        if user.tenant and user.tenant.has_feature("business_reports"):
            return redirect(url_for("reports.dashboard"))
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
        if security.ip_failures(request.remote_addr, window) >= current_app.config["LOGIN_IP_LIMIT"]:
            flash("Too many failed sign-ins from this network. Please try again in a few minutes.", "error")
            return render_template("auth/login.html", form=form, login_tenant=login_tenant)

        user = User.query.filter_by(email=email).first()
        until = security.locked_until(user) if user else None
        if until is not None:
            record_audit(user, "login_refused_locked", tenant_id=user.tenant_id)
            db.session.commit()
            flash(f"This account is locked after too many wrong passwords. Try again after "
                  f"{until.astimezone().strftime('%H:%M')}, or ask your administrator to reset the password.", "error")
            return render_template("auth/login.html", form=form, login_tenant=login_tenant)

        if user and user.is_active and user.check_password(form.password.data):
            security.clear_failures(user)
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
            elif user.is_auditor or user.is_sub_auditor:
                status = auditor_access_status(user)
                if status.blocked:
                    record_audit(
                        user, "login_blocked_auditor_access", details={"reason": status.reason},
                    )
                    db.session.commit()
                    flash(status.message, "error")
                    return render_template("auth/login.html", form=form, login_tenant=login_tenant)

            if user.has_two_factor or security.two_factor_required(user):
                # The password was right; the second step decides. Nothing
                # is signed in until then.
                session["2fa_user_id"] = user.id
                session["2fa_started"] = time.time()
                session["2fa_tries"] = 0
                db.session.commit()
                return redirect(url_for("auth.two_factor" if user.has_two_factor else "auth.two_factor_setup"))
            return _finish_login(user)

        if user is not None and user.is_active:
            if security.register_failure(user):
                record_audit(user, "account_locked", tenant_id=user.tenant_id,
                             details={"minutes": current_app.config["LOGIN_LOCKOUT_MINUTES"]})
        record_audit(None, "login_failed", details={"email": email})
        db.session.commit()
        flash("Incorrect email or password.", "error")

    return render_template("auth/login.html", form=form, login_tenant=login_tenant)


def _finish_login(user, method: str = "password"):
    login_user(user, remember=False)
    session["last_active"] = time.time()
    user.last_login_at = utcnow()
    record_audit(user, "login_success", tenant_id=user.tenant_id, details={"method": method})
    db.session.commit()
    return _post_login_redirect(user)


# --- two-step sign-in ----------------------------------------------------------------------

TWO_FACTOR_PENDING_SECONDS = 300
TWO_FACTOR_MAX_TRIES = 5


def _pending_user():
    """The account whose password was right and is now on its second step
    (None if there is none, or it took too long)."""
    user_id = session.get("2fa_user_id")
    started = session.get("2fa_started") or 0
    if not user_id or time.time() - started > TWO_FACTOR_PENDING_SECONDS:
        _clear_pending()
        return None
    user = db.session.get(User, user_id)
    return user if user is not None and user.is_active else None


def _clear_pending():
    for key in ("2fa_user_id", "2fa_started", "2fa_tries", "2fa_new_secret"):
        session.pop(key, None)


def _wrong_code(user):
    session["2fa_tries"] = session.get("2fa_tries", 0) + 1
    record_audit(user, "two_factor_failed", tenant_id=user.tenant_id)
    if session["2fa_tries"] >= TWO_FACTOR_MAX_TRIES:
        security.register_failure(user)
        _clear_pending()
        db.session.commit()
        flash("Too many wrong codes - sign in again.", "error")
        return redirect(url_for("auth.login"))
    db.session.commit()
    flash("That code didn't match. Check the time on your phone is right, and try the newest code.", "error")
    return None


@auth_bp.route("/two-factor", methods=["GET", "POST"])
def two_factor():
    user = _pending_user()
    if user is None or not user.has_two_factor:
        return redirect(url_for("auth.login"))
    form = TwoFactorCodeForm()
    if form.validate_on_submit():
        code = form.code.data.strip()
        if security.verify_user_code(user, code):
            _clear_pending()
            return _finish_login(user, "totp")
        if security.use_recovery_code(user, code):
            _clear_pending()
            record_audit(user, "two_factor_recovery_code_used", tenant_id=user.tenant_id,
                         details={"left": len(user.recovery_codes or [])})
            flash(f"Signed in with a recovery code - {len(user.recovery_codes or [])} left. Set up your "
                  "authenticator again under Account security if you lost your phone.", "info")
            return _finish_login(user, "recovery_code")
        response = _wrong_code(user)
        if response is not None:
            return response
    return render_template("auth/two_factor.html", form=form, user=user)


def _setup_user():
    """Whoever is setting up two-step sign-in: the pending login that must
    (admin hierarchy), or a signed-in user turning it on."""
    if current_user.is_authenticated:
        return current_user, False
    user = _pending_user()
    return user, True


@auth_bp.route("/two-factor/setup", methods=["GET", "POST"])
def two_factor_setup():
    user, pending = _setup_user()
    if user is None:
        return redirect(url_for("auth.login"))
    if not security.two_factor_allowed(user):
        abort(403)
    if user.has_two_factor:
        return redirect(url_for("auth.account_security") if not pending else url_for("auth.two_factor"))
    secret = security.unseal_secret(session.get("2fa_new_secret"))
    if secret is None:
        secret = security.new_secret()
        session["2fa_new_secret"] = security.seal_secret(secret)
    form = TwoFactorCodeForm()
    if form.validate_on_submit():
        if security.matching_step(secret, form.code.data) is not None:
            codes = security.enable_two_factor(user, secret)
            session.pop("2fa_new_secret", None)
            record_audit(user, "two_factor_enabled", tenant_id=user.tenant_id)
            if pending:
                _clear_pending()
                login_user(user, remember=False)
                session["last_active"] = time.time()
                user.last_login_at = utcnow()
                record_audit(user, "login_success", tenant_id=user.tenant_id, details={"method": "totp_setup"})
            db.session.commit()
            return render_template("auth/two_factor_codes.html", codes=codes, user=user)
        if pending:
            response = _wrong_code(user)
            if response is not None:
                return response
        else:
            flash("That code didn't match - scan the QR code again and enter the newest code.", "error")
    from app.pos.share import qr_svg_data_uri

    uri = security.provisioning_uri(secret, user.email, current_app.config["FIRM_NAME"])
    return render_template("auth/two_factor_setup.html", form=form, user=user, pending=pending,
                           qr=qr_svg_data_uri(uri, scale=5), secret=secret,
                           required=security.two_factor_required(user))


@auth_bp.route("/security", methods=["GET", "POST"])
@login_required
def account_security():
    if not security.two_factor_allowed(current_user):
        abort(404)
    form = TwoFactorDisableForm()
    codes = None
    if form.validate_on_submit():
        action = request.form.get("action")
        if not current_user.check_password(form.password.data) or not security.verify_user_code(current_user,
                                                                                                 form.code.data):
            db.session.rollback()
            flash("Password or code didn't match.", "error")
        elif action == "disable":
            if security.two_factor_required(current_user):
                flash("Two-step sign-in can't be turned off for this account.", "error")
            else:
                security.disable_two_factor(current_user)
                record_audit(current_user, "two_factor_disabled", tenant_id=current_user.tenant_id)
                db.session.commit()
                flash("Two-step sign-in is off.", "success")
                return redirect(url_for("auth.account_security"))
        elif action == "codes":
            codes, hashes = security.new_recovery_codes()
            current_user.recovery_codes = hashes
            record_audit(current_user, "two_factor_codes_renewed", tenant_id=current_user.tenant_id)
            db.session.commit()
            return render_template("auth/two_factor_codes.html", codes=codes, user=current_user)
    return render_template("auth/security.html", form=form, required=security.two_factor_required(current_user),
                           codes_left=len(current_user.recovery_codes or []))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    return _login_view()


@auth_bp.route("/login/<slug>", methods=["GET", "POST"])
def tenant_login(slug):
    # Each client's own branded entry point (logo + color from Settings),
    # distinct from the firm's generic /auth/login - same form, same
    # credential check, just themed for this one tenant.
    tenant = Tenant.query.filter_by(login_slug=slug, is_active=True).first_or_404()
    from app.utils.entitlements import own_branding

    # The own sign-in link comes with own branding (packages and powers, stage 5).
    if not own_branding(tenant):
        return redirect(url_for("auth.login"))
    return _login_view(login_tenant=tenant)


@auth_bp.route("/logout")
def logout():
    _clear_pending()
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    record_audit(current_user, "logout", tenant_id=current_user.tenant_id)
    db.session.commit()
    logout_user()
    session.pop("impersonator_id", None)
    session.pop("cashier_id", None)
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
    session.pop("cashier_id", None)
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
            # onboarding or verification, only here, on the first
            # successful login after changing the initial password the
            # Ultra Admin set. Never for Staff/branch logins, and never
            # re-stamped on a later password change. A Client Admin's
            # tenant and an Auditor/Sub-Auditor's own account share the
            # exact same billing fields (see app.models.tenant.Tenant /
            # app.models.user.User, app.utils.billing), so the same logic
            # covers both.
            billing_subject = None
            if current_user.is_client_admin and current_user.tenant:
                billing_subject = current_user.tenant
            elif current_user.is_auditor or current_user.is_sub_auditor:
                billing_subject = current_user

            if billing_subject is not None and billing_subject.cycle_anchor_date is None:
                billing_subject.cycle_anchor_date = utcnow().date()
                billing_subject.next_billing_due = advance_billing_cycle(
                    billing_subject, billing_subject.cycle_anchor_date
                )

            record_audit(current_user, "password_changed", tenant_id=current_user.tenant_id)
            db.session.commit()
            flash("Password updated.", "success")
            return _post_login_redirect(current_user)

    return render_template("auth/change_password.html", form=form)
