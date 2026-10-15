"""Expenses: rent, salaries, bills and petty cash - for clients with the
Expenses module on."""

from flask import Blueprint

expenses_bp = Blueprint("expenses", __name__, url_prefix="/expenses", template_folder="../templates/expenses")

from app.expenses import routes  # noqa: E402,F401
