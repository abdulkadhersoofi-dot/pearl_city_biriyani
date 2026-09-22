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

    def __repr__(self):
        return f"<Tenant {self.id} {self.legal_name}>"


class Gstin(db.Model, TimestampMixin):
    """A GSTIN registration held by a tenant. A tenant may hold more than one
    (e.g. one per state it operates in)."""

    __tablename__ = "gstins"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(
        db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True
    )
    gstin = db.Column(db.String(15), nullable=False, unique=True)
    state_code = db.Column(db.String(2), nullable=False)
    state_name = db.Column(db.String(64), nullable=False)
    registered_address = db.Column(db.Text)
    is_primary = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self):
        return f"<Gstin {self.gstin}>"
