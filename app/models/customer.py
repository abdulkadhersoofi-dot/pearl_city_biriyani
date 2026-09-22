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

    def __repr__(self):
        return f"<Customer {self.id} {self.name}>"
