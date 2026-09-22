"""Thin JSON endpoints for in-page interactivity (product lookup while
building an invoice today; POS cart/checkout will live here from Phase 2).
Kept separate from the server-rendered blueprints so a future SPA frontend
can consume the same endpoints without touching template routes.
"""

from flask import jsonify, request

from app.api import api_bp
from app.auth.decorators import tenant_user_required
from app.models.product import Product
from app.utils.tenant_scope import tenant_query


@api_bp.route("/products/search")
@tenant_user_required
def search_products():
    q = request.args.get("q", "").strip()
    query = tenant_query(Product).filter_by(is_active=True)
    if q:
        query = query.filter(Product.name.ilike(f"%{q}%"))
    products = query.order_by(Product.name).limit(20).all()
    return jsonify(
        [
            {
                "id": p.id,
                "name": p.name,
                "hsn_or_sac_code": p.hsn_or_sac_code,
                "gst_rate": str(p.gst_rate),
                "unit": p.unit,
                "default_price": str(p.default_price),
            }
            for p in products
        ]
    )
