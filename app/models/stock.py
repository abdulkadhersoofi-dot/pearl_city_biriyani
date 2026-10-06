from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin


class BranchStockAllocation(db.Model, TenantScopedMixin, TimestampMixin):
    """One "delivery" of stock from the kitchen (Client Admin) to a branch
    (a Staff login) - e.g. "sent 100 Biriyani, 50 Soda to MG Road branch".
    Append-only: a second delivery the same day is a second row, not an
    overwrite, so the day's running total and the delivery history are
    both just a sum/list of these rows. "Resets daily" falls out of only
    ever summing rows for today's date - see app/pos/stock.py.
    """

    __tablename__ = "branch_stock_allocations"

    id = db.Column(db.Integer, primary_key=True)
    branch_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    qty = db.Column(db.Numeric(12, 3), nullable=False)
    allocated_date = db.Column(db.Date, nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    branch_user = db.relationship("User", foreign_keys=[branch_user_id])
    product = db.relationship("Product")
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    def __repr__(self):
        return f"<BranchStockAllocation branch={self.branch_user_id} product={self.product_id} qty={self.qty} date={self.allocated_date}>"
