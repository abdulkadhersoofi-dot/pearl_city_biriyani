from datetime import date

from flask import jsonify, render_template
from flask_login import current_user

from app.auth.decorators import roles_required
from app.live import live_bp
from app.live.metrics import branch_leaderboard, item_sales_today
from app.models.user import UserRole
from app.reports.stock import stock_report_data


def _snapshot(tenant):
    today = date.today()
    branches = branch_leaderboard(tenant, today)
    items = item_sales_today(tenant, today)
    stock = stock_report_data(tenant, today, today)

    return {
        "generated_at": date.today().isoformat(),
        "branches": [
            {
                "id": b.branch_id,
                "name": b.branch_name,
                "revenue": float(b.revenue),
                "order_count": b.order_count,
            }
            for b in branches
        ],
        "items": [
            {
                "name": i.product_name,
                "qty": float(i.qty),
                "revenue": float(i.revenue),
                "share_percent": round(i.share_percent, 1),
            }
            for i in items
        ],
        "stock": [
            {
                "name": r.product_name,
                "unit": r.unit,
                "allocated": float(r.allocated),
                "sold": float(r.sold),
                "remaining": float(r.remaining),
            }
            for r in stock["rows"]
        ],
    }


@live_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
def dashboard():
    snapshot = _snapshot(current_user.tenant)
    return render_template("live/dashboard.html", snapshot=snapshot)


@live_bp.route("/data.json")
@roles_required(UserRole.CLIENT_ADMIN)
def data_json():
    return jsonify(_snapshot(current_user.tenant))
