import enum

from app.extensions import db
from app.models.mixins import TimestampMixin


class RegistrationType(str, enum.Enum):
    REGULAR = "regular"
    COMPOSITION = "composition"
    UNREGISTERED = "unregistered"


class BillingCycle(str, enum.Enum):
    MONTHLY = "monthly"
    YEARLY = "yearly"


class Tenant(db.Model, TimestampMixin):
    """A client business of the CA firm. One shared database, tenant_id everywhere."""

    __tablename__ = "tenants"

    id = db.Column(db.Integer, primary_key=True)
    legal_name = db.Column(db.String(255), nullable=False)
    trade_name = db.Column(db.String(255))
    registration_type = db.Column(
        db.Enum(RegistrationType, name="registration_type"), nullable=False
    )
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # use_alter breaks the tenants<->users circular FK dependency: this
    # constraint is added via a separate ALTER TABLE after both tables exist.
    onboarded_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", use_alter=True, name="fk_tenants_onboarded_by")
    )
    # Which Auditor (or Sub-Auditor) this client is currently allocated to -
    # null means unallocated (Ultra-Admin-onboarded and not yet handed off,
    # or a client who left their auditor). Only the Ultra Admin can set or
    # change this (tenants.reassign_auditor); an Auditor allocating one of
    # their own clients to their own Sub-Auditor also writes it, but only
    # within tenants already scoped to them - see app.utils.auditor_scope.
    auditor_id = db.Column(
        db.Integer, db.ForeignKey("users.id", use_alter=True, name="fk_tenants_auditor"), index=True
    )

    # Branding: shown instead of the platform's own name/logo wherever a
    # Client Admin or Staff user is signed in - the nav, invoice PDFs, POS
    # receipts, and (via login_slug) this client's own login page. Stored
    # in uploaded_images (database row, not a disk path) so it survives a
    # container restart/redeploy - see app/models/media.py.
    logo_image_id = db.Column(db.Integer, db.ForeignKey("uploaded_images.id"))
    # Site theme, each independently settable from Settings - see
    # app/utils/theme.py for how these four turn into CSS.
    primary_color = db.Column(db.String(7), nullable=False, default="#1f7a4d")  # buttons/links
    background_color = db.Column(db.String(7), nullable=False, default="#f6f7f5")  # page background
    text_color = db.Column(db.String(7), nullable=False, default="#1f2a24")  # body text on that background
    font_family = db.Column(db.String(20), nullable=False, default="system")
    login_slug = db.Column(db.String(64), unique=True)
    # 2in / 3in / a4 - app.pos.receipt.THERMAL_WIDTHS_IN keys, plus "a4".
    # Lets POS checkout print immediately without asking every time.
    default_receipt_format = db.Column(db.String(10), nullable=False, default="3in")

    # Access gating - see app.utils.billing.tenant_access_status, which
    # combines these with `is_active` (above) into one go/no-go check,
    # enforced both at login and on every subsequent request. Nullable:
    # a tenant onboarded before this feature existed has neither set, and
    # is never blocked on either criterion until a Super Admin opts it in
    # (set directly, or implicitly via the first "mark paid" action) -
    # nobody already using the app gets silently cut off by a migration.
    #
    # A hard subscription end date - only the Ultra Admin can move it
    # forward (see tenants.renew_access), and only ever sets it at
    # verification time (tenants.verify_client) or renewal, never at
    # onboarding - stored nullable since an existing client has none
    # until one is set.
    valid_until = db.Column(db.Date)
    # The next renewal date in the separate, recurring billing cycle -
    # independent of valid_until. A monthly or yearly reminder depending
    # on `billing_cycle` below. The Ultra Admin's "mark this period paid"
    # (tenants.mark_billing_paid) advances it by one month or one year
    # from its current value; nothing else moves it. See app.utils.billing
    # for the grace window this is checked against.
    next_billing_due = db.Column(db.Date)
    # Set once by the Ultra Admin at verification (tenants.verify_client) -
    # MONTHLY or YEARLY. Null means "pending verification": a client
    # created by anyone (Ultra Admin or Auditor) starts with no billing
    # cycle at all and cannot be signed into until the Ultra Admin
    # verifies it and picks one. This selection always stays with the
    # Ultra Admin, even when an Auditor created the client.
    billing_cycle = db.Column(db.Enum(BillingCycle, name="billing_cycle"))
    # The anchor the recurring cycle counts from. Deliberately NOT set at
    # onboarding or verification - only on the Client Admin's first
    # successful login after changing the initial password Ultra Admin
    # set for them (see app.auth.routes.change_password). Null until then,
    # even for an already-verified, active tenant.
    cycle_anchor_date = db.Column(db.Date)
    # The Ultra Admin's manual override of the billing-due notice banner -
    # ring it on command regardless of where the calendar cycle actually
    # is, or silence a notice that's already ringing. Does not by itself
    # block access; pause (`is_active`) still does that.
    manual_alarm_active = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    gstins = db.relationship(
        "Gstin", backref="tenant", cascade="all, delete-orphan", lazy="dynamic"
    )
    users = db.relationship(
        "User",
        backref="tenant",
        foreign_keys="User.tenant_id",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    auditor = db.relationship(
        "User", foreign_keys=[auditor_id], backref=db.backref("allocated_tenants", lazy="select")
    )

    @property
    def display_name(self) -> str:
        return self.trade_name or self.legal_name

    def __repr__(self):
        return f"<Tenant {self.id} {self.legal_name}>"


class Gstin(db.Model, TimestampMixin):
    """A tenant's place of business in a given state. A tenant may hold more
    than one (e.g. one per state it operates in).

    `gstin` is nullable: an UNREGISTERED tenant (below the GST threshold, or
    simply not registered) still has a real business location - a state it
    operates from, maybe an address - it just has no GSTIN number to show on
    it. Postgres treats multiple NULLs as distinct, so the unique constraint
    on `gstin` still holds across any number of unregistered tenants.
    """

    __tablename__ = "gstins"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(
        db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True
    )
    gstin = db.Column(db.String(15), unique=True)
    state_code = db.Column(db.String(2), nullable=False)
    state_name = db.Column(db.String(64), nullable=False)
    registered_address = db.Column(db.Text)
    is_primary = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def display_label(self) -> str:
        return self.gstin or f"Unregistered - {self.state_name}"

    def __repr__(self):
        return f"<Gstin {self.gstin or 'unregistered'}>"
