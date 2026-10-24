from io import BytesIO

from flask import abort, current_app, redirect, send_file, url_for
from flask_login import current_user
from sqlalchemy import text

from app.extensions import db
from app.main import main_bp
from app.models.media import UploadedImage


@main_bp.route("/healthz")
def healthz():
    # Unauthenticated, no template render - proves the app can reach its
    # database (and Redis, where sessions live). Used by the hosting
    # platform's health check and any uptime monitor: 200 when all is
    # well, 503 naming the part that isn't. Never says more than that.
    checks = {}
    try:
        db.session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        db.session.rollback()
        checks["database"] = "error"
    redis_client = current_app.config.get("SESSION_REDIS")
    if redis_client is not None:
        try:
            redis_client.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"
    healthy = all(v == "ok" for v in checks.values())
    body = {"status": "ok" if healthy else "error", "checks": checks,
            "version": current_app.jinja_env.globals.get("app_version")}
    return body, 200 if healthy else 503


def media_sig(image_id: int) -> str:
    """The part of an image's address that can't be guessed: a keyed hash
    of its id. Ids count up, so without it anyone could walk /media/1,
    /media/2 ... and pull every business's logo and product photos."""
    import hashlib
    import hmac

    key = (current_app.config["SECRET_KEY"] + ":media").encode()
    return hmac.new(key, str(int(image_id)).encode(), hashlib.sha256).hexdigest()[:24]


@main_bp.route("/media/<int:image_id>-<sig>")
def media(image_id, sig):
    # Tenant logos and product photos - stored in the database (see
    # app/models/media.py), served from here instead of as a static
    # file. Unauthenticated, same as a static file would be: a tenant's
    # branded sign-in page shows its logo before anyone is signed in.
    # Content is immutable once uploaded (replacing an image creates a
    # new row/id rather than overwriting this one), so this is safe to
    # cache hard. url_for("main.media", image_id=...) adds the signature
    # (app.utils.delivery); an address without the right one is a 404.
    import hmac

    if not hmac.compare_digest(sig, media_sig(image_id)):
        abort(404)
    record = UploadedImage.query.get_or_404(image_id)
    if record.is_private:
        abort(404)
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
    if current_user.is_admin_hierarchy:
        return redirect(url_for("tenants.today"))
    if current_user.is_client_admin:
        if current_user.tenant and current_user.tenant.has_feature("business_reports"):
            return redirect(url_for("reports.dashboard"))
        return redirect(url_for("invoicing.list_invoices"))
    return redirect(branch_landing(current_user))


def branch_landing(user, in_app: bool = False) -> str:
    """Where a branch login starts: its counter - or, with counter billing
    (the "pos" module) off for the business, its first other screen."""
    tenant = user.tenant
    if tenant is not None and tenant.has_feature("pos"):
        return url_for("counterapp.index") if in_app else url_for("pos.terminal")
    if tenant is not None and tenant.has_feature("job_cards"):
        return url_for("jobs.index")
    return url_for("main.no_counter")


@main_bp.route("/counter-off")
def no_counter():
    """A branch login with nothing to open: counter billing is off."""
    from flask import render_template

    if not current_user.is_authenticated or current_user.tenant_id is None:
        return redirect(url_for("main.index"))
    return render_template("no_counter.html")


@main_bp.route("/needs-you")
def needs_you():
    """The bell's list (loaded after the page, so working out low stock or
    expiring batches never slows a page down). Client Admins only."""
    from flask import render_template

    from app.navigation import needs_you as needs_you_items

    if current_user.is_authenticated and current_user.is_super_admin:
        from app.navigation import admin_needs_you

        return render_template("_needs_you.html", items=admin_needs_you())
    if not (current_user.is_authenticated and current_user.is_client_admin and current_user.tenant):
        abort(404)
    return render_template("_needs_you.html", items=needs_you_items(current_user.tenant))
