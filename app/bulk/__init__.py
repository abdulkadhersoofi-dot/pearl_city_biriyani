from flask import Blueprint

bulk_bp = Blueprint("bulk", __name__, url_prefix="/bulk", template_folder="../templates/bulk")

from app.bulk import routes  # noqa: E402,F401
