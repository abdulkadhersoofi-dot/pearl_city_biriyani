"""Stock: where it is (StockLocation), every change to it (StockMovement -
append-only), and the documents that move it by hand (adjustments,
transfers). Batches and serial numbers hang off movements for items
that track them. See app/inventory/ledger.py for the rules."""

import enum

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin, utcnow


class LocationKind(str, enum.Enum):
    STORE = "store"
    GODOWN = "godown"
    BRANCH = "branch"


LOCATION_KIND_LABELS = {LocationKind.STORE: "Store", LocationKind.GODOWN: "Godown", LocationKind.BRANCH: "Branch"}


class StockLocation(db.Model, TenantScopedMixin, TimestampMixin):
    """A place stock is kept - the main store, a godown, or a branch. A
    branch login (Staff) has its own location, created the first time it
    is needed, so a branch's sales come out of the branch's stock."""

    __tablename__ = "stock_locations"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "name", name="uq_stock_locations_tenant_name"),
        db.UniqueConstraint("tenant_id", "branch_user_id", name="uq_stock_locations_tenant_branch"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    kind = db.Column(db.Enum(LocationKind, name="location_kind"), nullable=False, default=LocationKind.STORE)
    branch_user_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_stock_locations_branch_user"))
    is_default = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    address = db.Column(db.Text)

    branch_user = db.relationship("User")

    def __repr__(self):
        return f"<StockLocation {self.id} {self.name}>"


class MovementType(str, enum.Enum):
    OPENING = "opening"
    PURCHASE = "purchase"
    PURCHASE_RETURN = "purchase_return"
    SALE = "sale"
    SALE_RETURN = "sale_return"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    ADJUSTMENT = "adjustment"
    DAMAGE = "damage"
    WASTAGE = "wastage"
    FOUND = "found"


MOVEMENT_TYPE_LABELS = {
    MovementType.OPENING: "Opening stock",
    MovementType.PURCHASE: "Purchase",
    MovementType.PURCHASE_RETURN: "Purchase return",
    MovementType.SALE: "Sale",
    MovementType.SALE_RETURN: "Sale return",
    MovementType.TRANSFER_IN: "Transfer in",
    MovementType.TRANSFER_OUT: "Transfer out",
    MovementType.ADJUSTMENT: "Adjustment",
    MovementType.DAMAGE: "Damage",
    MovementType.WASTAGE: "Wastage",
    MovementType.FOUND: "Found",
}


class StockMovement(db.Model, TenantScopedMixin):
    """One change to one item's stock at one place: + in, - out. Never
    edited or deleted - a voided document is undone by an opposite entry
    pointing back at the one it cancels (reversal_of_id). Stock on hand is
    simply the sum of qty."""

    __tablename__ = "stock_movements"
    __table_args__ = (
        db.Index("ix_stock_movements_tenant_product_location", "tenant_id", "product_id", "location_id"),
        db.Index("ix_stock_movements_source", "tenant_id", "source_type", "source_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_stock_movements_product"), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("stock_locations.id", name="fk_stock_movements_location"), nullable=False)
    qty = db.Column(db.Numeric(12, 3), nullable=False)
    movement_type = db.Column(db.Enum(MovementType, name="movement_type"), nullable=False)
    # The document that caused it: "pos_bill", "pos_return", "invoice",
    # "purchase_bill", "purchase_return", "adjustment", "transfer".
    source_type = db.Column(db.String(30), nullable=False)
    source_id = db.Column(db.Integer, nullable=False)
    source_line_id = db.Column(db.Integer)
    # Cost of one unit at the time (weighted average for outgoing stock,
    # landed cost for a purchase) - what the stock value and, later,
    # the cost of goods sold are worked out from.
    unit_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0, server_default="0")
    batch_id = db.Column(db.Integer, db.ForeignKey("product_batches.id", name="fk_stock_movements_batch"))
    serial_id = db.Column(db.Integer, db.ForeignKey("product_serials.id", name="fk_stock_movements_serial"))
    moved_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_stock_movements_created_by"))
    note = db.Column(db.String(255))
    reversal_of_id = db.Column(db.Integer, db.ForeignKey("stock_movements.id", name="fk_stock_movements_reversal_of"))

    product = db.relationship("Product")
    location = db.relationship("StockLocation")
    batch = db.relationship("ProductBatch")
    serial = db.relationship("ProductSerial")


class ProductBatch(db.Model, TenantScopedMixin, TimestampMixin):
    """A batch/lot of an item (feature "batch_expiry") - its own expiry
    date and MRP. Stock per batch is the sum of movements carrying it."""

    __tablename__ = "product_batches"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "product_id", "batch_no", name="uq_product_batches_tenant_product_no"),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_product_batches_product"), nullable=False, index=True)
    batch_no = db.Column(db.String(50), nullable=False)
    mfg_date = db.Column(db.Date)
    expiry_date = db.Column(db.Date, index=True)
    mrp = db.Column(db.Numeric(12, 2))

    product = db.relationship("Product")


class SerialStatus(str, enum.Enum):
    IN_STOCK = "in_stock"
    SOLD = "sold"
    RETURNED_TO_SUPPLIER = "returned_to_supplier"


