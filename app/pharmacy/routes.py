from datetime import date

from flask import jsonify, render_template, request, send_file
from flask_login import current_user

from app.auth.decorators import roles_required, tenant_user_required
from app.models.product import Product
from app.models.user import UserRole
from app.pharmacy import pharmacy_bp, services
from app.utils.features import feature_required
from app.utils.gst_config import DRUG_SCHEDULES
from app.utils.tenant_scope import tenant_query


@pharmacy_bp.route("/substitutes/<int:product_id>")
@tenant_user_required
@feature_required("pharmacy")
def substitutes(product_id):
    product = tenant_query(Product).filter_by(id=product_id).first_or_404()
    if not product.composition:
        return jsonify({"items": [], "message": f"{product.name} has no composition on file - add it on the item."})
    return jsonify({"items": services.substitutes(current_user.tenant, product)})


def _range():
    today = date.today()
    try:
        start = date.fromisoformat(request.args.get("start") or today.replace(day=1).isoformat())
        end = date.fromisoformat(request.args.get("end") or today.isoformat())
    except ValueError:
        start, end = today.replace(day=1), today
    schedules = [s for s in request.args.getlist("schedule") if s in DRUG_SCHEDULES] or ["H1", "X"]
    return start, end, schedules


@pharmacy_bp.route("/register")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("pharmacy")
def register():
    start, end, schedules = _range()
    rows = services.register_rows(current_user.tenant, start, end, schedules)
    return render_template("pharmacy/register.html", rows=rows, start=start, end=end, schedules=schedules,
                           all_schedules=DRUG_SCHEDULES)


@pharmacy_bp.route("/register.xlsx")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("pharmacy")
def register_xlsx():
    start, end, schedules = _range()
    buf = services.register_workbook(services.register_rows(current_user.tenant, start, end, schedules), start, end)
    return send_file(buf, as_attachment=True, download_name=f"drug_register_{start}_{end}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
