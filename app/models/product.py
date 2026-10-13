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
    # Stored in uploaded_images (database row, not a disk path) so it
    # survives a container restart/redeploy - see app/models/media.py.
    image_id = db.Column(db.Integer, db.ForeignKey("uploaded_images.id"))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # Optional even when the tenant has category-wise mode on
    # (Tenant.category_wise_items) - an item with neither just shows
    # under "Uncategorized" in the POS grouping. sub_category_id is only
    # meaningful together with a matching category_id; the product form
    # enforces that pairing, the column itself doesn't.
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    sub_category_id = db.Column(db.Integer, db.ForeignKey("sub_categories.id"))

    category = db.relationship("Category")
    sub_category = db.relationship("SubCategory")

    def __repr__(self):
        return f"<Product {self.id} {self.name}>"
