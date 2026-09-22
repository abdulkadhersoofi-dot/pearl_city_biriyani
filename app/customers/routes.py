from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.customers import customers_bp
from app.customers.forms import CustomerForm
from app.extensions import db
from app.models.customer import Customer
from app.models.user import UserRole
from app.utils.indian_states import STATE_NAME_BY_CODE
from app.utils.tenant_scope import assert_owns, tenant_query


@customers_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
def list_customers():
    search = request.args.get("q", "").strip()
    query = tenant_query(Customer).filter_by(is_active=True)
    if search:
        query = query.filter(Customer.name.ilike(f"%{search}%"))
    customers = query.order_by(Customer.name).all()
    return render_template("customers/list.html", customers=customers, search=search)


@customers_bp.route("/new", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def new_customer():
    form = CustomerForm()
    if form.validate_on_submit():
        customer = Customer(tenant_id=current_user.tenant_id)
        form.populate_obj(customer)
        customer.state_name = STATE_NAME_BY_CODE.get(customer.state_code, "")
        db.session.add(customer)
        db.session.commit()
        flash(f"{customer.name} added.", "success")
        return redirect(url_for("customers.list_customers"))
    return render_template("customers/form.html", form=form, customer=None)


@customers_bp.route("/<int:customer_id>/edit", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def edit_customer(customer_id):
    customer = tenant_query(Customer).filter_by(id=customer_id).first_or_404()
    assert_owns(customer)
    form = CustomerForm(obj=customer)
    if form.validate_on_submit():
        form.populate_obj(customer)
        customer.state_name = STATE_NAME_BY_CODE.get(customer.state_code, "")
        db.session.commit()
        flash(f"{customer.name} updated.", "success")
        return redirect(url_for("customers.list_customers"))
    return render_template("customers/form.html", form=form, customer=customer)


@customers_bp.route("/<int:customer_id>/deactivate", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def deactivate_customer(customer_id):
    customer = tenant_query(Customer).filter_by(id=customer_id).first_or_404()
    assert_owns(customer)
    customer.is_active = False
    db.session.commit()
    flash(f"{customer.name} removed from active list.", "success")
    return redirect(url_for("customers.list_customers"))
