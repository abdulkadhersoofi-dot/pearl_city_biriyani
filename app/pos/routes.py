from flask import render_template

from app.auth.decorators import tenant_user_required
from app.pos import pos_bp


@pos_bp.route("/")
@tenant_user_required
def terminal():
    # POS touchscreen checkout ships in Phase 2. Staff/Cashier logins land
    # here today with a placeholder so their role is already wired up.
    return render_template("pos/coming_soon.html")
