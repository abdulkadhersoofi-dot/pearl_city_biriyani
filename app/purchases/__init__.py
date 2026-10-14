"""Purchases: suppliers, purchase bills (with ITC), purchase returns,
purchase orders and GSTR-2B matching - for clients with the Purchases
module on."""

from flask import Blueprint

purchases_bp = Blueprint("purchases", __name__, url_prefix="/purchases", template_folder="../templates/purchases")

from app.purchases import routes  # noqa: E402,F401
