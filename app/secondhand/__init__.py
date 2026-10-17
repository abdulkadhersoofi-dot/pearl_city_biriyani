"""Second-hand goods (module "margin_scheme") - its own area, in order:
buy -> used stock -> sell -> e-way bills -> register & returns."""

from flask import Blueprint

margin_bp = Blueprint("margin", __name__, url_prefix="/second-hand", template_folder="../templates/secondhand")

from app.secondhand import routes  # noqa: E402,F401
