from flask import Blueprint

sell_bp = Blueprint("sell", __name__, url_prefix="/sell")

from app.sell import routes  # noqa: E402,F401
