"""Inventory: the stock ledger (ledger.py), the hooks every selling and
buying document calls (hooks.py), stock documents (services.py), and the
Stock screens (routes.py) - the screens only for clients with the
Inventory module on."""

from flask import Blueprint

inventory_bp = Blueprint("inventory", __name__, url_prefix="/inventory", template_folder="../templates/inventory")

from app.inventory import routes  # noqa: E402,F401
