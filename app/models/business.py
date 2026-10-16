"""Business-type modules (Phase 9) - each behind its own flag:

  restaurant_mode  DiningTable, KitchenTicket (KOT)
  jewellery        MetalRate (today's rate per metal / purity),
                   OldGoldPurchase (old gold bought from a customer - its
                   own numbered voucher, used as a payment at the counter)
  job_cards        JobCard + JobCardLine (parts from stock, labour)
  wholesale        CustomerGroup (-> a price list), PriceSlab (quantity
                   breaks), Salesman
  loyalty          LoyaltyEntry (points earned / spent - the balance is
                   their sum)

Columns these modules add to existing tables are on those models
(Product, POSBill, POSBillLine, Customer, Invoice, CustomerReceipt,
Tenant.module_settings)."""

import enum

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin, utcnow


# --- restaurant ------------------------------------------------------------------------

class DiningTable(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "dining_tables"
    __table_args__ = (db.UniqueConstraint("tenant_id", "name", name="uq_dining_tables_tenant_name"),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(30), nullable=False)
    section = db.Column(db.String(40))  # "AC hall", "Terrace"
    seats = db.Column(db.Integer)
    sort_order = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())


class KotStatus(str, enum.Enum):
    SENT = "sent"
    BILLED = "billed"  # its order was billed
    CANCELLED = "cancelled"  # the order was dropped without a bill


class KitchenTicket(db.Model, TenantScopedMixin):
    """A kitchen order ticket: what went to the kitchen, when. Numbered per
    day (not a tax document). A reduced quantity goes as a CANCEL line on
    the next ticket - nothing printed is ever changed."""

    __tablename__ = "kitchen_tickets"
    __table_args__ = (db.Index("ix_kitchen_tickets_tenant_table", "tenant_id", "table_id", "status"),)

    id = db.Column(db.Integer, primary_key=True)
    kot_date = db.Column(db.Date, nullable=False)
    kot_no = db.Column(db.Integer, nullable=False)
    order_type = db.Column(db.String(20), nullable=False)
    table_id = db.Column(db.Integer, db.ForeignKey("dining_tables.id", name="fk_kitchen_tickets_table"))
    order_ref = db.Column(db.String(40))  # takeaway / app order: a name or the app's order id
    # [{"product_id", "name", "qty" (negative = cancel), "note"}]
    items = db.Column(db.JSON, nullable=False)
    status = db.Column(db.Enum(KotStatus, name="kot_status"), nullable=False, default=KotStatus.SENT)
    pos_bill_id = db.Column(db.Integer, db.ForeignKey("pos_bills.id", name="fk_kitchen_tickets_pos_bill"))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_kitchen_tickets_created_by"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    table = db.relationship("DiningTable")
    created_by = db.relationship("User")


# --- jewellery -------------------------------------------------------------------------

class MetalRate(db.Model, TenantScopedMixin):
    """The day's rate per gram for one metal and purity - set each morning.
    Never edited: a correction is a newer row for the same day."""

    __tablename__ = "metal_rates"
    __table_args__ = (db.Index("ix_metal_rates_lookup", "tenant_id", "metal", "purity", "rate_date"),)

    id = db.Column(db.Integer, primary_key=True)
    rate_date = db.Column(db.Date, nullable=False)
    metal = db.Column(db.String(10), nullable=False)  # gold / silver / platinum
    purity = db.Column(db.String(6), nullable=False)  # 22K, 925...
    rate_per_gram = db.Column(db.Numeric(12, 2), nullable=False)
    set_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_metal_rates_set_by"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class OldGoldStatus(str, enum.Enum):
    OPEN = "open"  # value not (fully) used yet
    USED = "used"
    VOID = "void"


