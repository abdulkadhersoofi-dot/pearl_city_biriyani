from datetime import date
from decimal import Decimal, InvalidOperation

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.auth.decorators import roles_required, tenant_user_required
from app.extensions import db
from app.models.business import CustomerGroup, PriceSlab, Salesman
from app.models.customer import Customer
from app.models.price_list import PriceList
from app.models.product import Product
from app.models.user import UserRole
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.tenant_scope import assert_owns, tenant_query
from app.wholesale import pricing, report, wholesale_bp

ADMIN = UserRole.CLIENT_ADMIN
FLAG = "wholesale"


def _dec(value, label):
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{label} must be a number.")


@wholesale_bp.route("/", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required(FLAG)
def index():
    """Customer groups and salesmen."""
    tenant = current_user.tenant
    if request.method == "POST":
        action = request.form.get("action")
        name = (request.form.get("name") or "").strip()
        if action == "group" and name:
            list_id = request.form.get("price_list_id", type=int)
            if list_id and tenant_query(PriceList).filter_by(id=list_id).first() is None:
                abort(404)
            group = tenant_query(CustomerGroup).filter_by(name=name[:60]).first()
            if group is None:
                group = CustomerGroup(tenant_id=tenant.id, name=name[:60])
                db.session.add(group)
            group.price_list_id = list_id or None
            group.is_active = True
            flash(f"Group {group.name} saved.", "success")
        elif action == "salesman" and name:
            if tenant_query(Salesman).filter_by(name=name[:100]).first() is None:
                db.session.add(Salesman(tenant_id=tenant.id, name=name[:100], phone=(request.form.get("phone") or "").strip()[:20] or None))
                flash(f"Salesman {name} added.", "success")
        elif action == "toggle_salesman":
            s = tenant_query(Salesman).filter_by(id=request.form.get("id", type=int) or 0).first_or_404()
            s.is_active = not s.is_active
        elif action == "toggle_group":
            g = tenant_query(CustomerGroup).filter_by(id=request.form.get("id", type=int) or 0).first_or_404()
            g.is_active = not g.is_active
        else:
            flash("Enter a name.", "error")
        record_audit(current_user, f"wholesale_{action}", tenant_id=tenant.id, details={"name": name})
        db.session.commit()
        return redirect(url_for("wholesale.index"))
    groups = tenant_query(CustomerGroup).order_by(CustomerGroup.name).all()
    members = dict(db.session.query(Customer.customer_group_id, db.func.count(Customer.id))
                   .filter(Customer.tenant_id == tenant.id, Customer.customer_group_id.isnot(None))
                   .group_by(Customer.customer_group_id).all())
    return render_template("wholesale/index.html", groups=groups, members=members,
                           salesmen=tenant_query(Salesman).order_by(Salesman.name).all(),
                           price_lists=tenant_query(PriceList).filter_by(is_active=True).order_by(PriceList.name).all())


@wholesale_bp.route("/slabs/<int:product_id>", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required(FLAG)
def slabs(product_id):
    product = tenant_query(Product).filter_by(id=product_id).first_or_404()
    assert_owns(product)
    if request.method == "POST":
        try:
            if request.form.get("action") == "remove":
                slab = tenant_query(PriceSlab).filter_by(id=request.form.get("slab_id", type=int) or 0, product_id=product.id).first_or_404()
                db.session.delete(slab)
            else:
                qty = _dec(request.form.get("min_qty"), "Quantity")
                price = _dec(request.form.get("price"), "Price")
                if qty <= 1 or price < 0:
                    raise ValueError("A slab starts above 1 unit and its price can't be negative.")
                slab = tenant_query(PriceSlab).filter_by(product_id=product.id, min_qty=qty).first()
                if slab is None:
                    slab = PriceSlab(tenant_id=current_user.tenant_id, product_id=product.id, min_qty=qty, price=price)
                    db.session.add(slab)
                slab.price = price
            record_audit(current_user, "price_slab_changed", tenant_id=current_user.tenant_id, entity_type="product",
                         entity_id=product.id)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("wholesale.slabs", product_id=product.id))
    rows = tenant_query(PriceSlab).filter_by(product_id=product.id).order_by(PriceSlab.min_qty).all()
    return render_template("wholesale/slabs.html", product=product, slabs=rows)


@wholesale_bp.route("/price")
@tenant_user_required
@feature_required(FLAG)
def price():
    """The price one unit costs this customer at this quantity - for the
    invoice form's suggestion."""
    product = tenant_query(Product).filter_by(id=request.args.get("product_id", type=int) or 0).first_or_404()
    customer = None
    if request.args.get("customer_id", type=int):
        customer = tenant_query(Customer).filter_by(id=request.args.get("customer_id", type=int)).first()
    try:
        qty = _dec(request.args.get("qty") or "1", "Quantity")
    except ValueError:
        qty = Decimal("1")
    return jsonify({"rate": float(pricing.unit_price(current_user.tenant_id, product, qty, customer))})


def _range():
    today = date.today()
    try:
        return (date.fromisoformat(request.args.get("start") or today.replace(day=1).isoformat()),
                date.fromisoformat(request.args.get("end") or today.isoformat()))
    except ValueError:
        return today.replace(day=1), today


@wholesale_bp.route("/salesmen-report")
@roles_required(ADMIN)
@feature_required(FLAG)
def salesmen_report():
    start, end = _range()
    rows = report.salesman_report(current_user.tenant, start, end)
    if request.args.get("format") == "xlsx":
        return send_file(report.report_workbook(rows, start, end), as_attachment=True,
                         download_name=f"salesmen_{start}_{end}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return render_template("wholesale/report.html", rows=rows, start=start, end=end)
