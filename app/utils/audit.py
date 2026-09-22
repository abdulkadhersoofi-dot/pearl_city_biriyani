from flask import request

from app.extensions import db
from app.models.audit_log import AuditLog


def record_audit(
    actor,
    action: str,
    tenant_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    details: dict | None = None,
):
    """Writes one immutable audit row. Call this on every cross-tenant
    read/export by Super Admin, every login/password-reset, and every
    void/edit-on-behalf-of-client action.
    """
    log = AuditLog(
        actor_user_id=getattr(actor, "id", None),
        actor_role=getattr(getattr(actor, "role", None), "value", None),
        tenant_id=tenant_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details or {},
        ip_address=request.remote_addr if request else None,
    )
    db.session.add(log)
    return log
