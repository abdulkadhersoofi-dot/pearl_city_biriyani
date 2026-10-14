from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin, utcnow


class CashSession(db.Model, TenantScopedMixin, TimestampMixin):
    """One counter's trading day - only used once a client switches on
    day open/close (feature "day_close"). A counter is one login: a
    branch, or the owner billing from their own POS screen.

    Opened with the cash in the drawer, closed with a counted total; the
    difference against what should be there (opening + cash sales - cash
    refunds + cash in - cash out) is kept for the owner. Closing locks the
    day: its bills can no longer be voided, and returns against them are
    recorded in a later day.
    """

    __tablename__ = "cash_sessions"
    __table_args__ = (
        # At most one open day per counter.
        db.Index(
            "uq_cash_sessions_open_per_counter",
            "tenant_id",
            "user_id",
            unique=True,
            postgresql_where=db.text("closed_at IS NULL"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_cash_sessions_user"), nullable=False, index=True)
    business_date = db.Column(db.Date, nullable=False)
    opened_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    opening_cash = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    closed_at = db.Column(db.DateTime(timezone=True))
    closed_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_cash_sessions_closed_by"))
    counted_cash = db.Column(db.Numeric(12, 2))
    expected_cash = db.Column(db.Numeric(12, 2))
    difference = db.Column(db.Numeric(12, 2))
    closing_note = db.Column(db.Text)

    user = db.relationship("User", foreign_keys=[user_id])
    closed_by = db.relationship("User", foreign_keys=[closed_by_id])
    movements = db.relationship(
        "CashMovement", backref="session", cascade="all, delete-orphan", order_by="CashMovement.id"
    )

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def __repr__(self):
        return f"<CashSession {self.id} user={self.user_id} {self.business_date}>"


class CashMovement(db.Model, TenantScopedMixin):
    """Cash put into or taken out of the drawer that isn't a sale - change
    float topped up, petty cash paid out (tea, courier), cash sent to the
    bank. Never edited or deleted; a mistake is corrected by an opposite
    entry."""

    __tablename__ = "cash_movements"

    IN = "in"
    OUT = "out"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer, db.ForeignKey("cash_sessions.id", name="fk_cash_movements_session"), nullable=False, index=True
    )
    kind = db.Column(db.String(3), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    reason = db.Column(db.String(255), nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_cash_movements_created_by"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    created_by = db.relationship("User")
