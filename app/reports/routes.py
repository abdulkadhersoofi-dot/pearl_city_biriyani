from flask import render_template

from app.auth.decorators import roles_required
from app.models.user import UserRole
from app.reports import reports_bp


@reports_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
def index():
    # Sales Register, GSTR-1/3B, Excel/PDF/JSON exports ship in Phase 5.
    return render_template("reports/coming_soon.html")
