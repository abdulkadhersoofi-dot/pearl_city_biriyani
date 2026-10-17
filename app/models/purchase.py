"""Buying side: suppliers, purchase orders, purchase bills (with input
tax credit), purchase returns / debit notes to suppliers, and the
GSTR-2B statement they are matched against. Feature "purchases"."""

import enum

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin


class Supplier(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    gstin = db.Column(db.String(15), index=True)
    state_code = db.Column(db.String(2))
    state_name = db.Column(db.String(64))
    address_line1 = db.Column(db.String(255))
    city = db.Column(db.String(100))
    pincode = db.Column(db.String(10))
    contact_person = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    email = db.Column(db.String(255))
    # What was owed to them when the client started using the app.
    opening_balance = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    opening_balance_date = db.Column(db.Date)
    payment_terms_days = db.Column(db.Integer)
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "gstin": self.gstin,
            "state_code": self.state_code,
            "state_name": self.state_name,
            "address": self.address_line1,
            "city": self.city,
            "phone": self.phone,
        }

    def __repr__(self):
        return f"<Supplier {self.id} {self.name}>"


class ItcEligibility(str, enum.Enum):
    """Whether the GST paid on a purchase line can be claimed back as input
    tax credit. BLOCKED = expressly barred by s.17(5) CGST Act (motor cars,
    food & beverages, works contract for immovable property, personal
    use...); INELIGIBLE = not usable for another reason (exempt supplies,
    non-business use). Not-claimable GST becomes part of the item's cost."""

    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    BLOCKED = "blocked"


ITC_LABELS = {
    ItcEligibility.ELIGIBLE: "Eligible",
    ItcEligibility.INELIGIBLE: "Ineligible",
    ItcEligibility.BLOCKED: "Blocked u/s 17(5)",
}


class PurchaseStatus(str, enum.Enum):
    POSTED = "posted"
    VOIDED = "voided"


