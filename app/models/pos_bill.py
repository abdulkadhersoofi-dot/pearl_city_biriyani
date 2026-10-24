import enum

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin, utcnow


class POSBillStatus(str, enum.Enum):
    HELD = "held"
    COMPLETED = "completed"
    REFUNDED = "refunded"
    # Cancelled the same day it was billed (a mistake, not a return) -
    # kept, numbered and reported as cancelled, never deleted.
    VOIDED = "voided"


class PaymentMode(str, enum.Enum):
    CASH = "cash"
    UPI = "upi"
    CARD = "card"
    # On account - the customer pays later. Needs a named customer.
    CREDIT = "credit"
    WALLET = "wallet"
    CHEQUE = "cheque"
    BANK_TRANSFER = "bank_transfer"
    # Paid with the value of goods returned on a POS sales return (an
    # exchange) - never picked at the counter directly.
    EXCHANGE = "exchange"
    # Jewellery: the value of old gold bought on an OldGoldPurchase voucher.
    OLD_GOLD = "old_gold"
    # Loyalty points spent (module "loyalty").
    LOYALTY = "loyalty"
    # POSBill.payment_mode only, when a bill was paid more than one way -
    # the actual split is in POSPayment rows. Never a POSPayment's mode.
    MIXED = "mixed"


PAYMENT_MODE_LABELS = {
    PaymentMode.CASH: "Cash",
    PaymentMode.UPI: "UPI",
    PaymentMode.CARD: "Card",
    PaymentMode.CREDIT: "Credit (on account)",
    PaymentMode.WALLET: "Wallet",
    PaymentMode.CHEQUE: "Cheque",
    PaymentMode.BANK_TRANSFER: "Bank transfer",
    PaymentMode.EXCHANGE: "Exchange credit",
    PaymentMode.OLD_GOLD: "Old gold",
    PaymentMode.LOYALTY: "Loyalty points",
    PaymentMode.MIXED: "Split",
}

# What a client can switch on for its counters (Settings), in display
# order. Cash/UPI/Card are what every client had before this list existed.
SELECTABLE_PAYMENT_MODES = [
    PaymentMode.CASH,
    PaymentMode.UPI,
    PaymentMode.CARD,
    PaymentMode.WALLET,
    PaymentMode.CHEQUE,
    PaymentMode.BANK_TRANSFER,
    PaymentMode.CREDIT,
]
DEFAULT_PAYMENT_MODES = ["cash", "upi", "card"]


class DiscountKind(str, enum.Enum):
    PERCENT = "percent"
    AMOUNT = "amount"


