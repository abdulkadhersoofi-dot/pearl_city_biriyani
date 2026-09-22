from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.product import Product
from app.models.user import UserRole
from app.products import products_bp
from app.products.forms import ProductForm
from app.utils.tenant_scope import assert_owns, tenant_query


@products_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
def list_products():
    search = request.args.get("q", "").strip()
    query = tenant_query(Product).filter_by(is_active=True)
    if search:
        query = query.filter(Product.name.ilike(f"%{search}%"))
    products = query.order_by(Product.name).all()
    return render_template("products/list.html", products=products, search=search)


@products_bp.route("/new", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def new_product():
    form = ProductForm()
    if form.validate_on_submit():
        product = Product(tenant_id=current_user.tenant_id)
        form.populate_obj(product)
        db.session.add(product)
        db.session.commit()
        flash(f"{product.name} added.", "success")
        return redirect(url_for("products.list_products"))
    return render_template("products/form.html", form=form, product=None)


@products_bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def edit_product(product_id):
    product = tenant_query(Product).filter_by(id=product_id).first_or_404()
    assert_owns(product)
    form = ProductForm(obj=product)
    if form.validate_on_submit():
        form.populate_obj(product)
        db.session.commit()
        flash(f"{product.name} updated.", "success")
        return redirect(url_for("products.list_products"))
    return render_template("products/form.html", form=form, product=product)


@products_bp.route("/<int:product_id>/deactivate", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def deactivate_product(product_id):
    product = tenant_query(Product).filter_by(id=product_id).first_or_404()
    assert_owns(product)
    product.is_active = False
    db.session.commit()
    flash(f"{product.name} removed from active list.", "success")
    return redirect(url_for("products.list_products"))
