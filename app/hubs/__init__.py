from flask import Blueprint

hubs_bp = Blueprint("hubs", __name__)

from app.hubs import routes  # noqa: E402,F401
