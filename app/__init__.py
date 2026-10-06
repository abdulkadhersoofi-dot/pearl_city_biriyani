import logging
import os

from flask import Flask
from dotenv import load_dotenv

from app.config import get_config
from app.extensions import csrf, db, login_manager, migrate, sess

load_dotenv()


def create_app(config_object=None):
    app = Flask(__name__)
    app.config.from_object(config_object or get_config())

    _configure_logging(app)
    _init_extensions(app)
    _register_blueprints(app)
    _register_error_handlers(app)
    _register_cli(app)
    _register_context_processors(app)
    _register_request_hooks(app)

    return app


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

    if app.config.get("SESSION_TYPE") == "redis":
        import redis

        app.config["SESSION_REDIS"] = redis.from_url(app.config["REDIS_URL"])
    sess.init_app(app)

    from app.models.user import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))


def _register_blueprints(app: Flask) -> None:
    from app.api import api_bp
    from app.auth import auth_bp
    from app.branches import branches_bp
    from app.customers import customers_bp
    from app.invoicing import invoicing_bp
    from app.notes import notes_bp
    from app.pos import pos_bp
    from app.products import products_bp
    from app.reports import reports_bp
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
    app.register_blueprint(settings_bp)
    app.register_blueprint(branches_bp)
    app.register_blueprint(api_bp)


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
            if brand_tenant.logo_image_id:
                brand_logo_url = url_for("main.media", image_id=brand_tenant.logo_image_id)

            from app.utils.billing import tenant_access_status

            status = tenant_access_status(brand_tenant)
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
        }

    from app.utils.theme import tenant_theme_css

    app.jinja_env.globals["tenant_theme_css"] = tenant_theme_css

    @app.template_filter("state_name")
    def state_name_filter(state_code: str) -> str:
        from app.utils.indian_states import STATE_NAME_BY_CODE

        return STATE_NAME_BY_CODE.get(state_code, state_code)


def _register_request_hooks(app: Flask) -> None:
    @app.before_request
    def enforce_tenant_access():
        from flask import flash, redirect, request, session, url_for
        from flask_login import current_user, logout_user

        if not current_user.is_authenticated or current_user.is_super_admin:
            return None
        if session.get("impersonator_id"):
            # A Super Admin "acting as" this tenant's user is exempt -
            # otherwise the one person who could fix a paused/expired
            # tenant (e.g. by marking it paid) would be locked out of the
            # very screens needed to do that.
            return None
        if current_user.tenant_id is None or request.endpoint in (None, "static", "auth.logout"):
            return None

        tenant = current_user.tenant
        if tenant is None:
            return None

        from app.utils.billing import tenant_access_status

        status = tenant_access_status(tenant)
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
