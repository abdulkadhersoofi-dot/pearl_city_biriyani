import logging
import os
import time

from flask import Flask
from dotenv import load_dotenv

from app.config import get_config
from app.extensions import csrf, db, login_manager, migrate, sess

load_dotenv()


def create_app(config_object=None):
    app = Flask(__name__)
    app.config.from_object(config_object or get_config())

    _set_process_clock(app)
    _configure_logging(app)
    from app.monitoring import init_monitoring

    init_monitoring(app)
    _check_secret_key(app)
    if app.config.get("PROXY_FIX_X_FOR") or app.config.get("PROXY_FIX_X_HOST"):
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=app.config.get("PROXY_FIX_X_FOR", 0), x_proto=1,
                                x_host=app.config.get("PROXY_FIX_X_HOST", 0))
    _init_extensions(app)
    _register_blueprints(app)
    _register_error_handlers(app)
    _register_cli(app)
    _register_context_processors(app)
    _register_request_hooks(app)

    return app


def _check_secret_key(app: Flask) -> None:
    if app.config.get("REQUIRE_SECRET_KEY") and app.config.get("SECRET_KEY") in (None, "", "dev-only-insecure-key"):
        raise RuntimeError("SECRET_KEY is not set - refusing to start in production on the development key.")


def _set_process_clock(app: Flask) -> None:
    """The server's own clock reads Indian time (config TIMEZONE_*), so
    date.today() and datetime.now() are India's date and hour wherever the
    server runs - a cloud server's clock is UTC, which made a bill made
    after midnight carry yesterday's date until 5:30 AM."""
    posix = app.config.get("TIMEZONE_POSIX")
    if not posix or not hasattr(time, "tzset"):
        return
    os.environ["TZ"] = posix
    time.tzset()


def _configure_logging(app: Flask) -> None:
    # A bare `flask run` / gunicorn process has no handler on the root
    # logger by default, so anything logged via logging.getLogger(...)
    # would silently vanish. Give the root logger a handler if nothing
    # else has.
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    if app.config.get("SESSION_TYPE") == "database":
        from app.utils.web_session import DatabaseSessionInterface

        app.session_interface = DatabaseSessionInterface(app.config.get("SESSION_REFRESH_MINUTES", 10))
    else:
        if app.config.get("SESSION_TYPE") == "redis":
            import redis

            app.config["SESSION_REDIS"] = redis.from_url(app.config["REDIS_URL"])
        sess.init_app(app)

    from app.models.user import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))