class OldGoldPurchase(db.Model, TenantScopedMixin, TimestampMixin):
    """Old gold / silver bought from a customer (usually against a new
    purchase). Its own numbered voucher (series OG); its value pays for
    the new bill as the "Old gold" payment - after GST on the new
    jewellery, which is charged on its full value. No GST on buying old
    gold from a consumer. Never deleted - voided with a reason."""

    __tablename__ = "old_gold_purchases"
    __table_args__ = (db.UniqueConstraint("tenant_id", "number", name="uq_old_gold_purchases_tenant_number"),)

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_old_gold_purchases_gstin"), nullable=False)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_old_gold_purchases_series"),
                                  nullable=False)
    number = db.Column(db.String(64), nullable=False)
    purchase_date = db.Column(db.Date, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", name="fk_old_gold_purchases_customer"))
    seller_name = db.Column(db.String(255), nullable=False)
    seller_phone = db.Column(db.String(20))
    seller_address = db.Column(db.Text)
    id_proof = db.Column(db.String(60))  # "Aadhaar XXXX1234"
    description = db.Column(db.String(255), nullable=False)
    metal = db.Column(db.String(10), nullable=False)
    purity = db.Column(db.String(6), nullable=False)
    gross_weight = db.Column(db.Numeric(10, 3), nullable=False)
    net_weight = db.Column(db.Numeric(10, 3), nullable=False)  # after stones / wax / melting loss
    rate_per_gram = db.Column(db.Numeric(12, 2), nullable=False)
    value = db.Column(db.Numeric(12, 2), nullable=False)
    used_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    status = db.Column(db.Enum(OldGoldStatus, name="old_gold_status"), nullable=False, default=OldGoldStatus.OPEN)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_old_gold_purchases_created_by"), nullable=False)
    voided_at = db.Column(db.DateTime(timezone=True))
    void_reason = db.Column(db.Text)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_old_gold_purchases_voided_by"))

    customer = db.relationship("Customer")
    gstin = db.relationship("Gstin")
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    @property
    def balance(self):
        return (self.value or 0) - (self.used_amount or 0)


# --- job cards -------------------------------------------------------------------------

class JobStatus(str, enum.Enum):
    RECEIVED = "received"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


JOB_STATUS_LABELS = {
    JobStatus.RECEIVED: "Received",
    JobStatus.IN_PROGRESS: "In progress",
    JobStatus.READY: "Ready",
    JobStatus.DELIVERED: "Delivered",
    JobStatus.CANCELLED: "Cancelled",
}


class JobCard(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "job_cards"
    __table_args__ = (db.UniqueConstraint("tenant_id", "number", name="uq_job_cards_tenant_number"),)

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_job_cards_gstin"), nullable=False)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_job_cards_series"), nullable=False)
    number = db.Column(db.String(64), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", name="fk_job_cards_customer"), nullable=False,
                            index=True)
    received_on = db.Column(db.Date, nullable=False)
    item = db.Column(db.String(150), nullable=False)  # "Samsung A35, black"
    serial_no = db.Column(db.String(64))  # IMEI / serial of the item received
    accessories = db.Column(db.String(255))
    complaint = db.Column(db.Text, nullable=False)
    estimate = db.Column(db.Numeric(12, 2))
    promised_on = db.Column(db.Date)
    technician = db.Column(db.String(100))
    status = db.Column(db.Enum(JobStatus, name="job_status"), nullable=False, default=JobStatus.RECEIVED)
    status_changed_at = db.Column(db.DateTime(timezone=True))
    work_notes = db.Column(db.Text)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id", name="fk_job_cards_invoice"))
    delivered_at = db.Column(db.DateTime(timezone=True))
    cancel_reason = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_job_cards_created_by"), nullable=False)

    customer = db.relationship("Customer")
    gstin = db.relationship("Gstin")
    invoice = db.relationship("Invoice")
    lines = db.relationship("JobCardLine", backref="job", cascade="all, delete-orphan", order_by="JobCardLine.id")


class JobCardLine(db.Model, TimestampMixin):
    """A part used (product_id set - taken out of stock while the job is
    open) or labour (a SAC service line)."""

    __tablename__ = "job_card_lines"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("job_cards.id", name="fk_job_card_lines_job"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_job_card_lines_product"))
    description = db.Column(db.String(255), nullable=False)
    hsn_or_sac_code = db.Column(db.String(8), nullable=False)
    qty = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit = db.Column(db.String(20), nullable=False, default="NOS")
    rate = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    price_includes_tax = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    product = db.relationship("Product")


# --- wholesale -------------------------------------------------------------------------

class CustomerGroup(db.Model, TenantScopedMixin, TimestampMixin):
    """"Retailers", "Dealers", "Institutions" - each buys at its own price
    list."""

    __tablename__ = "customer_groups"
    __table_args__ = (db.UniqueConstraint("tenant_id", "name", name="uq_customer_groups_tenant_name"),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    price_list_id = db.Column(db.Integer, db.ForeignKey("price_lists.id", name="fk_customer_groups_price_list"))
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())

    price_list = db.relationship("PriceList")


class PriceSlab(db.Model, TenantScopedMixin):
    """Quantity break: buying at least min_qty (in the item's own unit), each
    unit costs `price` (on the item's own GST-inclusive / exclusive basis)."""

    __tablename__ = "price_slabs"
    __table_args__ = (db.UniqueConstraint("product_id", "min_qty", name="uq_price_slabs_product_qty"),)

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_price_slabs_product"), nullable=False,
                           index=True)
    min_qty = db.Column(db.Numeric(12, 3), nullable=False)
    price = db.Column(db.Numeric(12, 2), nullable=False)

    product = db.relationship("Product")


class Salesman(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "salesmen"
    __table_args__ = (db.UniqueConstraint("tenant_id", "name", name="uq_salesmen_tenant_name"),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20))
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())


# --- loyalty ---------------------------------------------------------------------------

class LoyaltyEntry(db.Model, TenantScopedMixin):
    """Points in (+, earned on a bill) or out (-, spent at the counter, or
    taken back when a bill is voided / returned). Never edited."""

    __tablename__ = "loyalty_entries"
    __table_args__ = (db.Index("ix_loyalty_entries_customer", "tenant_id", "customer_id"),)

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", name="fk_loyalty_entries_customer"), nullable=False)
    points = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(10), nullable=False)  # earn / redeem / reverse / adjust
    source_type = db.Column(db.String(20))  # pos_bill / pos_return
    source_id = db.Column(db.Integer)
    note = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_loyalty_entries_created_by"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
