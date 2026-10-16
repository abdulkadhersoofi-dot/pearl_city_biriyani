from datetime import date
from decimal import Decimal
from io import BytesIO

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.auth.decorators import roles_required, tenant_user_required
from app.extensions import db
from app.models.business import DiningTable, KitchenTicket
from app.models.price_list import PriceList
from app.models.product import Product
from app.models.user import UserRole
from app.pos.receipt import DEFAULT_PAGE_FORMAT, THERMAL_WIDTHS_IN, _render
from app.pos.services import POSValidationError
from app.restaurant import restaurant_bp, services
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.module_settings import ORDER_TYPES, dec, module_settings, save_module_settings
from app.utils.tenant_scope import assert_owns, tenant_query

FLAG = "restaurant_mode"


def _table(table_id):
    table = tenant_query(DiningTable).filter_by(id=table_id).first_or_404()
    assert_owns(table)
    return table


@restaurant_bp.route("/")
@tenant_user_required
@feature_required(FLAG)
def tables():
    tenant = current_user.tenant
    tiles = services.overview(tenant)
    return render_template("restaurant/tables.html", tiles=tiles, order_types=services.order_types(tenant),
                           free=[t["table"] for t in tiles if t["order"] is None],
                           busy=[t["table"] for t in tiles if t["order"] is not None])


@restaurant_bp.route("/tables/<int:table_id>/kot", methods=["POST"])
@tenant_user_required
@feature_required(FLAG)
def send_kot(table_id):
    from app.pos.routes import _cart_from_request

    table = _table(table_id)
    tenant = current_user.tenant
    try:
        cart = _cart_from_request()
        kot = services.send_kot(tenant, current_user, table, cart)
        record_audit(current_user, "kot_sent", tenant_id=tenant.id, entity_type="dining_table", entity_id=table.id,
                     details={"kot_no": kot.kot_no if kot else None})
        db.session.commit()
    except (POSValidationError, services.RestaurantError) as exc:
        db.session.rollback()
        return jsonify({"error": exc.message}), 400
    return jsonify({"kot_id": kot.id if kot else None, "kot_no": kot.kot_no if kot else None,
                    "pdf": url_for("restaurant.kot_pdf", kot_id=kot.id) if kot else None,
                    "tables_url": url_for("restaurant.tables")})


@restaurant_bp.route("/kot/<int:kot_id>.pdf")
@tenant_user_required
@feature_required(FLAG)
def kot_pdf(kot_id):
    kot = tenant_query(KitchenTicket).filter_by(id=kot_id).first_or_404()
    assert_owns(kot)
    fmt = request.args.get("format") or current_user.tenant.default_receipt_format
    width = THERMAL_WIDTHS_IN.get(fmt, THERMAL_WIDTHS_IN[DEFAULT_PAGE_FORMAT])
    height = 4 + len(kot.items) * 0.7
    pdf = _render("restaurant/kot_print.html", current_user.tenant,
                  {"kot": kot, "order_label": ORDER_TYPES.get(kot.order_type, kot.order_type), "lines": _kot_lines(kot)},
                  f"{width}in {height:.2f}in")
    return send_file(BytesIO(pdf), mimetype="application/pdf", download_name=f"KOT-{kot.kot_no}.pdf")


def _kot_lines(kot) -> list[dict]:
    out = []
    for item in kot.items:
        qty = Decimal(str(item["qty"]))
        out.append({"qty": f"{abs(qty).normalize():f}", "name": item["name"], "note": item.get("note"), "cancel": qty < 0})
    return out


@restaurant_bp.route("/tables/<int:table_id>/move", methods=["POST"])
@tenant_user_required
@feature_required(FLAG)
def move(table_id):
    table = _table(table_id)
    target = _table(request.form.get("to_table_id", type=int) or 0)
    action = request.form.get("action", "move")
    try:
        if action == "merge":
            services.merge_tables(current_user.tenant, current_user, table, target)
            msg = f"Table {table.name} merged into {target.name}."
        else:
            services.move_table(current_user.tenant, table, target)
            msg = f"Order moved from table {table.name} to {target.name}."
        record_audit(current_user, f"table_{action}", tenant_id=current_user.tenant_id, entity_type="dining_table",
                     entity_id=table.id, details={"to": target.id})
        db.session.commit()
        flash(msg, "success")
    except services.RestaurantError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("restaurant.tables"))


