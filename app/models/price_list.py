from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin


class PriceList(db.Model, TenantScopedMixin, TimestampMixin):
    """A named set of selling prices - "Wholesale", "Swiggy", "MG Road
    branch" - used instead of an item's normal sale price (feature
    "multi_price_list"). An item without a price in the list sells at its
    normal price; the item's own GST-inclusive setting always applies."""

    __tablename__ = "price_lists"
    __table_args__ = (db.UniqueConstraint("tenant_id", "name", name="uq_price_lists_tenant_name"),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())

    items = db.relationship("PriceListItem", backref="price_list", cascade="all, delete-orphan")

    def price_map(self) -> dict:
        return {item.product_id: item.price for item in self.items}

    def __repr__(self):
        return f"<PriceList {self.id} {self.name}>"


class PriceListItem(db.Model, TenantScopedMixin):
    __tablename__ = "price_list_items"
    __table_args__ = (
        db.UniqueConstraint("price_list_id", "product_id", name="uq_price_list_items_list_product"),
    )

    id = db.Column(db.Integer, primary_key=True)
    price_list_id = db.Column(
        db.Integer, db.ForeignKey("price_lists.id", name="fk_price_list_items_list"), nullable=False, index=True
    )
    product_id = db.Column(
        db.Integer, db.ForeignKey("products.id", name="fk_price_list_items_product"), nullable=False, index=True
    )
    price = db.Column(db.Numeric(12, 2), nullable=False)

    product = db.relationship("Product")
