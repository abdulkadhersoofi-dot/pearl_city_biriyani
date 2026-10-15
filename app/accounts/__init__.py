"""Accounts (Phase 6): what customers owe (receivables) and what is owed to
suppliers (payables), the vouchers that settle them, and the cash & bank
book - each module behind its own feature flag."""

from flask import Blueprint

accounts_bp = Blueprint("accounts", __name__, url_prefix="/accounts", template_folder="../templates/accounts")

from app.accounts import routes  # noqa: E402,F401
