from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin


class Category(db.Model, TenantScopedMixin, TimestampMixin):
    """A top-level menu grouping (e.g. "Starters", "Biriyani", "Drinks").
    Only meaningful when Tenant.category_wise_items is on - see
    app.settings and app.pos.routes.terminal for where that toggle is
    read. Categorizing an item is always optional even then; an
    uncategorized item just shows under "Uncategorized" in the POS."""

    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    sub_categories = db.relationship(
        "SubCategory", backref="category", cascade="all, delete-orphan", lazy="dynamic"
    )

    def __repr__(self):
        return f"<Category {self.id} {self.name}>"


class SubCategory(db.Model, TenantScopedMixin, TimestampMixin):
    """A grouping within one Category (e.g. "Chicken" under "Biriyani").
    Always belongs to exactly one Category - there's no such thing as a
    sub-category that isn't nested under a category."""

    __tablename__ = "sub_categories"

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self):
        return f"<SubCategory {self.id} {self.name}>"
