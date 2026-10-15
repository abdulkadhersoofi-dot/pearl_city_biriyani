"""Margin-scheme lots (Phase 5, module "margin_scheme").

Every unit of a second-hand item sold under Rule 32(5) is tied to the
purchase it came from - the purchase bill line (one serial/IMEI unit of
it when serial numbers are tracked) - because its purchase price is half
of the margin. One MarginAllocation row per (sale line, purchase lot): it
is the margin register. Rows are never deleted: a void or a refund marks
them released, a sales return counts the units that came back."""

from app.extensions import db
from app.models.mixins import TenantScopedMixin, utcnow


class MarginAllocation(db.Model, TenantScopedMixin):
    __tablename__ = "margin_allocations"

    id = db.Column(db.Integer, primary_key=True)
    # The sale: "pos_bill" or "invoice", its id and line id.
    source_type = db.Column(db.String(20), nullable=False)
    source_id = db.Column(db.Integer, nullable=False)
    source_line_id = db.Column(db.Integer, nullable=False)
    document_number = db.Column(db.String(64))
    sold_on = db.Column(db.Date, nullable=False)
    # The lot it came from.
    purchase_line_id = db.Column(db.Integer, db.ForeignKey("purchase_bill_lines.id", name="fk_margin_allocations_purchase_line"),
                                 nullable=False, index=True)
    serial_id = db.Column(db.Integer, db.ForeignKey("product_serials.id", name="fk_margin_allocations_serial"))
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_margin_allocations_product"), nullable=False)
    qty = db.Column(db.Numeric(12, 3), nullable=False)
    # Units of `qty` that came back on a sales return.
    returned_qty = db.Column(db.Numeric(12, 3), nullable=False, default=0, server_default="0")
    purchase_value = db.Column(db.Numeric(12, 2), nullable=False)
    sale_value = db.Column(db.Numeric(12, 2), nullable=False)
    margin = db.Column(db.Numeric(12, 2), nullable=False)  # never below 0
    taxable_value = db.Column(db.Numeric(12, 2), nullable=False)
    cgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    sgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    igst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    released_at = db.Column(db.DateTime(timezone=True))  # the sale was voided / refunded

    purchase_line = db.relationship("PurchaseBillLine")
    serial = db.relationship("ProductSerial")
    product = db.relationship("Product")

    __table_args__ = (
        db.Index("ix_margin_allocations_source", "source_type", "source_id"),
    )

    @property
    def tax(self):
        return self.cgst_amount + self.sgst_amount + self.igst_amount

    def __repr__(self):
        return f"<MarginAllocation {self.source_type}#{self.source_id} lot {self.purchase_line_id} x{self.qty}>"
