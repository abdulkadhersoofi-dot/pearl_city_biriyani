from flask import Blueprint

# The ARFA Counter app's own screens (the Android app opens /app/; a phone
# browser can too). Phone-sized, for branch logins only.
counterapp_bp = Blueprint("counterapp", __name__, url_prefix="/app")

# Reached before (or without) a signed-in branch: pairing, signing back in,
# the app's own files.
_ALWAYS_OPEN = {"counterapp.pair", "counterapp.resume", "counterapp.ping", "counterapp.unpair",
                "counterapp.manifest", "counterapp.service_worker"}


@counterapp_bp.before_request
def _app_needs_the_counter():
    """The app is the counter on a phone: with the "pos" module off for
    the business, its home says so and every other screen is gone."""
    from flask import abort, render_template, request
    from flask_login import current_user

    from app.utils.features import current_tenant_has

    if request.endpoint in _ALWAYS_OPEN or not current_user.is_authenticated or current_user.tenant_id is None:
        return None
    if current_tenant_has("pos"):
        return None
    if request.endpoint == "counterapp.index":
        return render_template("counterapp/counter_off.html", tenant=current_user.tenant)
    abort(404)

from app.counterapp import routes  # noqa: E402,F401
from app.counterapp import counter_routes  # noqa: E402,F401
