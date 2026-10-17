"""Packages and powers (Ultra Admin control, stage 1): what each client may
use, and what each auditor may do. The rules that read these live in
app/utils/entitlements.py.

A Package belongs to the firm, not to one client, so it has no tenant_id
(like PlatformProfile). Each client's own package and its exceptions are
in TenantPackage, which does carry the tenant_id. AuditorPowers belongs
to one auditor login, which has no tenant either.
"""

from app.extensions import db
from app.models.mixins import TimestampMixin


class Package(db.Model, TimestampMixin):
    """A bundle the Ultra Admin assigns to a client: each module Off,
    Included or Client's choice; each setting changeable by the client or
    by ARFA only; branch / login / GSTIN limits; own branding; price."""

    __tablename__ = "packages"

    id = db.Column(db.Integer, primary_key=True)
    # Stable handle for the packages ARFA ships ("current", "retail", ...);
    # null for ones the Ultra Admin makes.
    key = db.Column(db.String(40), unique=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    description = db.Column(db.Text)
    # {feature key: "off" | "inc" | "choice"}; a module not listed takes
    # `unlisted_modules` - "off" normally, "choice" for "Current setup" so a
    # module added to the app later stays the client's own choice there.
    modules = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    unlisted_modules = db.Column(db.String(10), nullable=False, default="off", server_default="off")
    # {setting key: "client" | "arfa"}; not listed = "client".
    settings = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    # {"branches": n, "logins": n, "gstins": n}; not listed = no limit.
    limits = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    own_branding = db.Column(db.Boolean, nullable=False, default=True, server_default="true")
    price_monthly = db.Column(db.Numeric(10, 2))
    price_yearly = db.Column(db.Numeric(10, 2))
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default="true")
    sort_order = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    def __repr__(self):
        return f"<Package {self.id} {self.name}>"


class TenantPackage(db.Model, TimestampMixin):
    """One client's package and the Ultra Admin's exceptions for this client
    only. No row means the client was never given a package: everything
    stays as it was before packages existed (every module the client's own
    choice, every setting theirs)."""

    __tablename__ = "tenant_packages"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", name="fk_tenant_packages_tenant"),
                          nullable=False, unique=True, index=True)
    package_id = db.Column(db.Integer, db.ForeignKey("packages.id", name="fk_tenant_packages_package"),
                           nullable=False, index=True)
    # Exceptions for this client, each beating the package:
    # {feature key: "off" | "inc" | "choice"}
    module_overrides = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    # {setting key: "client" | "arfa"}
    setting_overrides = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    # {limit key: n or null (no limit)}
    limit_overrides = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    own_branding_override = db.Column(db.Boolean)
    # Modules taken away while they still held data: their old records stay
    # readable (stage 3 decides when a module lands here).
    read_only_modules = db.Column(db.JSON, nullable=False, default=list, server_default="[]")
    assigned_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_tenant_packages_assigned_by"))

    package = db.relationship("Package")
    tenant = db.relationship("Tenant", backref=db.backref("package_grant", uselist=False))

    def __repr__(self):
        return f"<TenantPackage tenant={self.tenant_id} package={self.package_id}>"


class AuditorPowers(db.Model, TimestampMixin):
    """What one Auditor or Sub-Auditor may do, set by the Ultra Admin (or,
    for a Sub-Auditor, by their Auditor within their own powers). No row
    means the role's powers as they were before this existed."""

    __tablename__ = "auditor_powers"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_auditor_powers_user"),
                        nullable=False, unique=True, index=True)
    # {power key: true | false}; not listed = the role's default.
    powers = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    # Packages this auditor may give their clients (stage 4).
    allowed_package_ids = db.Column(db.JSON, nullable=False, default=list, server_default="[]")
    max_clients = db.Column(db.Integer)
    max_sub_auditors = db.Column(db.Integer)
    set_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_auditor_powers_set_by"))

    user = db.relationship("User", foreign_keys=[user_id], backref=db.backref("power_grant", uselist=False))

    def __repr__(self):
        return f"<AuditorPowers user={self.user_id}>"


class ModuleRequest(db.Model, TimestampMixin):
    """A client asking ARFA to add a module that isn't in their package
    ("Ask ARFA to add" in Settings > Modules). Lands in the Ultra Admin's
    bell; one open request per module per client."""

    __tablename__ = "module_requests"

    PENDING, ADDED, DECLINED = "pending", "added", "declined"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", name="fk_module_requests_tenant"),
                          nullable=False, index=True)
    feature_key = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(10), nullable=False, default="pending", server_default="pending", index=True)
    requested_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_module_requests_requested_by"))
    resolved_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_module_requests_resolved_by"))
    resolved_at = db.Column(db.DateTime(timezone=True))

    tenant = db.relationship("Tenant")
    requested_by = db.relationship("User", foreign_keys=[requested_by_id])

    def __repr__(self):
        return f"<ModuleRequest tenant={self.tenant_id} {self.feature_key} {self.status}>"
