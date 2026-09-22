from flask import Blueprint

notes_bp = Blueprint("notes", __name__, url_prefix="/notes", template_folder="../templates/notes")

from app.notes import routes  # noqa: E402,F401
