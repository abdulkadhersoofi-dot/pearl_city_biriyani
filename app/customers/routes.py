from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.customers import customers_bp
from app.customers.forms import CustomerForm
from app.extensions import db
from app.models.customer import Customer
from app.models.user import UserRole
from app.utils.indian_states import STATE_NAME_BY_CODE


def _customer_form(**kwargs) -> CustomerForm:
    form = CustomerForm(**kwargs)
    if not current_user.tenant.has_feature("receivables"):
        for name in CustomerForm.ACCOUNT_FIELDS:
            delattr(form, name)
    return form


def _save(form, customer):
    form.populate_obj(customer)
    if current_user.tenant.has_feature("wholesale"):
        from app.models.business import CustomerGroup

        group_id = request.form.get("customer_group_id", type=int)
        group = tenant_query(CustomerGroup).filter_by(id=group_id).first() if group_id else None
        customer.customer_group_id = group.id if group else None
    if getattr(form, "opening_balance", None) is not None and customer.opening_balance is None:
        customer.opening_balance = 0
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
    form = _customer_form()
    if form.validate_on_submit():
        customer = Customer(tenant_id=current_user.tenant_id)
        _save(form, customer)
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
    form = _customer_form(obj=customer)
    if form.validate_on_submit():
        _save(form, customer)
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
