from flask import Blueprint

staff_bp = Blueprint("staff", __name__, url_prefix="/staff", template_folder="../templates/staff")

from app.staff import routes  # noqa: E402,F401
