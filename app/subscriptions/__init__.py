from flask import Blueprint

subscriptions_bp = Blueprint("subscriptions", __name__, url_prefix="/admin/subscriptions",
                             template_folder="../templates/subscriptions")

from app.subscriptions import routes  # noqa: E402,F401
