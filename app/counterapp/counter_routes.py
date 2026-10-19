"""The ARFA Counter app's Held, Today and Kitchen (counter app, stage 4).

  /app/held                 GET this counter's held bills; POST hold the cart
  /app/held/<id>/resume     POST - the held bill's lines, and it leaves the list
  /app/held/<id>/discard    POST
  /app/today                today at this counter (needs "view reports")
  /app/kitchen              the kitchen screen (restaurant; online)
  /app/kitchen/state        its tickets, polled
  /app/kitchen/<id>/ready   POST {ready: true|false}
"""

from flask import abort, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.counterapp import counter as counter_service
from app.counterapp import counterapp_bp
from app.counterapp.routes import _is_branch, _screen
from app.extensions import db
from app.utils.audit import record_audit
from app.utils.features import current_tenant_has
from app.utils.permissions import user_can


def _branch_json():
    if not _is_branch():
        abort(401)


# --- held bills ------------------------------------------------------------------------------

@counterapp_bp.route("/held", methods=["GET", "POST"])
def held():
    _branch_json()
    tenant = current_user.tenant
    if request.method == "POST":
        from app.pos.services import POSValidationError

        try:
            bill = counter_service.hold(tenant, current_user, request.get_json(silent=True) or {})
        except (counter_service.CounterError, POSValidationError) as exc:
            db.session.rollback()
            return jsonify({"error": exc.message}), 400
        record_audit(current_user, "pos_bill_held", tenant_id=tenant.id, entity_type="pos_bill", entity_id=bill.id,
                     details={"from": "app"})
        db.session.commit()
        return jsonify({"held": counter_service.held_json(bill)})
    return jsonify({"held": [counter_service.held_json(b) for b in counter_service.held_bills(tenant, current_user)]})


@counterapp_bp.route("/held/<int:bill_id>/<action>", methods=["POST"])
def held_action(bill_id, action):
    _branch_json()
    if action not in ("resume", "discard"):
        abort(404)
    tenant = current_user.tenant
    try:
        bill = counter_service.own_held(tenant, current_user, bill_id)
    except counter_service.CounterError as exc:
        return jsonify({"error": exc.message}), 404
    data = counter_service.held_json(bill)
    if action == "discard":
        from app.pos.services import discard_held_bill

        discard_held_bill(bill)
        record_audit(current_user, "pos_held_discarded", tenant_id=tenant.id, entity_type="pos_bill", entity_id=bill_id,
                     details={"from": "app"})
    else:
        # Back on the phone's bill: it leaves the held list, as at the counter.
        db.session.delete(bill)
    db.session.commit()
    return jsonify({"held": data})


# --- today -----------------------------------------------------------------------------------

@counterapp_bp.route("/today")
def today():
    if not _is_branch():
        return redirect(url_for("counterapp.index"))
    if not user_can(current_user, "view_reports"):
        abort(403)
    from app.pos.sessions import day_close_on

    tenant = current_user.tenant
    return _screen(render_template(
        "counterapp/today.html", tenant=tenant, t=counter_service.today(tenant, current_user),
        day_close=day_close_on(tenant), tables_on=current_tenant_has("restaurant_mode")))


# --- the kitchen screen ----------------------------------------------------------------------

def _kitchen_on():
    if not current_tenant_has("restaurant_mode"):
        abort(404)


@counterapp_bp.route("/kitchen")
def kitchen():
    if not _is_branch():
        return redirect(url_for("counterapp.index"))
    _kitchen_on()
    from flask_wtf.csrf import generate_csrf

    return render_template("counterapp/kitchen.html", tenant=current_user.tenant, csrf=generate_csrf(), in_app=True,
                           state_url=url_for("counterapp.kitchen_state"),
                           ready_url=url_for("counterapp.kitchen_ready", ticket_id=0),
                           back_url=url_for("counterapp.index"), back_label="Back to the app's home")


@counterapp_bp.route("/kitchen/state")
def kitchen_state():
    _branch_json()
    _kitchen_on()
    from app.restaurant.services import kitchen_state as state

    return jsonify(state(current_user.tenant, current_user))


@counterapp_bp.route("/kitchen/<int:ticket_id>/ready", methods=["POST"])
def kitchen_ready(ticket_id):
    _branch_json()
    _kitchen_on()
    from app.restaurant.services import RestaurantError, mark_ready

    ready = (request.get_json(silent=True) or {}).get("ready", True) is not False
    try:
        ticket = mark_ready(current_user.tenant, current_user, ticket_id, ready)
    except RestaurantError as exc:
        return jsonify({"error": exc.message}), 404
    record_audit(current_user, "kot_ready" if ready else "kot_not_ready", tenant_id=current_user.tenant_id,
                 entity_type="kitchen_ticket", entity_id=ticket.id, details={"kot_no": ticket.kot_no})
    db.session.commit()
    return jsonify({"ok": True})
