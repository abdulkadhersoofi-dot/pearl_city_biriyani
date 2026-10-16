"""Restaurant mode (flag restaurant_mode): tables, running orders, KOT,
order types, packing / service charges, sold-out-today. See services.py."""

from flask import Blueprint

restaurant_bp = Blueprint("restaurant", __name__, url_prefix="/restaurant", template_folder="../templates/restaurant")

from app.restaurant import routes  # noqa: E402,F401