def _register_blueprints(app: Flask) -> None:
    from app.accounts import accounts_bp
    from app.api import api_bp
    from app.auth import auth_bp
    from app.branches import branches_bp
    from app.counterapp import counterapp_bp
    from app.bulk import bulk_bp
    from app.customers import customers_bp
    from app.einvoice import einvoice_bp
    from app.restaurant import restaurant_bp
    from app.pharmacy import pharmacy_bp
    from app.jewellery import jewellery_bp
    from app.jobs import jobs_bp
    from app.secondhand import margin_bp
    from app.wholesale import wholesale_bp
    from app.loyalty import loyalty_bp
    from app.subscriptions import subscriptions_bp
    from app.expenses import expenses_bp
    from app.inventory import inventory_bp
    from app.invoicing import invoicing_bp
    from app.live import live_bp
    from app.notes import notes_bp
    from app.pos import pos_bp
    from app.products import products_bp
    from app.purchases import purchases_bp
    from app.reports import reports_bp
    from app.sales import sales_bp
    from app.settings import settings_bp
    from app.tenants import tenants_bp
    from app.main import main_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(tenants_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(invoicing_bp)
    app.register_blueprint(pos_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(live_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(branches_bp)
    app.register_blueprint(counterapp_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(bulk_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(purchases_bp)
    app.register_blueprint(expenses_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(sales_bp)
    app.register_blueprint(einvoice_bp)
    app.register_blueprint(restaurant_bp)
    app.register_blueprint(pharmacy_bp)
    app.register_blueprint(jewellery_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(margin_bp)
    app.register_blueprint(wholesale_bp)
    app.register_blueprint(loyalty_bp)
    app.register_blueprint(subscriptions_bp)


def _register_error_handlers(app: Flask) -> None:
    from flask import render_template

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(_e):
        db.session.rollback()
        return render_template("errors/500.html"), 500


def _register_context_processors(app: Flask) -> None:
    @app.context_processor
    def inject_globals():
        from flask import session, url_for
        from flask_login import current_user

        # Platform identity (ARFA) everywhere by default. A signed-in
        # Client Admin/Staff user sees their own tenant's branding instead -
        # nav, page titles, and (passed explicitly) their printed documents.
        brand_name = app.config["FIRM_NAME"]
        brand_logo_url = None
        brand_tenant = None
        billing_notice = None
        if current_user.is_authenticated and not current_user.is_super_admin and current_user.tenant:
            brand_tenant = current_user.tenant
            brand_name = brand_tenant.display_name
            from app.utils.entitlements import brand_logo_id

            if brand_logo_id(brand_tenant):
                brand_logo_url = url_for("main.media", image_id=brand_tenant.logo_image_id)

            from app.utils.billing import tenant_access_status

            status = tenant_access_status(brand_tenant)
            if status.billing_notice:
                billing_notice = status.billing_notice_message
        elif current_user.is_authenticated and (current_user.is_auditor or current_user.is_sub_auditor):
            from app.utils.billing import auditor_access_status

            status = auditor_access_status(current_user)
            if status.billing_notice:
                billing_notice = status.billing_notice_message

        # Set only while a Super Admin is impersonating a tenant user (see
        # tenants.act_as) - current_user IS that tenant user for the
        # duration, so every other part of the app (tenant_query, POS,
        # invoicing...) just works unmodified. This only drives the
        # "Return to admin" banner in base.html.
        impersonator_name = None
        if session.get("impersonator_id"):
            from app.models.user import User

            admin = User.query.get(session["impersonator_id"])
            impersonator_name = admin.name if admin else "Admin"

        return {
            "firm_name": brand_name,
            "brand_logo_url": brand_logo_url,
            "brand_tenant": brand_tenant,
            "platform_name": app.config["FIRM_NAME"],
            "billing_notice": billing_notice,
            "impersonator_name": impersonator_name,
            "read_only_copy": read_only_mode(),
        }

    from app.utils.features import current_tenant_has
    from app.utils.permissions import current_user_can
    from app.utils.theme import tenant_theme_css

    app.jinja_env.globals["tenant_theme_css"] = tenant_theme_css

    # console_can("act_as"): may the signed-in console login use this power
    # (packages and powers, stage 4)? Always true for the Ultra Admin.
    from flask_login import current_user as _console_user

    from app.utils.entitlements import auditor_can as _auditor_can

    app.jinja_env.globals["console_can"] = lambda power: _auditor_can(_console_user, power)
    from app import navigation

    # The grouped side menu, the "New" menu, the search palette and the
    # phone's bottom bar (app/navigation.py), plus the icon sprite.
    app.jinja_env.globals["menu_groups"] = navigation.menu_groups
    app.jinja_env.globals["new_menu"] = navigation.new_menu
    app.jinja_env.globals["palette_entries"] = navigation.palette_entries
    app.jinja_env.globals["bottom_tabs"] = navigation.bottom_tabs
    app.jinja_env.globals["has_menu"] = navigation.has_menu
    app.jinja_env.globals["icon"] = navigation.icon
    # {% if has_feature('categories') %} / {% if can('refund') %} - the
    # template-side counterparts of feature_required/permission_required,
    # so a switched-off module's links and fields never render.
    app.jinja_env.globals["has_feature"] = current_tenant_has
    app.jinja_env.globals["can"] = current_user_can
    app.jinja_env.globals["app_version"] = _app_version()

    def margin_declaration_for(tenant_id):
        from app.models.tenant import Tenant
        from app.utils.margin import declaration_for

        return declaration_for(db.session.get(Tenant, tenant_id))

    # The Rule 32(5) wording printed on a client's margin-scheme invoices.
    app.jinja_env.globals["margin_declaration_for"] = margin_declaration_for
    from app.utils.gst_config import B2CL_INVOICE_VALUE_THRESHOLD

    app.jinja_env.globals["b2cl_threshold"] = B2CL_INVOICE_VALUE_THRESHOLD

    # e-Invoice / e-way bill panels on document pages (None when the flag
    # is off) and the IRN QR (the signed QR string, drawn at view time).
    from app.einvoice.routes import eway_panel, irn_panel

    app.jinja_env.globals["irn_panel"] = irn_panel
    app.jinja_env.globals["eway_panel"] = eway_panel

    def irn_qr(doc, scale=3):
        data = getattr(doc, "qr_code_data", None)
        if not data:
            return None
        if data.startswith("data:image"):
            return data
        from app.pos.share import qr_svg_data_uri

        return qr_svg_data_uri(data, scale=scale)

    app.jinja_env.globals["irn_qr"] = irn_qr

    def order_type_label(value):
        from app.utils.module_settings import ORDER_TYPES

        return ORDER_TYPES.get(value, value or "")

    app.jinja_env.globals["order_type_label"] = order_type_label
    from app.utils.gst_config import JEWELLERY_GST_RATE

    app.jinja_env.globals["jewellery_gst_rate"] = f"{JEWELLERY_GST_RATE.normalize():f}"

    def active_salesmen():
        from flask_login import current_user

        from app.models.business import Salesman

        if not current_user.is_authenticated or not current_user.tenant_id:
            return []
        return Salesman.query.filter_by(tenant_id=current_user.tenant_id, is_active=True).order_by(Salesman.name).all()

    def active_customer_groups():
        from flask_login import current_user

        from app.models.business import CustomerGroup

        if not current_user.is_authenticated or not current_user.tenant_id:
            return []
        return (CustomerGroup.query.filter_by(tenant_id=current_user.tenant_id, is_active=True)
                .order_by(CustomerGroup.name).all())

    def first_run_checklist():
        from flask_login import current_user

        if not current_user.is_authenticated or not current_user.is_client_admin or not current_user.tenant:
            return None
        from app.onboarding import checklist

        return checklist(current_user.tenant)

    app.jinja_env.globals["first_run_checklist"] = first_run_checklist
    app.jinja_env.globals["active_salesmen"] = active_salesmen
    app.jinja_env.globals["active_customer_groups"] = active_customer_groups
    from app.utils.gst_config import EWAY_BILL_CANCEL_HOURS, IRN_CANCEL_HOURS

    app.jinja_env.globals["irn_cancel_hours"] = IRN_CANCEL_HOURS
    app.jinja_env.globals["ewb_cancel_hours"] = EWAY_BILL_CANCEL_HOURS

    @app.template_filter("ist")
    def ist_filter(value, fmt="%d %b %Y %H:%M"):
        """A stored (UTC) timestamp in Indian time - how the IRP and the
        e-way bill system show it."""
        if not value:
            return ""
        from app.einvoice.providers.common import IST

        return value.astimezone(IST).strftime(fmt)

    @app.template_filter("qty")
    def qty_filter(value):
        """1.000 -> "1", 0.750 -> "0.75", 10.00 -> "10" (never "1E+1")."""
        from decimal import Decimal

        if value is None:
            return ""
        text = f"{Decimal(value).normalize():f}"
        return text

    from app.utils.money import inr, inr_compact

    # {{ amount|inr }} -> 1,64,944.00; {{ amount|inr_compact }} -> 1.6L
    app.jinja_env.filters["inr"] = inr
    app.jinja_env.filters["inr_compact"] = inr_compact

    @app.template_filter("state_name")
    def state_name_filter(state_code: str) -> str:
        from app.utils.indian_states import STATE_NAME_BY_CODE

        return STATE_NAME_BY_CODE.get(state_code, state_code)


READ_ONLY_FLAG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy", "render-read-only")
# Signing in and out still work on a read-only copy, so its records can be looked at.
_READ_ONLY_ALLOWED = {"auth.login", "auth.logout", "auth.two_factor", "auth.two_factor_setup", "counterapp.resume",
                      "counterapp.pair", "counterapp.ping"}


def read_only_mode() -> bool:
    """A copy of the app kept only to be looked at - Render, after the move
    to Google Cloud. READ_ONLY_MODE=1 anywhere; on Render (which sets
    RENDER=true) also when deploy/render-read-only is in the code, so a
    commit switches Render to read-only without anyone opening its
    dashboard. Never on Cloud Run, which doesn't set RENDER."""
    if os.environ.get("READ_ONLY_MODE") == "1":
        return True
    return bool(os.environ.get("RENDER")) and os.path.exists(READ_ONLY_FLAG)


def _register_request_hooks(app: Flask) -> None:
    from app.utils.delivery import init_delivery

    init_delivery(app)

    @app.before_request
    def refuse_changes_when_read_only():
        from flask import jsonify, render_template, request

        if request.method in ("GET", "HEAD", "OPTIONS") or not read_only_mode():
            return None
        if request.endpoint in _READ_ONLY_ALLOWED:
            return None
        message = ("This copy of ARFA is read-only - billing has moved to "
                   f"{app.config.get('PUBLIC_URL') or 'the new address'}. Nothing was saved here.")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({"error": message, "read_only": True}), 503
        return render_template("read_only.html", message=message), 503

    @app.before_request
    def enforce_session_security():
        """Idle sign-out, and two-step sign-in for the accounts that must
        have it (app.auth.security)."""
        import time

        from flask import flash, jsonify, redirect, request, session, url_for
        from flask_login import current_user, logout_user

        if request.endpoint in (None, "static", "main.healthz", "pos.service_worker"):
            return None
        if not current_user.is_authenticated:
            return None
        from app.auth import security

        impersonating = bool(session.get("impersonator_id"))
        limit = security.idle_limit_minutes(current_user, impersonating)
        now = time.time()
        last = session.get("last_active")
        if limit and last and now - last > limit * 60:
            from app.utils.audit import record_audit

            record_audit(current_user, "signed_out_idle", tenant_id=current_user.tenant_id,
                         details={"minutes": limit})
            db.session.commit()
            logout_user()
            for key in ("impersonator_id", "cashier_id", "last_active"):
                session.pop(key, None)
            message = f"Signed out after {limit} minutes without activity - sign in again."
            if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
                return jsonify({"error": message, "signed_out": True}), 401
            flash(message, "info")
            return redirect(url_for("auth.login"))
        # Background polls (the counter's offline sync) don't count as
        # someone using the screen.
        # Written at most once a minute - each write is a session save.
        if not request.headers.get("X-Background") and (not last or now - last >= 60):
            session["last_active"] = now

        if (not impersonating and security.two_factor_required(current_user) and not current_user.has_two_factor
                and request.endpoint not in ("auth.two_factor_setup", "auth.logout")):
            return redirect(url_for("auth.two_factor_setup"))
        return None

    @app.before_request
    def enforce_tenant_access():
        from flask import flash, redirect, request, session, url_for
        from flask_login import current_user, logout_user

        if not current_user.is_authenticated or current_user.is_super_admin:
            return None
        if session.get("impersonator_id"):
            # An admin "acting as" this tenant user or Auditor/Sub-Auditor
            # is exempt - otherwise the one person who could fix a paused
            # or overdue account (e.g. by marking it paid) would be locked
            # out of the very screens needed to do that.
            return None
        if request.endpoint in (None, "static", "auth.logout"):
            return None

        if current_user.tenant_id is not None:
            tenant = current_user.tenant
            if tenant is None:
                return None
            from app.utils.billing import tenant_access_status

            status = tenant_access_status(tenant)
        elif current_user.is_auditor or current_user.is_sub_auditor:
            from app.utils.billing import auditor_access_status

            status = auditor_access_status(current_user)
        else:
            return None

        if status.blocked:
            logout_user()
            flash(status.message, "error")
            return redirect(url_for("auth.login"))
        return None


def _register_cli(app: Flask) -> None:
    import click

    @app.cli.command("create-super-admin")
    @click.option("--name", prompt=True)
    @click.option("--email", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_super_admin(name, email, password):
        """Creates the firm's first Super Admin login."""
        from app.models.user import User, UserRole

        if User.query.filter_by(email=email.strip().lower()).first():
            click.echo("A user with that email already exists.")
            return

        user = User(
            name=name.strip(),
            email=email.strip().lower(),
            role=UserRole.SUPER_ADMIN,
            must_change_password=False,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Super Admin '{email}' created.")

    @app.cli.command("owner-summary-send")
    @click.option("--day", default=None, help="YYYY-MM-DD (default: today)")
    def owner_summary_send(day):
        """Emails each client with the Daily owner summary on its day's
        summary (sales, payment modes, counters, top items, low and
        oversold stock). Needs SMTP_HOST / SMTP_FROM; run each evening."""
        from datetime import date as _date

        from app.finance.owner_summary import email_ready, owner_summary, send_email
        from app.models.tenant import Tenant

        if not email_ready():
            click.echo("Email isn't set up (SMTP_HOST / SMTP_FROM) - nothing sent.")
            return
        on = _date.fromisoformat(day) if day else _date.today()
        sent = 0
        for tenant in Tenant.query.filter_by(is_active=True).order_by(Tenant.id).all():
            if tenant.has_feature("daily_summary") and send_email(tenant, owner_summary(tenant, on)):
                sent += 1
        click.echo(f"Day summary emailed for {sent} client(s).")

    @app.cli.command("platform-invoices-generate")
    def platform_invoices_generate():
        """Raises the firm's subscription invoice for every client and
        Auditor office whose renewal is within the billing profile's
        "days before due" and isn't invoiced yet. Run daily from cron."""
        from app.subscriptions.services import raise_due

        made = raise_due(None)
        db.session.commit()
        click.echo(f"{len(made)} subscription invoice(s) raised.")

    @app.cli.command("reset-2fa")
    @click.argument("email")
    def reset_2fa(email):
        """Turns two-step sign-in off for one account (a lost phone with no
        recovery codes left) and opens it if locked. An Ultra Admin /
        Auditor sets it up again at their next sign-in."""
        from app.auth.security import clear_failures, disable_two_factor
        from app.models.user import User
        from app.utils.audit import record_audit

        user = User.query.filter_by(email=email.strip().lower()).first()
        if user is None:
            click.echo("No account with that email.")
            return
        disable_two_factor(user)
        clear_failures(user)
        record_audit(None, "two_factor_reset_from_cli", tenant_id=user.tenant_id, entity_type="user",
                     entity_id=user.id)
        db.session.commit()
        click.echo(f"Two-step sign-in reset for {user.email}.")

    @app.cli.command("expenses-generate-drafts")
    def expenses_generate_drafts():
        """Makes the draft expenses that have fallen due on every client's
        recurring expenses (rent, salary). The Expenses screen does the
        same for its own client whenever it's opened; run this daily from
        cron so drafts are waiting even before anyone looks."""
        from app.expenses.services import generate_due_drafts
        from app.models.tenant import Tenant

        made = 0
        for tenant in Tenant.query.order_by(Tenant.id).all():
            if tenant.has_feature("expenses"):
                made += len(generate_due_drafts(tenant))
                db.session.commit()
        click.echo(f"{made} draft expense(s) made.")

    @app.cli.command("recurring-invoices-generate")
    def recurring_invoices_generate():
        """Drafts every recurring invoice (AMC, rent, catering) that has
        fallen due, for each client with Recurring invoices on. Nothing is
        numbered - the client issues or skips each draft. The Recurring
        invoices screen does the same whenever it's opened; run this daily
        from cron so drafts are waiting before anyone looks."""
        from app.models.tenant import Tenant
        from app.sales.services import generate_due_runs

        made = 0
        for tenant in Tenant.query.order_by(Tenant.id).all():
            if tenant.has_feature("recurring_invoices"):
                made += len(generate_due_runs(tenant))
                db.session.commit()
        click.echo(f"{made} recurring invoice draft(s) made.")

    @app.cli.command("sessions-cleanup")
    def sessions_cleanup():
        """Deletes expired sign-in sessions (SESSION_TYPE=database, the
        web_sessions table). Run daily - "flask nightly" does."""
        from app.utils.web_session import delete_expired

        click.echo(f"{delete_expired()} expired session(s) deleted.")

    @app.cli.command("nightly")
    @click.pass_context
    def nightly(ctx):
        """The jobs that run every night on Google Cloud (Cloud Scheduler ->
        the "nightly" Cloud Run Job): expired sessions, recurring expense
        and invoice drafts. Each step runs even if an earlier one failed."""
        failed = []
        steps = [("sessions-cleanup", sessions_cleanup), ("expenses-generate-drafts", expenses_generate_drafts),
                 ("recurring-invoices-generate", recurring_invoices_generate)]
        for name, command in steps:
            try:
                ctx.invoke(command)
            except Exception as exc:  # noqa: BLE001 - report and carry on with the next step
                db.session.rollback()
                failed.append(name)
                click.echo(f"{name} failed: {exc}", err=True)
        if failed:
            raise click.ClickException("failed: " + ", ".join(failed))

    @app.cli.command("bootstrap-super-admin")
    def bootstrap_super_admin():
        """Non-interactive counterpart to create-super-admin, driven by
        SUPER_ADMIN_EMAIL / SUPER_ADMIN_PASSWORD / SUPER_ADMIN_NAME env
        vars. Meant to run at container startup (see
        deploy/entrypoint.sh) on hosts whose free tier has no shell
        access - Render's is one. Idempotent and safe on every restart:
        does nothing if the env vars aren't set, or if that email
        already has an account.
        """
        from app.models.user import User, UserRole

        email = os.environ.get("SUPER_ADMIN_EMAIL", "").strip().lower()
        password = os.environ.get("SUPER_ADMIN_PASSWORD", "")
        name = os.environ.get("SUPER_ADMIN_NAME", "Firm Admin").strip()

        if not email or not password:
            click.echo("SUPER_ADMIN_EMAIL/SUPER_ADMIN_PASSWORD not set - skipping bootstrap.")
            return

        if User.query.filter_by(email=email).first():
            click.echo(f"'{email}' already exists - skipping bootstrap.")
            return

        user = User(
            name=name,
            email=email,
            role=UserRole.SUPER_ADMIN,
            must_change_password=False,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Super Admin '{email}' created via bootstrap.")

    @app.cli.command("backup")
    @click.option("--dir", "out_dir", default=lambda: os.environ.get("BACKUP_DIR", "backups"), show_default="BACKUP_DIR or ./backups")
    @click.option("--keep", default=lambda: int(os.environ.get("BACKUP_KEEP", "14")), show_default="BACKUP_KEEP or 14", type=int)
    def backup(out_dir, keep):
        """pg_dump the database to a gzipped, timestamped file, keeping the
        newest --keep backups in --dir (see app/utils/backup.py)."""
        from app.utils.backup import BackupError, run_backup

        try:
            path = run_backup(app.config["SQLALCHEMY_DATABASE_URI"], out_dir, keep)
        except BackupError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"Backup written: {path} ({path.stat().st_size} bytes)")


def _app_version() -> str:
    """Short commit id of the code that is running, shown on the POS
    screen so "which version is this server on?" has a one-glance
    answer. Cloud Build sets APP_VERSION, Render RENDER_GIT_COMMIT;
    elsewhere ask git; else dev."""
    import subprocess

    for key in ("APP_VERSION", "RENDER_GIT_COMMIT", "GIT_COMMIT", "SOURCE_VERSION"):
        value = os.environ.get(key)
        if value:
            return value[:7]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3, cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "dev"
