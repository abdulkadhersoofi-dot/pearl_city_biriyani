import enum

from app.extensions import db
from app.models.mixins import TimestampMixin


class RegistrationType(str, enum.Enum):
    REGULAR = "regular"
    COMPOSITION = "composition"
    UNREGISTERED = "unregistered"


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

    # Branding: shown instead of the platform's own name/logo wherever a
    # Client Admin or Staff user is signed in - the nav, invoice PDFs, POS
    # receipts, and (via login_slug) this client's own login page.
    logo_path = db.Column(db.String(255))  # relative to app/static/, e.g. "uploads/logos/xyz.png"
    # Site theme, each independently settable from Settings - see
    # app/utils/theme.py for how these three turn into CSS.
    primary_color = db.Column(db.String(7), nullable=False, default="#1f7a4d")  # buttons/links
    background_color = db.Column(db.String(7), nullable=False, default="#f6f7f5")  # page background
    font_family = db.Column(db.String(20), nullable=False, default="system")
    login_slug = db.Column(db.String(64), unique=True)
    # 2in / 3in / a4 - app.pos.receipt.THERMAL_WIDTHS_IN keys, plus "a4".
    # Lets POS checkout print immediately without asking every time.
    default_receipt_format = db.Column(db.String(10), nullable=False, default="3in")

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
