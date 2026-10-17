from flask import Blueprint

tenants_bp = Blueprint("tenants", __name__, url_prefix="/admin", template_folder="../templates/tenants")

from app.tenants import packages, routes  # noqa: E402,F401
