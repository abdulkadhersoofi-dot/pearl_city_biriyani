from flask import render_template, request
from flask_login import current_user

from app.auth.decorators import roles_required
from app.loyalty import loyalty_bp
from app.loyalty.points import balance, settings
from app.models.business import LoyaltyEntry
from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.pos_bill import POSBill, POSBillStatus
from app.models.user import UserRole
from app.utils.features import feature_required
from app.utils.tenant_scope import assert_owns, tenant_query


@loyalty_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("loyalty")
def index():
    """Customers with points, most first."""
    from sqlalchemy import func

    from app.extensions import db

    q = (request.args.get("q") or "").strip()
    query = (db.session.query(Customer, func.coalesce(func.sum(LoyaltyEntry.points), 0).label("points"))
             .join(LoyaltyEntry, LoyaltyEntry.customer_id == Customer.id)
             .filter(Customer.tenant_id == current_user.tenant_id, LoyaltyEntry.tenant_id == current_user.tenant_id))
    if q:
        query = query.filter(Customer.name.ilike(f"%{q}%") | Customer.phone.ilike(f"%{q}%"))
    rows = query.group_by(Customer.id).order_by(func.sum(LoyaltyEntry.points).desc()).limit(300).all()
    return render_template("loyalty/index.html", rows=rows, q=q, s=settings(current_user.tenant))


@loyalty_bp.route("/customers/<int:customer_id>")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("loyalty")
def customer(customer_id):
    c = tenant_query(Customer).filter_by(id=customer_id).first_or_404()
    assert_owns(c)
    bills = (tenant_query(POSBill).filter(POSBill.customer_id == c.id, POSBill.status != POSBillStatus.HELD)
             .order_by(POSBill.completed_at.desc()).limit(200).all())
    invoices = tenant_query(Invoice).filter_by(customer_id=c.id).order_by(Invoice.invoice_date.desc()).limit(200).all()
    entries = tenant_query(LoyaltyEntry).filter_by(customer_id=c.id).order_by(LoyaltyEntry.id.desc()).limit(200).all()
    spent = sum(b.grand_total for b in bills if b.status.value == "completed") + sum(
        i.grand_total for i in invoices if i.status.value == "issued")
    return render_template("loyalty/customer.html", c=c, bills=bills, invoices=invoices, entries=entries,
                           points=balance(current_user.tenant_id, c.id), spent=spent)
