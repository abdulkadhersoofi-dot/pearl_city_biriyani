from flask import Blueprint

invoicing_bp = Blueprint(
    "invoicing", __name__, url_prefix="/invoices", template_folder="../templates/invoicing"
)

from app.invoicing import routes  # noqa: E402,F401
