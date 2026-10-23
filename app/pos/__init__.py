from flask import Blueprint

pos_bp = Blueprint("pos", __name__, url_prefix="/pos", template_folder="../templates/pos")


@pos_bp.before_request
def _counter_is_a_module():
    """The whole counter is the "pos" module: off, every page here is
    gone (404); kept read-only for its records, bills still open and print."""
    from app.utils.features import module_gate

    module_gate("pos")


from app.pos import counter, routes  # noqa: E402,F401
