"""Jewellery (flag "jewellery"): metal rates, old gold vouchers, the job-
work making-charge item. Pricing itself is app.utils.jewellery."""

from flask import Blueprint

jewellery_bp = Blueprint("jewellery", __name__, url_prefix="/jewellery", template_folder="../templates/jewellery")

from app.jewellery import routes  # noqa: E402,F401
