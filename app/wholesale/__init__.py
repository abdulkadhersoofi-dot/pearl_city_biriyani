"""Wholesale (flag "wholesale"): customer groups with their own price
list, quantity-slab prices, salesmen and their sales & collections."""

from flask import Blueprint

wholesale_bp = Blueprint("wholesale", __name__, url_prefix="/wholesale", template_folder="../templates/wholesale")

from app.wholesale import routes  # noqa: E402,F401
