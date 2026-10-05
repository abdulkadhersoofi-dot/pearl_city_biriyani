from io import BytesIO

from flask import redirect, send_file, url_for
from flask_login import current_user
from sqlalchemy import text

from app.extensions import db
from app.main import main_bp
from app.models.media import UploadedImage


@main_bp.route("/healthz")
def healthz():
    # Unauthenticated, no template render - just enough to prove the app
    # can reach its database. Used by the hosting platform's health check.
    db.session.execute(text("SELECT 1"))
    return {"status": "ok"}, 200


@main_bp.route("/media/<int:image_id>")
def media(image_id):
    # Tenant logos and product photos - stored in the database (see
    # app/models/media.py), served from here instead of as a static
    # file. Unauthenticated, same as a static file would be: a tenant's
    # branded sign-in page shows its logo before anyone is signed in.
    # Content is immutable once uploaded (replacing an image creates a
    # new row/id rather than overwriting this one), so this is safe to
    # cache hard.
    record = UploadedImage.query.get_or_404(image_id)
    return send_file(
        BytesIO(record.data),
        mimetype=record.content_type,
        as_attachment=False,
        conditional=True,
        etag=str(record.id),
        last_modified=record.created_at,
        max_age=31536000,
    )


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
