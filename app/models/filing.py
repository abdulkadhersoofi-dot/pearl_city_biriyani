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


class BooksCheckRun(db.Model, TenantScopedMixin):
    """One run of the Books check (app/finance/checks.py) for a business
    and a month: whether every figure agreed, and each check's result as it
    was shown. Kept so the last result opens at once and a failure can be
    seen under "Needs you"; the nightly job adds one a night."""

    __tablename__ = "books_check_runs"
    __table_args__ = (db.Index("ix_books_check_runs_tenant_period", "tenant_id", "period", "ran_at"),)

    id = db.Column(db.Integer, primary_key=True)
    period = db.Column(db.String(7), nullable=False)  # "2026-11"
    ran_at = db.Column(db.DateTime(timezone=True), nullable=False)
    ok = db.Column(db.Boolean, nullable=False, default=True)
    failed_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    results = db.Column(db.JSON, nullable=False, default=list)
