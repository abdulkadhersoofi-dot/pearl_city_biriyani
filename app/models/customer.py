from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin


class Customer(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    gstin = db.Column(db.String(15))
    address_line1 = db.Column(db.String(255))
    address_line2 = db.Column(db.String(255))
    city = db.Column(db.String(100))
    state_code = db.Column(db.String(2))
    state_name = db.Column(db.String(64))
    pincode = db.Column(db.String(10))
    phone = db.Column(db.String(20))
    email = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # Customer accounts (feature "receivables"): what they owed when the
    # client started keeping accounts here, and how much credit to allow.
    opening_balance = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    opening_balance_date = db.Column(db.Date)
    credit_limit = db.Column(db.Numeric(12, 2))
    credit_days = db.Column(db.Integer)
    # Wholesale: the group whose price list this customer buys at.
    customer_group_id = db.Column(db.Integer, db.ForeignKey("customer_groups.id", name="fk_customers_group"))

    customer_group = db.relationship("CustomerGroup")

    def __repr__(self):
        return f"<Customer {self.id} {self.name}>"
