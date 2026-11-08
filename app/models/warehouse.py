"""Warehouse dispatch by serial scan (module "warehouse_dispatch").

The showroom sells the product and quantity only; a picker in the chosen
warehouse scans the actual unit's label in the ARFA Warehouse app, and the
serial is fixed at that moment. The warehouse computer then makes the
invoice from the scanned serials.

  - WarehouseLogin: one per person (not a website user), tied to the
    warehouses they work in. Signs in once per phone with the business
    code, a login id and a password, then unlocks with a PIN.
  - WarehouseSession: one per phone - only the refresh token's hash is
    kept, with the phone's push (FCM) token.
  - SerialPick: every scan attempt against an order, pass or fail, so the
    failed scans show where the warehouse struggles.
  - WarehouseNotice: the push outbox - written in the same transaction as
    the order change, sent after it commits, so a push never goes out for
    a rolled-back order.
  - WarehouseReceipt / ReceivedUnit: a purchase order received at a
    warehouse, unit by unit (Receive mode).
  - StockCount / StockCountScan: count mode - every serial on the floor.
"""

import enum

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin, utcnow

_hasher = PasswordHasher()


class DispatchStatus(str, enum.Enum):
    TO_PICK = "to_pick"      # waiting in the warehouse's pipeline
    PICKING = "picking"      # claimed by a picker
    PICKED = "picked"        # every unit scanned, confirmed - at the warehouse computer
    INVOICED = "invoiced"


DISPATCH_STATUS_LABELS = {
    DispatchStatus.TO_PICK: "Waiting at the warehouse",
    DispatchStatus.PICKING: "Picking",
    DispatchStatus.PICKED: "Picked - at the warehouse for invoice",
    DispatchStatus.INVOICED: "Invoiced",
}

# Statuses whose quantity is held at the warehouse (reserved).
HOLDING = (DispatchStatus.TO_PICK, DispatchStatus.PICKING, DispatchStatus.PICKED)


class PickMethod(str, enum.Enum):
    BARCODE = "barcode"
    PHOTO = "photo"      # on-device text reading, or ARFA's label reader
    MANUAL = "manual"
    SCANNER = "scanner"  # a rugged scanner phone's own scanner


class PickResult(str, enum.Enum):
    OK = "ok"
    ADDED = "added"                  # serial wasn't in ARFA; product right - added to stock and picked
    WRONG_PRODUCT = "wrong_product"
    NOT_IN_STOCK = "not_in_stock"
    WRONG_LOCATION = "wrong_location"
    UNKNOWN = "unknown"
    DUPLICATE = "duplicate"          # already picked (for this order or another)
    LINE_FULL = "line_full"          # every unit on the line is already scanned


PICK_RESULT_LABELS = {
    PickResult.OK: "Picked",
    PickResult.ADDED: "Added to stock and picked",
    PickResult.WRONG_PRODUCT: "Wrong product",
    PickResult.NOT_IN_STOCK: "Not in stock",
    PickResult.WRONG_LOCATION: "At another location",
    PickResult.UNKNOWN: "Serial not in ARFA",
    PickResult.DUPLICATE: "Already picked",
    PickResult.LINE_FULL: "Line already complete",
}

warehouse_login_locations = db.Table(
    "warehouse_login_locations",
    db.Column("login_id", db.Integer, db.ForeignKey("warehouse_logins.id", name="fk_wll_login", ondelete="CASCADE"),
              primary_key=True),
    db.Column("location_id", db.Integer, db.ForeignKey("stock_locations.id", name="fk_wll_location",
                                                       ondelete="CASCADE"), primary_key=True),
)


