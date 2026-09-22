from flask import render_template

from app.auth.decorators import roles_required
from app.models.user import UserRole
from app.notes import notes_bp


@notes_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
def list_notes():
    # Credit/Debit notes ship in Phase 3.
    return render_template("notes/coming_soon.html")
