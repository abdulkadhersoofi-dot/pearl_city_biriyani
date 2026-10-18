from flask import Blueprint

# The ARFA Counter app's own screens (the Android app opens /app/; a phone
# browser can too). Phone-sized, for branch logins only.
counterapp_bp = Blueprint("counterapp", __name__, url_prefix="/app")

from app.counterapp import routes  # noqa: E402,F401
from app.counterapp import counter_routes  # noqa: E402,F401
