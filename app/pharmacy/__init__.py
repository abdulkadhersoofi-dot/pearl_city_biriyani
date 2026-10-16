"""Pharmacy (flag "pharmacy"): substitutes by composition and the
Schedule H / H1 / X sale register. The counter rules (prescription
required, batch & expiry) live in app.pos.business and the item form."""

from flask import Blueprint

pharmacy_bp = Blueprint("pharmacy", __name__, url_prefix="/pharmacy", template_folder="../templates/pharmacy")

from app.pharmacy import routes  # noqa: E402,F401
