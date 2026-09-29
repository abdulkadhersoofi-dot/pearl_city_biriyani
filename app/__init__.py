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

    return app


def _configure_logging(app: Flask) -> None:
    # A bare `flask run` / gunicorn process has no handler on the root
    # logger by default, so anything logged via logging.getLogger(...)
    # (e.g. the console-mail OTP fallback) would silently vanish. Give the
    # root logger a handler if nothing else has, and always surface OTPs
    # from the console-mail backend regardless of the app's overall log
    # level - that fallback exists specifically to be visible.
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")
    logging.getLogger("gstapp.mail").setLevel(logging.INFO)


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
    from app.customers import customers_bp
    from app.invoicing import invoicing_bp
    from app.notes import notes_bp
    from app.pos import pos_bp
    from app.products import products_bp
    from app.reports import reports_bp
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
        return {"firm_name": app.config["FIRM_NAME"]}

    @app.template_filter("state_name")
    def state_name_filter(state_code: str) -> str:
        from app.utils.indian_states import STATE_NAME_BY_CODE

        return STATE_NAME_BY_CODE.get(state_code, state_code)


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
