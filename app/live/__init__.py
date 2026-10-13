from flask import Blueprint

live_bp = Blueprint("live", __name__, url_prefix="/live", template_folder="../templates/live")

from app.live import routes  # noqa: E402,F401
