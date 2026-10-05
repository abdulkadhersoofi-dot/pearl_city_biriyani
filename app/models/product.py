from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin


class Product(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    hsn_or_sac_code = db.Column(db.String(8), nullable=False)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    unit = db.Column(db.String(20), nullable=False, default="pcs")
    default_price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    image_path = db.Column(db.String(255))  # relative to app/static/, shown on the POS tile
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self):
        return f"<Product {self.id} {self.name}>"
