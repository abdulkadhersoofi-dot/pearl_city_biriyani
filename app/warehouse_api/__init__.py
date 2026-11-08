"""The ARFA Warehouse app's API (/api/wh/v1) - JSON only, bearer tokens per
phone, never cookies (routes.py)."""

from flask import Blueprint

from app.extensions import csrf

warehouse_api_bp = Blueprint("warehouse_api", __name__, url_prefix="/api/wh/v1")
csrf.exempt(warehouse_api_bp)

from app.warehouse_api import routes  # noqa: E402,F401
