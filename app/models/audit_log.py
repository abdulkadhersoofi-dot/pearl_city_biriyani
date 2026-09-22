from app.extensions import db
from app.models.mixins import utcnow


class AuditLog(db.Model):
    """Immutable trail of who did what, when, and for which client.

    Written for Super Admin cross-tenant access (mandatory), and reused for
    security-relevant events (logins, password resets, voids) across all
    roles.
    """

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    actor_role = db.Column(db.String(20))
    # Tenant the action was performed against/for - not necessarily the
    # actor's own tenant (e.g. Super Admin viewing a client).
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"), index=True)

    action = db.Column(db.String(64), nullable=False, index=True)
    entity_type = db.Column(db.String(64))
    entity_id = db.Column(db.Integer)
    details = db.Column(db.JSON)
    ip_address = db.Column(db.String(45))

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    actor = db.relationship("User")
    tenant = db.relationship("Tenant")

    def __repr__(self):
        return f"<AuditLog {self.action} tenant={self.tenant_id} actor={self.actor_user_id}>"
