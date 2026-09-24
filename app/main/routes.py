from flask import redirect, url_for
from flask_login import current_user
from sqlalchemy import text

from app.extensions import db
from app.main import main_bp


@main_bp.route("/healthz")
def healthz():
    # Unauthenticated, no template render - just enough to prove the app
    # can reach its database. Used by the hosting platform's health check.
    db.session.execute(text("SELECT 1"))
    return {"status": "ok"}, 200


@main_bp.route("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    if current_user.must_change_password:
        return redirect(url_for("auth.change_password"))
    if current_user.is_super_admin:
        return redirect(url_for("tenants.directory"))
    if current_user.is_client_admin:
        return redirect(url_for("invoicing.list_invoices"))
    return redirect(url_for("pos.terminal"))