class POSBill(db.Model, TenantScopedMixin, TimestampMixin):
    """A POS cart. A HELD bill is just a parked draft - no bill number is
    allocated for it and it is freely resumed or discarded. A number is
    only allocated at checkout (COMPLETED), because that's the point a
    fiscal receipt actually exists and must never be reused or skipped.
    """

    __tablename__ = "pos_bills"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "bill_number", name="uq_pos_bill_tenant_number"),
        db.UniqueConstraint("tenant_id", "checkout_uuid", name="uq_pos_bills_tenant_checkout_uuid"),
    )

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id"), nullable=False, index=True)
    invoice_series_id = db.Column(db.Integer, db.ForeignKey("invoice_series.id"))
    bill_number = db.Column(db.String(64))   # looked up within a business: uq_*_tenant_number covers it

    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    customer_snapshot = db.Column(db.JSON)

    status = db.Column(db.Enum(POSBillStatus, name="pos_bill_status"), nullable=False, default=POSBillStatus.HELD)
    payment_mode = db.Column(db.Enum(PaymentMode, name="payment_mode"))
    hold_label = db.Column(db.String(100))

    subtotal = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_discount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_cgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_sgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_igst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    round_off = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    grand_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    completed_at = db.Column(db.DateTime(timezone=True))

    refunded_amount = db.Column(db.Numeric(12, 2))
    refund_reason = db.Column(db.Text)
    refunded_at = db.Column(db.DateTime(timezone=True))
    refunded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    # How the refund was paid out, and which open counter day it came out
    # of (day open/close on) - so that day's expected cash accounts for it.
    refund_mode = db.Column(db.Enum(PaymentMode, name="payment_mode"))
    refund_session_id = db.Column(db.Integer, db.ForeignKey("cash_sessions.id", name="fk_pos_bills_refund_session"))

    # Bill-level discount exactly as the cashier entered it (a % of the
    # bill, or a rupee amount off it). It is spread over the lines as
    # POSBillLine.discount_flat - the lines are what tax is computed on.
    bill_discount_kind = db.Column(db.String(7))
    bill_discount_value = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    price_list_id = db.Column(db.Integer, db.ForeignKey("price_lists.id", name="fk_pos_bills_price_list"))
    # The counter day (CashSession) this bill was rung up in - only set
    # while the client has day open/close switched on.
    cash_session_id = db.Column(db.Integer, db.ForeignKey("cash_sessions.id", name="fk_pos_bills_cash_session"), index=True)
    # A margin-scheme invoice (all its lines second-hand goods under Rule
    # 32(5)) - numbered in its own series, no GST shown to the buyer.
    is_margin_scheme = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    # Cash handed over and change given back, when paid (partly) in cash.
    cash_tendered = db.Column(db.Numeric(12, 2))
    change_due = db.Column(db.Numeric(12, 2))

    voided_at = db.Column(db.DateTime(timezone=True))
    void_reason = db.Column(db.Text)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_pos_bills_voided_by"))
    # When the original was printed. Every later print - a reprint, the
    # day-book's copy - is marked DUPLICATE COPY (app.pos.receipt).
    original_printed_at = db.Column(db.DateTime(timezone=True))
    # Restaurant (restaurant_mode): dine_in / takeaway / delivery / swiggy /
    # zomato, and the table of a dine-in order (a HELD bill with a table is
    # that table's running order).
    order_type = db.Column(db.String(20))
    table_id = db.Column(db.Integer, db.ForeignKey("dining_tables.id", name="fk_pos_bills_table"))
    # Pharmacy: Schedule H / H1 / X sale - {"doctor", "doctor_reg_no",
    # "patient", "patient_address"}.
    prescription = db.Column(db.JSON)
    # Wholesale: who made the sale.
    salesman_id = db.Column(db.Integer, db.ForeignKey("salesmen.id", name="fk_pos_bills_salesman"))
    # Phase 10: the counter device whose own series numbered a bill made
    # while offline (feature "offline_pos"), and the cashier who signed in
    # with their PIN on a shared counter (feature "cashier_pin").
    device_id = db.Column(db.Integer, db.ForeignKey("counter_devices.id", name="fk_pos_bills_device"))
    cashier_id = db.Column(db.Integer, db.ForeignKey("cashiers.id", name="fk_pos_bills_cashier"))
    # The counter's own id for one checkout: a retry after a lost reply
    # returns the same bill instead of billing twice, and the same id on
    # a bill queued offline shows it already reached the server.
    checkout_uuid = db.Column(db.String(36))

    gstin = db.relationship("Gstin")
    customer = db.relationship("Customer")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    refunded_by = db.relationship("User", foreign_keys=[refunded_by_id])
    voided_by = db.relationship("User", foreign_keys=[voided_by_id])
    price_list = db.relationship("PriceList")
    table = db.relationship("DiningTable")
    salesman = db.relationship("Salesman")
    device = db.relationship("CounterDevice")
    cashier = db.relationship("Cashier")
    lines = db.relationship(
        "POSBillLine",
        backref="bill",
        cascade="all, delete-orphan",
        order_by="POSBillLine.sort_order",
    )
    payments = db.relationship(
        "POSPayment",
        backref="bill",
        cascade="all, delete-orphan",
        order_by="POSPayment.id",
    )

    @property
    def is_b2b(self) -> bool:
        return bool(((self.customer_snapshot or {}).get("gstin") or "").strip())

    @property
    def credit_amount(self):
        return sum((p.amount for p in self.payments if p.mode == PaymentMode.CREDIT), 0)

    def payment_summary(self) -> str:
        """"Cash" for a single-mode bill, "Cash 300.00 + UPI 200.00" for a
        split one - what the day-book and receipt print."""
        if len(self.payments) > 1:
            return " + ".join(f"{PAYMENT_MODE_LABELS[p.mode]} {p.amount:.2f}" for p in self.payments)
        if self.payments:
            return PAYMENT_MODE_LABELS[self.payments[0].mode]
        return PAYMENT_MODE_LABELS.get(self.payment_mode, "-") if self.payment_mode else "-"

    def __repr__(self):
        return f"<POSBill {self.bill_number or f'held-{self.id}'}>"