class ProductSerial(db.Model, TenantScopedMixin, TimestampMixin):
    """One unit of a serial/IMEI-tracked item (feature "serial_imei") -
    captured when it's bought, picked when it's sold, so "where is this
    IMEI" has an answer."""

    __tablename__ = "product_serials"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "serial_no", name="uq_product_serials_tenant_serial"),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_product_serials_product"), nullable=False, index=True)
    serial_no = db.Column(db.String(64), nullable=False)
    status = db.Column(db.Enum(SerialStatus, name="serial_status"), nullable=False, default=SerialStatus.IN_STOCK)
    location_id = db.Column(db.Integer, db.ForeignKey("stock_locations.id", name="fk_product_serials_location"))
    purchase_bill_id = db.Column(db.Integer, db.ForeignKey("purchase_bills.id", name="fk_product_serials_purchase_bill"))
    sold_pos_bill_id = db.Column(db.Integer, db.ForeignKey("pos_bills.id", name="fk_product_serials_pos_bill"))
    sold_pos_line_id = db.Column(db.Integer, db.ForeignKey("pos_bill_lines.id", name="fk_product_serials_pos_line"))
    sold_at = db.Column(db.DateTime(timezone=True))
    warranty_end = db.Column(db.Date)

    product = db.relationship("Product")
    location = db.relationship("StockLocation")


class StockDocStatus(str, enum.Enum):
    COMPLETED = "completed"
    VOIDED = "voided"


class AdjustmentReason(str, enum.Enum):
    OPENING = "opening"
    COUNT = "count"
    DAMAGE = "damage"
    WASTAGE = "wastage"
    FOUND = "found"
    OTHER = "other"


ADJUSTMENT_REASON_LABELS = {
    AdjustmentReason.OPENING: "Opening stock",
    AdjustmentReason.COUNT: "Physical count (set to what's on the shelf)",
    AdjustmentReason.DAMAGE: "Damaged",
    AdjustmentReason.WASTAGE: "Wastage / expired",
    AdjustmentReason.FOUND: "Found / extra",
    AdjustmentReason.OTHER: "Other correction",
}


class StockAdjustment(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "stock_adjustments"
    __table_args__ = (db.UniqueConstraint("tenant_id", "number", name="uq_stock_adjustments_tenant_number"),)

    id = db.Column(db.Integer, primary_key=True)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_stock_adjustments_series"), nullable=False)
    number = db.Column(db.String(64), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("stock_locations.id", name="fk_stock_adjustments_location"), nullable=False)
    adjusted_on = db.Column(db.Date, nullable=False)
    reason = db.Column(db.Enum(AdjustmentReason, name="adjustment_reason"), nullable=False)
    note = db.Column(db.Text)
    status = db.Column(db.Enum(StockDocStatus, name="stock_doc_status"), nullable=False, default=StockDocStatus.COMPLETED)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_stock_adjustments_created_by"), nullable=False)
    voided_at = db.Column(db.DateTime(timezone=True))
    void_reason = db.Column(db.Text)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_stock_adjustments_voided_by"))

    location = db.relationship("StockLocation")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    lines = db.relationship("StockAdjustmentLine", backref="adjustment", cascade="all, delete-orphan", order_by="StockAdjustmentLine.id")


class StockAdjustmentLine(db.Model):
    __tablename__ = "stock_adjustment_lines"

    id = db.Column(db.Integer, primary_key=True)
    adjustment_id = db.Column(db.Integer, db.ForeignKey("stock_adjustments.id", name="fk_stock_adjustment_lines_adjustment"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_stock_adjustment_lines_product"), nullable=False)
    # Change posted (+/-). For a physical count, counted_qty is what was on
    # the shelf and qty is the difference from what the books said.
    qty = db.Column(db.Numeric(12, 3), nullable=False)
    counted_qty = db.Column(db.Numeric(12, 3))
    unit_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0, server_default="0")
    batch_id = db.Column(db.Integer, db.ForeignKey("product_batches.id", name="fk_stock_adjustment_lines_batch"))

    product = db.relationship("Product")
    batch = db.relationship("ProductBatch")


class StockTransfer(db.Model, TenantScopedMixin, TimestampMixin):
    """Stock moved between two of the client's own locations, with a
    Delivery Challan (its own series) to travel with the goods."""

    __tablename__ = "stock_transfers"
    __table_args__ = (db.UniqueConstraint("tenant_id", "number", name="uq_stock_transfers_tenant_number"),)

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_stock_transfers_gstin"), nullable=False)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_stock_transfers_series"), nullable=False)
    number = db.Column(db.String(64), nullable=False)
    from_location_id = db.Column(db.Integer, db.ForeignKey("stock_locations.id", name="fk_stock_transfers_from"), nullable=False)
    to_location_id = db.Column(db.Integer, db.ForeignKey("stock_locations.id", name="fk_stock_transfers_to"), nullable=False)
    transfer_date = db.Column(db.Date, nullable=False)
    note = db.Column(db.Text)
    vehicle_no = db.Column(db.String(30))
    status = db.Column(db.Enum(StockDocStatus, name="stock_doc_status"), nullable=False, default=StockDocStatus.COMPLETED)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_stock_transfers_created_by"), nullable=False)
    voided_at = db.Column(db.DateTime(timezone=True))
    void_reason = db.Column(db.Text)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_stock_transfers_voided_by"))

    gstin = db.relationship("Gstin")
    from_location = db.relationship("StockLocation", foreign_keys=[from_location_id])
    to_location = db.relationship("StockLocation", foreign_keys=[to_location_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    lines = db.relationship("StockTransferLine", backref="transfer", cascade="all, delete-orphan", order_by="StockTransferLine.id")


class StockTransferLine(db.Model):
    __tablename__ = "stock_transfer_lines"

    id = db.Column(db.Integer, primary_key=True)
    transfer_id = db.Column(db.Integer, db.ForeignKey("stock_transfers.id", name="fk_stock_transfer_lines_transfer"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_stock_transfer_lines_product"), nullable=False)
    qty = db.Column(db.Numeric(12, 3), nullable=False)
    unit_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0, server_default="0")
    batch_id = db.Column(db.Integer, db.ForeignKey("product_batches.id", name="fk_stock_transfer_lines_batch"))

    product = db.relationship("Product")
    batch = db.relationship("ProductBatch")
