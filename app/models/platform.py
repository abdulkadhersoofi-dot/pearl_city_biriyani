"""Platform, counter and security tables (Phase 10):

  offline_pos     CounterDevice - one browser / Android app at a counter,
                  with its own bill series (prefix O1, O2, ...) for bills
                  made while offline; OfflineBill - each bill a device
                  queued, kept until it is on the books
                  DevicePairing - a one-time code that signs a phone in
                  as a branch login (the ARFA Counter app)
  cashier_pin     Cashier - people sharing one branch counter, each with
                  a PIN, so the bill records who rang it up
  platform        PlatformProfile - the firm's own billing details (one
                  row); PlatformSeries + PlatformInvoice - the firm's
                  subscription invoices to its clients and Auditor offices

The firm's own profile and series are not any client's data, so they carry
no tenant_id; a PlatformInvoice carries the tenant_id of the client it
bills (null for an Auditor office), which is what a Client Admin's
Subscription page is scoped by."""

import enum

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin


# --- counter devices / offline bills -----------------------------------------------------

class CounterDevice(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "counter_devices"
    __table_args__ = (db.UniqueConstraint("tenant_id", "code", name="uq_counter_devices_tenant_code"),)

    id = db.Column(db.Integer, primary_key=True)
    # The login (a branch, or the Client Admin) this device bills as.
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_counter_devices_user"), nullable=False,
                        index=True)
    # "O1".."O99" - the prefix of this device's offline series, so its bill
    # numbers ("O1/2026-27/0001") stay within GST's 16 characters.
    code = db.Column(db.String(4), nullable=False)
    name = db.Column(db.String(60), nullable=False)
    platform = db.Column(db.String(10), nullable=False, default="web", server_default="web")  # web / android / app
    # SHA-256 of the device's secret - the secret itself only ever lives on
    # the device.
    token_hash = db.Column(db.String(64), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    last_seen_at = db.Column(db.DateTime(timezone=True))
    last_synced_at = db.Column(db.DateTime(timezone=True))

    user = db.relationship("User")


class DevicePairing(db.Model, TenantScopedMixin, TimestampMixin):
    """A one-time code the Client Admin gives a branch login's phone or
    tablet so the ARFA Counter app can sign in without the branch's email
    and password (counter app, stage 1). Six digits, valid for 15 minutes,
    used once; only its hash is kept. Pairing makes the device a
    CounterDevice of that login."""

    __tablename__ = "device_pairings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_device_pairings_user"), nullable=False,
                        index=True)
    code_hash = db.Column(db.String(64), nullable=False, index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_device_pairings_created_by"))
    device_id = db.Column(db.Integer, db.ForeignKey("counter_devices.id", name="fk_device_pairings_device"))

    user = db.relationship("User", foreign_keys=[user_id])
    device = db.relationship("CounterDevice")


class LoginHandoff(db.Model, TimestampMixin):
    """A one-time hand-over from a client's own sign-in address
    (Tenant.login_domain) to the main address: a browser's sign-in can't
    cross from one domain to another, so the sign-in address issues this
    and the main address redeems it (app/auth/login_domain.py). Valid for
    a minute, used once; only its hash is kept."""

    __tablename__ = "login_handoffs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_login_handoffs_user"), nullable=False,
                        index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    from_host = db.Column(db.String(253), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    used_at = db.Column(db.DateTime(timezone=True))

    user = db.relationship("User")


class OfflineBillStatus(str, enum.Enum):
    SYNCED = "synced"  # on the books as a POS bill
    FAILED = "failed"  # the server couldn't take it - waits for the Client Admin
    VOIDED = "voided"  # the Client Admin cancelled it; its number is kept as a cancelled bill


class OfflineBill(db.Model, TenantScopedMixin, TimestampMixin):
    """One bill a device made offline, as it sent it. Never deleted - the
    number on the customer's paper bill is accounted for here even while
    the bill itself can't be booked."""

    __tablename__ = "offline_bills"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "client_uuid", name="uq_offline_bills_tenant_uuid"),
        db.UniqueConstraint("tenant_id", "bill_number", name="uq_offline_bills_tenant_number"),
    )

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey("counter_devices.id", name="fk_offline_bills_device"),
                          nullable=False, index=True)
    # Made on the device, so a retried upload is recognised and never
    # booked twice.
    client_uuid = db.Column(db.String(36), nullable=False)
    bill_number = db.Column(db.String(20), nullable=False)
    billed_at = db.Column(db.DateTime(timezone=True), nullable=False)
    payload = db.Column(db.JSON, nullable=False)
    status = db.Column(db.Enum(OfflineBillStatus, name="offline_bill_status"), nullable=False)
    error = db.Column(db.Text)
    # The device's total differed from the server's - booked at the
    # server's figure, flagged for a look.
    total_mismatch = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    attempts = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    pos_bill_id = db.Column(db.Integer, db.ForeignKey("pos_bills.id", name="fk_offline_bills_pos_bill"))
    resolved_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_offline_bills_resolved_by"))
    resolved_at = db.Column(db.DateTime(timezone=True))
    resolution_note = db.Column(db.Text)

    device = db.relationship("CounterDevice")
    pos_bill = db.relationship("POSBill")
    resolved_by = db.relationship("User")


