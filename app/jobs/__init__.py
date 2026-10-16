"""Job cards (flag "job_cards") and the electronics warranty check (flag
"electronics")."""

from flask import Blueprint

jobs_bp = Blueprint("jobs", __name__, url_prefix="/jobs", template_folder="../templates/jobs")

from app.jobs import routes  # noqa: E402,F401