class PurchaseBill(db.Model, TenantScopedMixin, TimestampMixin):
    """A supplier's invoice, entered by the client. Posting it adds the
    goods to stock at landed cost and records the input tax credit. Gets
    an internal number from its own series (PB/<FY>/0001) besides the
    supplier's own invoice number. Never deleted - voided with a reason."""

    __tablename__ = "purchase_bills"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "number", name="uq_purchase_bills_tenant_number"),
        db.Index("ix_purchase_bills_supplier_invoice", "tenant_id", "supplier_id", "supplier_invoice_no"),
    )

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_purchase_bills_gstin"), nullable=False)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_purchase_bills_series"), nullable=False)
    number = db.Column(db.String(64), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id", name="fk_purchase_bills_supplier"), nullable=False, index=True)
    supplier_snapshot = db.Column(db.JSON, nullable=False)
    supplier_invoice_no = db.Column(db.String(50), nullable=False)
    supplier_invoice_date = db.Column(db.Date, nullable=False)
    bill_date = db.Column(db.Date, nullable=False)
    place_of_supply_state_code = db.Column(db.String(2), nullable=False)
    is_rcm = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    location_id = db.Column(db.Integer, db.ForeignKey("stock_locations.id", name="fk_purchase_bills_location"), nullable=False)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id", name="fk_purchase_bills_order"))
    status = db.Column(db.Enum(PurchaseStatus, name="purchase_status"), nullable=False, default=PurchaseStatus.POSTED)

    # Freight, loading, packing... on the bill: spread over the goods for
    # costing (landed cost) and added to what is payable.
    other_charges = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    other_charges_label = db.Column(db.String(60))

    subtotal = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_discount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_cgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_sgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_igst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    round_off = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    grand_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    # Input tax credit this bill gives (eligible lines only).
    itc_cgst = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    itc_sgst = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    itc_igst = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")

    # "bill" - a supplier's invoice entered here; "margin_voucher" - the
    # dealer's own voucher for second-hand goods bought for resale under
    # the margin scheme (app/margin/buying.py), numbered SHP/<FY>/0001.
    kind = db.Column(db.String(20), nullable=False, default="bill", server_default="bill")
    # For a margin voucher: who sold the goods - address, ID proof, how
    # they were paid ({"address", "id_type", "id_no", "paid_by"}).
    seller_details = db.Column(db.JSON)
    notes = db.Column(db.Text)
    attachment_id = db.Column(db.Integer, db.ForeignKey("uploaded_images.id", name="fk_purchase_bills_attachment"))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_purchase_bills_created_by"), nullable=False)
    voided_at = db.Column(db.DateTime(timezone=True))
    void_reason = db.Column(db.Text)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_purchase_bills_voided_by"))

    gstin = db.relationship("Gstin")
    supplier = db.relationship("Supplier")
    location = db.relationship("StockLocation")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    lines = db.relationship("PurchaseBillLine", backref="bill", cascade="all, delete-orphan", order_by="PurchaseBillLine.sort_order")

    @property
    def total_tax(self):
        return self.total_cgst + self.total_sgst + self.total_igst

    @property
    def itc_total(self):
        return self.itc_cgst + self.itc_sgst + self.itc_igst


class PurchaseBillLine(db.Model, TimestampMixin):
    __tablename__ = "purchase_bill_lines"

    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("purchase_bills.id", name="fk_purchase_bill_lines_bill"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_purchase_bill_lines_product"))
    description = db.Column(db.String(255), nullable=False)
    hsn_or_sac_code = db.Column(db.String(8))
    qty = db.Column(db.Numeric(12, 3), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default="PCS")
    rate = db.Column(db.Numeric(12, 2), nullable=False)
    discount_percent = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    discount_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    cgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    sgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    igst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    itc_eligibility = db.Column(db.Enum(ItcEligibility, name="itc_eligibility"), nullable=False, default=ItcEligibility.ELIGIBLE)
    # Share of the bill's other charges, and the resulting cost of one unit
    # (taxable + share + any GST that can't be claimed, / qty).
    charges_share = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    unit_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0, server_default="0")
    batch_id = db.Column(db.Integer, db.ForeignKey("product_batches.id", name="fk_purchase_bill_lines_batch"))
    # Bought for resale under the margin scheme: never gives ITC, and each
    # unit's price is what the margin is later worked out against.
    is_margin_scheme = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    product = db.relationship("Product")
    batch = db.relationship("ProductBatch")

    @property
    def margin_unit_price(self):
        """Purchase price of one unit for the margin scheme: what was paid
        for the line (after discount, with any GST - no credit is taken on
        it), per unit. Freight and other bill charges aren't part of it."""
        from decimal import ROUND_HALF_UP, Decimal

        return (Decimal(self.line_total) / Decimal(self.qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) \
            if self.qty else Decimal("0")


class PurchaseReturn(db.Model, TenantScopedMixin, TimestampMixin):
    """Goods sent back to a supplier against a purchase bill - a debit note
    to the supplier, own series (PDN/<FY>/0001). Takes the goods out of
    stock at what they cost and reverses the input tax credit."""

    __tablename__ = "purchase_returns"
    __table_args__ = (db.UniqueConstraint("tenant_id", "number", name="uq_purchase_returns_tenant_number"),)

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_purchase_returns_gstin"), nullable=False)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_purchase_returns_series"), nullable=False)
    number = db.Column(db.String(64), nullable=False)
    purchase_bill_id = db.Column(db.Integer, db.ForeignKey("purchase_bills.id", name="fk_purchase_returns_bill"), nullable=False, index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id", name="fk_purchase_returns_supplier"), nullable=False)
    supplier_snapshot = db.Column(db.JSON, nullable=False)
    return_date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("stock_locations.id", name="fk_purchase_returns_location"), nullable=False)
    status = db.Column(db.Enum(PurchaseStatus, name="purchase_status"), nullable=False, default=PurchaseStatus.POSTED)

    subtotal = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_discount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_cgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_sgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_igst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    round_off = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    grand_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    itc_cgst = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    itc_sgst = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    itc_igst = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_purchase_returns_created_by"), nullable=False)
    voided_at = db.Column(db.DateTime(timezone=True))
    void_reason = db.Column(db.Text)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_purchase_returns_voided_by"))

    gstin = db.relationship("Gstin")
    bill = db.relationship("PurchaseBill", backref=db.backref("returns", order_by="PurchaseReturn.id"))
    supplier = db.relationship("Supplier")
    location = db.relationship("StockLocation")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    lines = db.relationship("PurchaseReturnLine", backref="purchase_return", cascade="all, delete-orphan", order_by="PurchaseReturnLine.id")


