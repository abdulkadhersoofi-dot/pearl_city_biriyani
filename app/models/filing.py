"""GST return filing tracker (Phase 7): one row per client GSTIN, month
and return. The client marks their month's data ready; the firm (Ultra
Admin / Auditor) marks the return filed, with the date and ARN. Due dates
come from app.utils.gst_config, never stored."""

import enum

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin


class ReturnType(str, enum.Enum):
    GSTR1 = "gstr1"
    GSTR3B = "gstr3b"


RETURN_LABELS = {ReturnType.GSTR1: "GSTR-1", ReturnType.GSTR3B: "GSTR-3B"}


class FilingStatus(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "filing_statuses"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "gstin_id", "period", "return_type", name="uq_filing_statuses_return"),
    )

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_filing_statuses_gstin"), nullable=False)
    period = db.Column(db.String(7), nullable=False)  # "2026-09"
    return_type = db.Column(db.Enum(ReturnType, name="filing_return_type"), nullable=False)
    data_ready = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    data_ready_at = db.Column(db.DateTime(timezone=True))
    data_ready_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_filing_statuses_ready_by"))
    filed = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    filed_on = db.Column(db.Date)
    arn = db.Column(db.String(30))
    filed_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_filing_statuses_filed_by"))

    gstin = db.relationship("Gstin")
