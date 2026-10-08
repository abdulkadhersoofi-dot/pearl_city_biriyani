import enum

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask_login import UserMixin

from app.extensions import db
from app.models.mixins import TimestampMixin
from app.models.tenant import BillingCycle

_hasher = PasswordHasher()


class UserRole(str, enum.Enum):
    # Stored value kept as "super_admin" for DB/enum stability - displayed
    # everywhere in the UI as "Ultra Admin" (see ROLE_LABELS below). The
    # one account above the whole Auditor hierarchy: unrestricted access
    # to every tenant, every auditor, and the only role that can set a
    # tenant's billing cycle or verify a pending client.
    SUPER_ADMIN = "super_admin"
    # Same operational power as Ultra Admin (create/pause/act-as clients,
    # create branches via act-as) but scoped to only the tenants allocated
    # to them (app.utils.auditor_scope.auditor_tenant_ids) - and never the
    # billing cycle, which always stays with the Ultra Admin.
    AUDITOR = "auditor"
    # Created by an Auditor "within their office" - scoped to only the
    # tenants that Auditor explicitly allocates to them (never their
    # parent's full pool). parent_auditor_id names the Auditor who created
    # them.
    SUB_AUDITOR = "sub_auditor"
    CLIENT_ADMIN = "client_admin"
    STAFF = "staff"


# Display label only - never the stored enum value, so the Postgres enum
# type and every `role ==` comparison in the codebase stay untouched.
ROLE_LABELS = {
    UserRole.SUPER_ADMIN: "Ultra Admin",
    UserRole.AUDITOR: "Auditor",
    UserRole.SUB_AUDITOR: "Sub-Auditor",
    UserRole.CLIENT_ADMIN: "Client Admin",
    UserRole.STAFF: "Branch",
}


class BranchType(str, enum.Enum):
    """Only meaningful for a Staff (branch) login - see app/pos/stock.py
    and app/invoicing/services.py. An OWNED branch is part of the kitchen
    tenant itself, so stock sent to it is an internal transfer (Delivery
    Challan, no GST); a THIRD_PARTY branch is an independent buyer who
    resells the stock, so it's billed as a real sale (Tax Invoice, GST
    applies)."""

    OWNED = "owned"
    THIRD_PARTY = "third_party"


class User(db.Model, UserMixin, TimestampMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    # Null tenant_id => an admin-hierarchy login (Ultra Admin, Auditor or
    # Sub-Auditor), not attached to any one client.
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"), index=True)
    # Only set for role=SUB_AUDITOR - the Auditor who created this
    # Sub-Auditor account. Scopes the Sub-Auditor to only whichever of
    # that Auditor's own tenants get explicitly allocated to them (see
    # app.utils.auditor_scope).
    parent_auditor_id = db.Column(db.Integer, db.ForeignKey("users.id", use_alter=True, name="fk_users_parent_auditor"), index=True)

    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    phone = db.Column(db.String(20))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.Enum(UserRole, name="user_role"), nullable=False)
    # Only set for role=STAFF (a branch login) - see BranchType above.
    branch_type = db.Column(db.Enum(BranchType, name="branch_type"))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=True)
    last_login_at = db.Column(db.DateTime(timezone=True))

    # Billing for an Auditor/Sub-Auditor login - the same system a Tenant
    # has (see app.models.tenant.Tenant and app.utils.billing), just
    # billing the Auditor's own office instead of a client. Null/unused
    # for Ultra Admin, Client Admin and Staff. Null billing_cycle means
    # "pending verification" - a newly created Auditor/Sub-Auditor, by
    # the Ultra Admin or by another Auditor, can't sign in until the
    # Ultra Admin sets one (tenants.set_auditor_billing_cycle).
    billing_cycle = db.Column(db.Enum(BillingCycle, name="billing_cycle"))
    # Anchor for the recurring cycle - stamped only on this Auditor's own
    # first successful login after changing their initial password (see
    # app.auth.routes.change_password), exactly like a Client Admin's
    # tenant.
    cycle_anchor_date = db.Column(db.Date)
    next_billing_due = db.Column(db.Date)
    manual_alarm_active = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    def set_password(self, raw_password: str) -> None:
        self.password_hash = _hasher.hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        try:
            valid = _hasher.verify(self.password_hash, raw_password)
        except VerifyMismatchError:
            return False
        if valid and _hasher.check_needs_rehash(self.password_hash):
            self.password_hash = _hasher.hash(raw_password)
        return valid

    sub_auditors = db.relationship(
        "User",
        backref=db.backref("parent_auditor", remote_side="User.id"),
        foreign_keys=[parent_auditor_id],
    )

    @property
    def is_super_admin(self) -> bool:
        return self.role == UserRole.SUPER_ADMIN

    @property
    def is_auditor(self) -> bool:
        return self.role == UserRole.AUDITOR

    @property
    def is_sub_auditor(self) -> bool:
        return self.role == UserRole.SUB_AUDITOR

    @property
    def is_admin_hierarchy(self) -> bool:
        """True for any of the three roles that manage clients rather than
        being one (Ultra Admin, Auditor, Sub-Auditor) - the shared admin
        console is reachable by all three, scoped differently per role."""
        return self.role in (UserRole.SUPER_ADMIN, UserRole.AUDITOR, UserRole.SUB_AUDITOR)

    @property
    def is_client_admin(self) -> bool:
        return self.role == UserRole.CLIENT_ADMIN

    @property
    def is_staff(self) -> bool:
        return self.role == UserRole.STAFF

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role.value)

    def get_id(self):
        # Flask-Login identity; kept explicit and separate from any JWT subject.
        return str(self.id)

    def __repr__(self):
        return f"<User {self.id} {self.email} {self.role}>"
