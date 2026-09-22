from flask import Blueprint

reports_bp = Blueprint(
    "reports", __name__, url_prefix="/reports", template_folder="../templates/reports"
)

from app.reports import routes  # noqa: E402,F401