@restaurant_bp.route("/tables/<int:table_id>/cancel", methods=["POST"])
@tenant_user_required
@feature_required(FLAG)
def cancel(table_id):
    table = _table(table_id)
    try:
        services.cancel_order(current_user.tenant, table, request.form.get("reason", ""))
        record_audit(current_user, "table_order_cancelled", tenant_id=current_user.tenant_id,
                     entity_type="dining_table", entity_id=table.id, details={"reason": request.form.get("reason")})
        db.session.commit()
        flash(f"Order on table {table.name} cancelled.", "success")
    except services.RestaurantError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("restaurant.tables"))


@restaurant_bp.route("/menu", methods=["GET", "POST"])
@tenant_user_required
@feature_required(FLAG)
def menu():
    """Today's menu: mark an item sold out (back on sale tomorrow by itself)."""
    if request.method == "POST":
        product = tenant_query(Product).filter_by(id=request.form.get("product_id", type=int) or 0).first_or_404()
        product.sold_out_on = None if request.form.get("available") else date.today()
        record_audit(current_user, "sold_out_toggled", tenant_id=current_user.tenant_id, entity_type="product",
                     entity_id=product.id, details={"sold_out": product.sold_out_on is not None})
        db.session.commit()
        return redirect(url_for("restaurant.menu", q=request.args.get("q", "")))
    q = (request.args.get("q") or "").strip()
    query = tenant_query(Product).filter_by(is_active=True).filter(Product.has_variants.is_(False))
    if q:
        query = query.filter(Product.name.ilike(f"%{q}%"))
    items = query.order_by(Product.name).all()
    return render_template("restaurant/menu.html", items=items, q=q, today=date.today())


@restaurant_bp.route("/setup", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required(FLAG)
def setup():
    tenant = current_user.tenant
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_table":
            names = [n.strip()[:30] for n in (request.form.get("names") or "").replace("\n", ",").split(",") if n.strip()]
            existing = {t.name for t in tenant_query(DiningTable)}
            added = 0
            for n in names:
                if n not in existing:
                    db.session.add(DiningTable(tenant_id=tenant.id, name=n, section=(request.form.get("section") or "").strip()[:40] or None,
                                               sort_order=len(existing) + added))
                    existing.add(n)
                    added += 1
            db.session.commit()
            flash(f"{added} table{'s' if added != 1 else ''} added.", "success")
        elif action == "toggle_table":
            table = _table(request.form.get("table_id", type=int) or 0)
            if table.is_active and services.running_order(tenant.id, table.id) is not None:
                flash(f"Table {table.name} has a running order - bill or cancel it first.", "error")
            else:
                table.is_active = not table.is_active
                db.session.commit()
        elif action == "settings":
            f = request.form
            chosen = [k for k in ORDER_TYPES if f.get(f"type_{k}")] or ["dine_in"]
            lists = {pl.id for pl in tenant_query(PriceList).filter_by(is_active=True)}
            price_lists = {}
            for k in ORDER_TYPES:
                pid = f.get(f"list_{k}", type=int)
                price_lists[k] = pid if pid in lists else None
            values = {
                "order_types": chosen, "price_lists": price_lists,
                "packing_charge": str(max(dec(f.get("packing_charge")), dec(0))),
                "packing_per": "item" if f.get("packing_per") == "item" else "order",
                "packing_on": [k for k in ORDER_TYPES if f.get(f"packing_{k}")],
                "service_charge_percent": str(min(max(dec(f.get("service_charge_percent")), dec(0)), dec(25))),
                "charge_gst_rate": str(dec(f.get("charge_gst_rate"), "5")),
            }
            save_module_settings(tenant, "restaurant", values)
            record_audit(current_user, "restaurant_settings_saved", tenant_id=tenant.id, details=values)
            db.session.commit()
            flash("Restaurant settings saved.", "success")
        return redirect(url_for("restaurant.setup"))
    return render_template("restaurant/setup.html", tables=tenant_query(DiningTable).order_by(DiningTable.sort_order, DiningTable.name).all(),
                           s=module_settings(tenant, "restaurant"), order_types=ORDER_TYPES,
                           price_lists=tenant_query(PriceList).filter_by(is_active=True).order_by(PriceList.name).all())
