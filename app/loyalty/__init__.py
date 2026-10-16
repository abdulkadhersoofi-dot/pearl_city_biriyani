"""Loyalty (flag "loyalty"): points earned and spent at the counter
(app.loyalty.points) and each customer's purchase history."""

from flask import Blueprint

loyalty_bp = Blueprint("loyalty", __name__, url_prefix="/loyalty", template_folder="../templates/loyalty")

from app.loyalty import routes  # noqa: E402,F401