class POSBillLine(db.Model, TimestampMixin):
    __tablename__ = "pos_bill_lines"

    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("pos_bills.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"))

    description = db.Column(db.String(255), nullable=False)
    hsn_or_sac_code = db.Column(db.String(8))
    qty = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit = db.Column(db.String(20), nullable=False, default="pcs")
    rate = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    discount_percent = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    # Rupees off the line on its own price basis (pre-tax for an exclusive
    # price, GST-inclusive for an inclusive one) - a line's own rupee
    # discount plus its share of any bill discount. With discount_percent,
    # exactly what app.utils.gst.compute_line was given.
    discount_flat = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    # The line's own discount as entered (for resuming a held bill and for
    # the receipt) - a % or a rupee amount off what the customer pays.
    line_discount_kind = db.Column(db.String(7))
    line_discount_value = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    discount_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    taxable_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    # Whether `rate` was the GST-inclusive price (the taxable value was
    # backed out of it - see app.utils.gst.compute_line) or the pre-tax
    # price. Kept so a document can be shown, copied or credited exactly
    # as it was computed.
    price_includes_tax = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    cgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    sgst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    igst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    # Margin scheme (Rule 32(5)): on such a line taxable_value and the tax
    # are the margin's; line_total is the selling price. What it cost to
    # buy (margin_purchase_value) and the margin itself (never below 0)
    # are kept for the margin register and the HSN 0% row.
    is_margin_scheme = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    margin_purchase_value = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    margin_value = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    # Phase 9. A kitchen note ("less spicy"); how many of the item's own
    # unit one of `unit` is (a strip of 10 tablets: 10) - stock moves in
    # qty x unit_factor; a jewellery line's price breakdown; and whether the
    # line is a charge (packing / service charge), not an item.
    note = db.Column(db.String(140))
    unit_factor = db.Column(db.Numeric(12, 3), nullable=False, default=1, server_default="1")
    jewellery = db.Column(db.JSON)
    charge_kind = db.Column(db.String(10))

    product = db.relationship("Product")

    @property
    def serial_numbers(self) -> list[str]:
        """Serial numbers / IMEIs sold on this line (feature "serial_imei"),
        printed on the receipt under the item."""
        from app.models.inventory import ProductSerial

        if self.id is None or not self.product_id:
            return []
        return [u.serial_no for u in ProductSerial.query.filter_by(sold_pos_line_id=self.id).order_by(ProductSerial.serial_no)]

    def __repr__(self):
        return f"<POSBillLine {self.id} {self.description}>"


class POSPayment(db.Model, TenantScopedMixin):
    """One way a POS bill was paid - a bill paid Rs. 300 cash + Rs. 200 UPI
    has two rows. Every completed bill has at least one; they always add
    up to its grand total."""

    __tablename__ = "pos_payments"

    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("pos_bills.id", name="fk_pos_payments_bill"), nullable=False, index=True)
    mode = db.Column(db.Enum(PaymentMode, name="payment_mode"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    # UPI ref / card slip / cheque number - optional, free text.
    reference = db.Column(db.String(100))
    # For an EXCHANGE payment, the sales return whose value paid for it.
    pos_return_id = db.Column(db.Integer, db.ForeignKey("pos_returns.id", name="fk_pos_payments_return"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    pos_return = db.relationship("POSReturn")

    @property
    def label(self) -> str:
        return PAYMENT_MODE_LABELS[self.mode]
