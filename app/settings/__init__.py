from flask import Blueprint

settings_bp = Blueprint("settings", __name__, url_prefix="/settings", template_folder="../templates/settings")

from app.settings import routes  # noqa: E402,F401
