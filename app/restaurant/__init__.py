"""Restaurant mode (flag restaurant_mode): tables, running orders, KOT,
order types, packing / service charges, sold-out-today. See services.py."""

from flask import Blueprint

restaurant_bp = Blueprint("restaurant", __name__, url_prefix="/restaurant", template_folder="../templates/restaurant")


@restaurant_bp.before_request
def _tables_need_the_counter():
    """Tables, KOTs and the kitchen work through the counter (POS)."""
    from app.utils.features import module_gate

    module_gate("pos")


from app.restaurant import routes  # noqa: E402,F401