# --- cashiers on a shared counter -------------------------------------------------------

class Cashier(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "cashiers"
    __table_args__ = (db.UniqueConstraint("branch_user_id", "name", name="uq_cashiers_branch_name"),)

    id = db.Column(db.Integer, primary_key=True)
    # The branch login whose counter this cashier works at.
    branch_user_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_cashiers_branch_user"),
                               nullable=False, index=True)
    name = db.Column(db.String(60), nullable=False)
    # HMAC-SHA256 of the PIN keyed by the server's secret (app.auth.security)
    # - a PIN picks its cashier, so it must be unique at a counter.
    pin_digest = db.Column(db.String(64), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())

    branch_user = db.relationship("User")


# --- the firm's own subscription invoices --------------------------------------------------

class PlatformProfile(db.Model, TimestampMixin):
    """The firm's billing details, printed on its subscription invoices.
    One row, edited by the Ultra Admin."""

    __tablename__ = "platform_profile"

    id = db.Column(db.Integer, primary_key=True)
    legal_name = db.Column(db.String(255), nullable=False)
    gstin = db.Column(db.String(15))
    state_code = db.Column(db.String(2), nullable=False)
    address = db.Column(db.Text)
    email = db.Column(db.String(255))
    phone = db.Column(db.String(20))
    upi_vpa = db.Column(db.String(100))
    upi_payee_name = db.Column(db.String(100))
    sac_code = db.Column(db.String(8), nullable=False)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False)
    # Raise each invoice this many days before the renewal date.
    days_before_due = db.Column(db.Integer, nullable=False, default=7, server_default="7")
    invoice_prefix = db.Column(db.String(10), nullable=False, default="SUB", server_default="SUB")


class PlatformSeries(db.Model, TimestampMixin):
    """The firm's own numbering counter per financial year - allocated
    under the same row lock as every client series (app.utils.numbering)."""

    __tablename__ = "platform_series"
    __table_args__ = (db.UniqueConstraint("financial_year", "prefix", name="uq_platform_series_fy_prefix"),)

    id = db.Column(db.Integer, primary_key=True)
    financial_year = db.Column(db.String(7), nullable=False)
    prefix = db.Column(db.String(10), nullable=False)
    next_number = db.Column(db.Integer, nullable=False, default=1)


class PlatformInvoiceStatus(str, enum.Enum):
    ISSUED = "issued"
    PAID = "paid"
    VOID = "void"


class PlatformInvoice(db.Model, TimestampMixin):
    __tablename__ = "platform_invoices"
    __table_args__ = (
        db.UniqueConstraint("number", name="uq_platform_invoices_number"),
        # One live invoice per subscriber per period (a voided one can be
        # raised again).
        db.Index("uq_platform_invoices_tenant_period", "tenant_id", "period_start", unique=True,
                 postgresql_where=db.text("tenant_id IS NOT NULL AND status <> 'VOID'")),
        db.Index("uq_platform_invoices_auditor_period", "auditor_id", "period_start", unique=True,
                 postgresql_where=db.text("auditor_id IS NOT NULL AND status <> 'VOID'")),
    )

    id = db.Column(db.Integer, primary_key=True)
    # Exactly one of these: the client billed, or the Auditor office billed.
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", name="fk_platform_invoices_tenant"), index=True)
    auditor_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_platform_invoices_auditor"), index=True)
    series_id = db.Column(db.Integer, db.ForeignKey("platform_series.id", name="fk_platform_invoices_series"),
                          nullable=False)
    number = db.Column(db.String(20), nullable=False)
    invoice_date = db.Column(db.Date, nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    billing_cycle = db.Column(db.String(10), nullable=False)
    seller = db.Column(db.JSON, nullable=False)  # the firm's details as printed
    bill_to = db.Column(db.JSON, nullable=False)  # name, gstin, state_code, address, email
    place_of_supply = db.Column(db.String(2), nullable=False)
    sac_code = db.Column(db.String(8), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    gst_rate = db.Column(db.Numeric(5, 2), nullable=False)
    taxable_value = db.Column(db.Numeric(12, 2), nullable=False)
    cgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    sgst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    igst = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    round_off = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    grand_total = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.Enum(PlatformInvoiceStatus, name="platform_invoice_status"),
                       nullable=False, default=PlatformInvoiceStatus.ISSUED)
    paid_at = db.Column(db.DateTime(timezone=True))
    paid_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_platform_invoices_paid_by"))
    payment_reference = db.Column(db.String(100))
    voided_at = db.Column(db.DateTime(timezone=True))
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_platform_invoices_voided_by"))
    void_reason = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_platform_invoices_created_by"))

    tenant = db.relationship("Tenant")
    auditor = db.relationship("User", foreign_keys=[auditor_id])
    series = db.relationship("PlatformSeries")
    paid_by = db.relationship("User", foreign_keys=[paid_by_id])
    voided_by = db.relationship("User", foreign_keys=[voided_by_id])

    @property
    def subscriber_name(self) -> str:
        return (self.bill_to or {}).get("name", "")
