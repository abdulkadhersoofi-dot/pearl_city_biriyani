from flask import Blueprint

products_bp = Blueprint(
    "products", __name__, url_prefix="/products", template_folder="../templates/products"
)

from app.products import routes  # noqa: E402,F401
