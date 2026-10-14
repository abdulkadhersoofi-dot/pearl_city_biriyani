from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin


class Product(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "products"
    __table_args__ = (
        # Postgres treats NULLs as distinct, so items with no SKU/barcode
        # never collide - only two items claiming the same code do.
        db.UniqueConstraint("tenant_id", "sku", name="uq_products_tenant_sku"),
        db.UniqueConstraint("tenant_id", "barcode", name="uq_products_tenant_barcode"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    # HSN for goods, SAC (chapter 99) for a service - validated by
    # app.utils.gst.hsn_error against the client's AATO bracket.
    hsn_or_sac_code = db.Column(db.String(8), nullable=False)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    # A GST portal Unit Quantity Code (app.utils.gst_config.UQC_CODES) for
    # anything created/edited since the UQC list was introduced; older
    # items may still carry their original free-text unit ("plate") until
    # next edited, so printed bills don't change underneath anyone.
    unit = db.Column(db.String(20), nullable=False, default="PCS")
    # Optional bigger pack unit: 1 alt_unit = alt_unit_factor x unit
    # (e.g. 1 BOX = 12 PCS). Both set or both empty.
    alt_unit = db.Column(db.String(20))
    alt_unit_factor = db.Column(db.Numeric(12, 3))

    # Normal selling price. Whether it already includes GST is
    # price_includes_tax - app.utils.gst.compute_line backs the taxable
    # value out of an inclusive price.
    sale_price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    price_includes_tax = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    wholesale_price = db.Column(db.Numeric(12, 2))
    # Maximum retail price - always tax-inclusive by law; the item form
    # refuses a sale price that would exceed it.
    mrp = db.Column(db.Numeric(12, 2))
    # Last price paid to a supplier, and the average cost of stock on
    # hand. Entered by hand for now; purchase bills keep them current once
    # Purchases exists.
    purchase_rate = db.Column(db.Numeric(12, 2))
    cost_price = db.Column(db.Numeric(12, 2))

    sku = db.Column(db.String(64))
    barcode = db.Column(db.String(64))
    min_stock_level = db.Column(db.Numeric(12, 3))
    # A service (repair labour, consultation) - SAC code, never stocked.
    is_service = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    # Today nothing blocks a sale on stock (branch counters are a tracking
    # signal only), so this defaults on; the Inventory module is what will
    # honour an item switched off here.
    allow_negative_stock = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())

    # Stored in uploaded_images (database row, not a disk path) so it
    # survives a container restart/redeploy - see app/models/media.py.
    image_id = db.Column(db.Integer, db.ForeignKey("uploaded_images.id"))
    # Pinned to the top of the POS favourites row (feature "favourites"),
    # ahead of the automatic best sellers.
    is_favourite = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # Optional even when the tenant has the "categories" feature on - an
    # item with neither just shows under "Uncategorized" in the POS
    # grouping. sub_category_id is only meaningful together with a
    # matching category_id; the product form enforces that pairing, the
    # column itself doesn't.
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    sub_category_id = db.Column(db.Integer, db.ForeignKey("sub_categories.id"))

    category = db.relationship("Category")
    sub_category = db.relationship("SubCategory")

    @property
    def sells_loose(self) -> bool:
        """Weighed/measured (KGS, LTR, MTR...) - billed in decimal quantities."""
        from app.utils.gst_config import LOOSE_QTY_UQC

        return (self.unit or "").upper() in LOOSE_QTY_UQC

    def __repr__(self):
        return f"<Product {self.id} {self.name}>"