class WarehouseLogin(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "warehouse_logins"
    __table_args__ = (db.UniqueConstraint("tenant_id", "login_id", name="uq_warehouse_logins_tenant_login"),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    login_id = db.Column(db.String(40), nullable=False)       # what they type: a mobile number or a short name
    mobile = db.Column(db.String(15))
    password_hash = db.Column(db.String(255), nullable=False)
    pin_hash = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    # May type a serial by hand (always logged with a photo).
    manual_serial = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    failed_attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    locked_until = db.Column(db.DateTime(timezone=True))
    last_seen_at = db.Column(db.DateTime(timezone=True))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_warehouse_logins_created_by"))

    locations = db.relationship("StockLocation", secondary=warehouse_login_locations, order_by="StockLocation.name")

    def set_password(self, raw: str) -> None:
        self.password_hash = _hasher.hash(raw)
        self.failed_attempts = 0
        self.locked_until = None

    def check_password(self, raw: str) -> bool:
        return _verify(self.password_hash, raw)

    def set_pin(self, raw: str | None) -> None:
        self.pin_hash = _hasher.hash(raw) if raw else None

    def check_pin(self, raw: str) -> bool:
        return bool(self.pin_hash) and _verify(self.pin_hash, raw)

    @property
    def location_ids(self) -> set[int]:
        return {loc.id for loc in self.locations if loc.is_active}

    def __repr__(self):
        return f"<WarehouseLogin {self.login_id}>"


def _verify(hashed: str | None, raw: str) -> bool:
    if not hashed:
        return False
    try:
        return _hasher.verify(hashed, raw or "")
    except (VerifyMismatchError, InvalidHashError):
        return False


class WarehouseSession(db.Model, TenantScopedMixin, TimestampMixin):
    """One phone signed in to one warehouse login."""

    __tablename__ = "warehouse_sessions"

    id = db.Column(db.Integer, primary_key=True)
    login_id = db.Column(db.Integer, db.ForeignKey("warehouse_logins.id", name="fk_warehouse_sessions_login"),
                         nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)   # sha256 of the refresh token
    device_name = db.Column(db.String(120))
    fcm_token = db.Column(db.String(400))
    app_version = db.Column(db.String(20))
    last_seen_at = db.Column(db.DateTime(timezone=True))
    pin_failures = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    # Bumped by every PIN unlock: an access token from before the last one stops working.
    generation = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    revoked_at = db.Column(db.DateTime(timezone=True))
    revoke_reason = db.Column(db.String(60))

    login = db.relationship("WarehouseLogin", backref=db.backref("sessions", lazy="dynamic"))

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None


class SerialPick(db.Model, TenantScopedMixin):
    """One scan attempt against a sales order line - kept whatever the result."""

    __tablename__ = "serial_picks"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "client_pick_id", name="uq_serial_picks_tenant_client"),
        db.Index("ix_serial_picks_tenant_created", "tenant_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    sales_document_id = db.Column(db.Integer, db.ForeignKey("sales_documents.id", name="fk_serial_picks_doc"),
                                  nullable=False, index=True)
    sales_line_id = db.Column(db.Integer, db.ForeignKey("sales_document_lines.id", name="fk_serial_picks_line"),
                              nullable=False)
    serial_id = db.Column(db.Integer, db.ForeignKey("product_serials.id", name="fk_serial_picks_serial"))
    raw_text = db.Column(db.String(200))
    serial_no = db.Column(db.String(64))
    method = db.Column(db.Enum(PickMethod, name="pick_method"), nullable=False)
    result = db.Column(db.Enum(PickResult, name="pick_result"), nullable=False)
    login_id = db.Column(db.Integer, db.ForeignKey("warehouse_logins.id", name="fk_serial_picks_login"))
    session_id = db.Column(db.Integer, db.ForeignKey("warehouse_sessions.id", name="fk_serial_picks_session"))
    client_pick_id = db.Column(db.String(64))
    photo_id = db.Column(db.Integer, db.ForeignKey("uploaded_images.id", name="fk_serial_picks_photo"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    undone_at = db.Column(db.DateTime(timezone=True))
    undone_reason = db.Column(db.String(60))

    serial = db.relationship("ProductSerial")
    login = db.relationship("WarehouseLogin")
    line = db.relationship("SalesDocumentLine")

    @property
    def is_good(self) -> bool:
        return self.result in (PickResult.OK, PickResult.ADDED) and self.undone_at is None


class WarehouseNotice(db.Model, TenantScopedMixin):
    """The push outbox (sent after the change commits)."""

    __tablename__ = "warehouse_notices"

    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey("stock_locations.id", name="fk_warehouse_notices_location"),
                            nullable=False)
    kind = db.Column(db.String(30), nullable=False)       # new_order, claimed, released, cancelled, moved, picked
    sales_document_id = db.Column(db.Integer, db.ForeignKey("sales_documents.id", name="fk_warehouse_notices_doc"))
    title = db.Column(db.String(120))
    body = db.Column(db.String(255))
    silent = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    sent_at = db.Column(db.DateTime(timezone=True))
    attempts = db.Column(db.Integer, nullable=False, default=0)
    recipients = db.Column(db.Integer)
    last_error = db.Column(db.String(255))


class ReceiptStatus(str, enum.Enum):
    OPEN = "open"
    DONE = "done"            # its purchase bill is made
    CANCELLED = "cancelled"


class WarehouseReceipt(db.Model, TenantScopedMixin, TimestampMixin):
    """A purchase order's goods received at a warehouse, unit by unit."""

    __tablename__ = "warehouse_receipts"

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id", name="fk_warehouse_receipts_po"),
                                  nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("stock_locations.id", name="fk_warehouse_receipts_location"),
                            nullable=False)
    status = db.Column(db.Enum(ReceiptStatus, name="receipt_status"), nullable=False, default=ReceiptStatus.OPEN)
    purchase_bill_id = db.Column(db.Integer, db.ForeignKey("purchase_bills.id", name="fk_warehouse_receipts_bill"))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_warehouse_receipts_created_by"))

    purchase_order = db.relationship("PurchaseOrder")
    location = db.relationship("StockLocation")
    units = db.relationship("ReceivedUnit", backref="receipt", cascade="all, delete-orphan",
                            order_by="ReceivedUnit.id")


class ReceivedUnit(db.Model, TenantScopedMixin):
    __tablename__ = "received_units"
    __table_args__ = (
        db.UniqueConstraint("receipt_id", "serial_no", name="uq_received_units_receipt_serial"),
        db.UniqueConstraint("tenant_id", "client_unit_id", name="uq_received_units_tenant_client"),
    )

    id = db.Column(db.Integer, primary_key=True)
    receipt_id = db.Column(db.Integer, db.ForeignKey("warehouse_receipts.id", name="fk_received_units_receipt"),
                           nullable=False, index=True)
    order_line_id = db.Column(db.Integer, db.ForeignKey("purchase_order_lines.id", name="fk_received_units_line"),
                              nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_received_units_product"), nullable=False)
    serial_no = db.Column(db.String(64), nullable=False)
    raw_text = db.Column(db.String(200))
    method = db.Column(db.Enum(PickMethod, name="pick_method"), nullable=False, default=PickMethod.BARCODE)
    photo_id = db.Column(db.Integer, db.ForeignKey("uploaded_images.id", name="fk_received_units_photo"))
    login_id = db.Column(db.Integer, db.ForeignKey("warehouse_logins.id", name="fk_received_units_login"))
    client_unit_id = db.Column(db.String(64))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    login = db.relationship("WarehouseLogin")


class StockCount(db.Model, TenantScopedMixin, TimestampMixin):
    """Count mode: every serial on a warehouse's floor, scanned."""

    __tablename__ = "stock_counts"

    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey("stock_locations.id", name="fk_stock_counts_location"),
                            nullable=False)
    started_by_id = db.Column(db.Integer, db.ForeignKey("warehouse_logins.id", name="fk_stock_counts_started_by"))
    finished_at = db.Column(db.DateTime(timezone=True))
    note = db.Column(db.String(255))

    location = db.relationship("StockLocation")
    started_by = db.relationship("WarehouseLogin")
    scans = db.relationship("StockCountScan", backref="count", cascade="all, delete-orphan",
                            order_by="StockCountScan.id")


class StockCountScan(db.Model, TenantScopedMixin):
    __tablename__ = "stock_count_scans"
    __table_args__ = (
        db.UniqueConstraint("count_id", "serial_no", name="uq_stock_count_scans_count_serial"),
        db.UniqueConstraint("tenant_id", "client_scan_id", name="uq_stock_count_scans_tenant_client"),
    )

    id = db.Column(db.Integer, primary_key=True)
    count_id = db.Column(db.Integer, db.ForeignKey("stock_counts.id", name="fk_stock_count_scans_count"),
                         nullable=False, index=True)
    serial_no = db.Column(db.String(64), nullable=False)
    login_id = db.Column(db.Integer, db.ForeignKey("warehouse_logins.id", name="fk_stock_count_scans_login"))
    client_scan_id = db.Column(db.String(64))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
