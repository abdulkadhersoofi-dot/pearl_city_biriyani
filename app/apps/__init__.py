"""The ARFA Android apps on arfa.live: the download page (signed in only),
the update check both apps make, and the short-lived download links.
store.py says where the builds live."""

from flask import Blueprint

apps_bp = Blueprint("apps", __name__, url_prefix="/apps")

from app.apps import routes  # noqa: E402,F401
