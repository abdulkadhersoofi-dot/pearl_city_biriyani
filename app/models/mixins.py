from datetime import datetime, timezone

from sqlalchemy.ext.declarative import declared_attr

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class TenantScopedMixin:
    """Attach to every table that holds a single tenant's business data.

    A tenant_id foreign key plus a composite index is what keeps a client's
    data invisible to every other client at the query layer.
    """

    @declared_attr
    def tenant_id(cls):
        return db.Column(
            db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True
        )
