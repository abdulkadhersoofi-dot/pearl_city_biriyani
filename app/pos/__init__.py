from flask import Blueprint

pos_bp = Blueprint("pos", __name__, url_prefix="/pos", template_folder="../templates/pos")

from app.pos import routes  # noqa: E402,F401