class PurchaseReturnLine(db.Model):
    __tablename__ = "purchase_return_lines"

    id = db.Column(db.Integer, primary_key=True)
    return_id = db.Column(db.Integer, db.ForeignKey("purchase_returns.id", name="fk_purchase_return_lines_return"), nullable=False, index=True)
    bill_line_id = db.Column(db.Integer, db.ForeignKey("purchase_bill_lines.id", name="fk_purchase_return_lines_bill_line"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_purchase_return_lines_product"))
    description = db.Column(db.String(255), nullable=False)
    hsn_or_sac_code = db.Column(db.String(8))
    qty = db.Column(db.Numeric(12, 3), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default="PCS")
    rate = db.Column(db.Numeric(12, 2), nullable=False)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    discount_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    cgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    sgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    igst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    unit_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0, server_default="0")
    itc_eligibility = db.Column(db.Enum(ItcEligibility, name="itc_eligibility"), nullable=False, default=ItcEligibility.ELIGIBLE)

    bill_line = db.relationship("PurchaseBillLine")
    product = db.relationship("Product")


class PurchaseOrderStatus(str, enum.Enum):
    OPEN = "open"
    CONVERTED = "converted"
    CANCELLED = "cancelled"


class PurchaseOrder(db.Model, TenantScopedMixin, TimestampMixin):
    """What the client asked a supplier for. No tax effect; turns into a
    purchase bill (pre-filled) when the goods and the supplier's invoice
    arrive."""

    __tablename__ = "purchase_orders"
    __table_args__ = (db.UniqueConstraint("tenant_id", "number", name="uq_purchase_orders_tenant_number"),)

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_purchase_orders_gstin"), nullable=False)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id", name="fk_purchase_orders_series"), nullable=False)
    number = db.Column(db.String(64), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id", name="fk_purchase_orders_supplier"), nullable=False)
    order_date = db.Column(db.Date, nullable=False)
    expected_date = db.Column(db.Date)
    status = db.Column(db.Enum(PurchaseOrderStatus, name="purchase_order_status"), nullable=False, default=PurchaseOrderStatus.OPEN)
    notes = db.Column(db.Text)
    estimated_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_purchase_orders_created_by"), nullable=False)
    cancel_reason = db.Column(db.Text)

    supplier = db.relationship("Supplier")
    created_by = db.relationship("User")
    lines = db.relationship("PurchaseOrderLine", backref="order", cascade="all, delete-orphan", order_by="PurchaseOrderLine.id")
    bills = db.relationship("PurchaseBill", backref="purchase_order")


class PurchaseOrderLine(db.Model):
    __tablename__ = "purchase_order_lines"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id", name="fk_purchase_order_lines_order"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_purchase_order_lines_product"))
    description = db.Column(db.String(255), nullable=False)
    hsn_or_sac_code = db.Column(db.String(8))
    qty = db.Column(db.Numeric(12, 3), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default="PCS")
    rate = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)

    product = db.relationship("Product")


class MatchStatus(str, enum.Enum):
    MATCHED = "matched"
    MISMATCH = "mismatch"
    ONLY_IN_2B = "only_in_2b"


MATCH_STATUS_LABELS = {
    MatchStatus.MATCHED: "Matched",
    MatchStatus.MISMATCH: "Mismatch",
    MatchStatus.ONLY_IN_2B: "Only in 2B",
}


class Gstr2bImport(db.Model, TenantScopedMixin, TimestampMixin):
    """One GSTR-2B statement uploaded for a GSTIN and month."""

    __tablename__ = "gstr2b_imports"

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_gstr2b_imports_gstin"), nullable=False)
    period = db.Column(db.String(7), nullable=False)  # YYYY-MM
    filename = db.Column(db.String(255))
    source = db.Column(db.String(10), nullable=False)  # excel / json
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_gstr2b_imports_uploaded_by"), nullable=False)

    gstin = db.relationship("Gstin")
    entries = db.relationship("Gstr2bEntry", backref="gstr2b_import", cascade="all, delete-orphan", order_by="Gstr2bEntry.id")


class Gstr2bEntry(db.Model, TenantScopedMixin):
    """One supplier invoice as the government's GSTR-2B reports it."""

    __tablename__ = "gstr2b_entries"

    id = db.Column(db.Integer, primary_key=True)
    import_id = db.Column(db.Integer, db.ForeignKey("gstr2b_imports.id", name="fk_gstr2b_entries_import"), nullable=False, index=True)
    supplier_gstin = db.Column(db.String(15), nullable=False)
    supplier_name = db.Column(db.String(255))
    invoice_no = db.Column(db.String(50), nullable=False)
    invoice_date = db.Column(db.Date)
    invoice_value = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable_value = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    igst = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    cgst = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    sgst = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    cess = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    is_rcm = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    itc_available = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    status = db.Column(db.Enum(MatchStatus, name="gstr2b_match_status"), nullable=False, default=MatchStatus.ONLY_IN_2B)
    matched_bill_id = db.Column(db.Integer, db.ForeignKey("purchase_bills.id", name="fk_gstr2b_entries_bill"))
    match_note = db.Column(db.String(255))
    manually_matched = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    matched_bill = db.relationship("PurchaseBill")

    @property
    def total_tax(self):
        return self.igst + self.cgst + self.sgst
