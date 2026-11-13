"""The hub page behind each door of the simple menu (app/doors.py), and the
switch between the simple and the classic menu.

A hub only links to screens that already exist; it saves nothing. Home's hub
is for a business without the dashboard (which is Home otherwise)."""

from flask import abort, make_response, redirect, render_template, request, url_for
from flask_login import current_user

from app import doors
from app.auth.decorators import roles_required
from app.hubs import hubs_bp
from app.models.user import UserRole
from app.navigation import MENU_COOKIE, MENU_STYLES


def _hub(key):
    if key == "buy" and not doors._buy_is_on():
        abort(404)
    return render_template("hubs/hub.html", hub=doors.hub(key, current_user.tenant),
                           doors=doors.menu_links() if key == "home" else [])


@hubs_bp.route("/home")
@roles_required(UserRole.CLIENT_ADMIN)
def home():
    return _hub("home")


@hubs_bp.route("/sell")
@roles_required(UserRole.CLIENT_ADMIN)
def sell():
    return _hub("sell")


@hubs_bp.route("/buy")
@roles_required(UserRole.CLIENT_ADMIN)
def buy():
    return _hub("buy")


@hubs_bp.route("/stock")
@roles_required(UserRole.CLIENT_ADMIN)
def stock():
    return _hub("stock")


@hubs_bp.route("/money")
@roles_required(UserRole.CLIENT_ADMIN)
def money():
    return _hub("money")


@hubs_bp.route("/setup")
@roles_required(UserRole.CLIENT_ADMIN)
def setup():
    return _hub("setup")


@hubs_bp.route("/moved")
@roles_required(UserRole.CLIENT_ADMIN)
def moved():
    """What moved where: every entry of the classic menu and its new door."""
    return render_template("hubs/moved.html", rows=doors.what_moved())


@hubs_bp.route("/find.json")
@roles_required(UserRole.CLIENT_ADMIN)
def find():
    """Records for the search box (Ctrl K): app/search.py."""
    from flask import jsonify, request

    from app import search

    return jsonify({"records": search.records(current_user.tenant, request.args.get("q", ""))})


@hubs_bp.route("/menu/<style>", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def choose_menu(style):
    """Simple or classic menu, remembered on this device for a year."""
    if style not in MENU_STYLES:
        abort(404)
    back = request.form.get("next") or ""
    # Only ever back to a page of this site.
    target = back if back.startswith("/") and not back.startswith("//") else url_for("main.index")
    response = make_response(redirect(target))
    response.set_cookie(MENU_COOKIE, style, max_age=365 * 24 * 3600, samesite="Lax", httponly=True,
                        secure=request.is_secure)
    return response
