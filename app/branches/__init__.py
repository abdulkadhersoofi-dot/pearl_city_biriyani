from flask import Blueprint

branches_bp = Blueprint("branches", __name__, url_prefix="/branches", template_folder="../templates/branches")

from app.branches import routes  # noqa: E402,F401
