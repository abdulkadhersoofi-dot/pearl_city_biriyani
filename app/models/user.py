import enum
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask_login import UserMixin

from app.extensions import db
from app.models.mixins import TimestampMixin, utcnow

_hasher = PasswordHasher()


class UserRole(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    CLIENT_ADMIN = "client_admin"
    STAFF = "staff"


class User(db.Model, UserMixin, TimestampMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    # Null tenant_id => Super Admin (the firm), not attached to any one client.
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"), index=True)

    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    phone = db.Column(db.String(20))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.Enum(UserRole, name="user_role"), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=True)
    last_login_at = db.Column(db.DateTime(timezone=True))

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

    @property
    def is_super_admin(self) -> bool:
        return self.role == UserRole.SUPER_ADMIN

    @property
    def is_client_admin(self) -> bool:
        return self.role == UserRole.CLIENT_ADMIN

    @property
    def is_staff(self) -> bool:
        return self.role == UserRole.STAFF

    def get_id(self):
        # Flask-Login identity; kept explicit and separate from any JWT subject.
        return str(self.id)

    def __repr__(self):
        return f"<User {self.id} {self.email} {self.role}>"


class PasswordResetOTP(db.Model, TimestampMixin):
    """One-time password for the forgot-password flow. The OTP itself is
    never stored in plaintext - only a hash of it, like a password."""

    __tablename__ = "password_reset_otps"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    otp_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    consumed_at = db.Column(db.DateTime(timezone=True))
    attempts = db.Column(db.Integer, nullable=False, default=0)

    user = db.relationship("User")

    @staticmethod
    def generate_code(length: int) -> str:
        return "".join(secrets.choice("0123456789") for _ in range(length))

    @classmethod
    def issue(cls, user: "User", length: int, expiry_minutes: int) -> tuple["PasswordResetOTP", str]:
        code = cls.generate_code(length)
        otp = cls(
            user_id=user.id,
            otp_hash=_hasher.hash(code),
            expires_at=utcnow() + timedelta(minutes=expiry_minutes),
        )
        db.session.add(otp)
        return otp, code

    def is_expired(self) -> bool:
        return utcnow() > self.expires_at

    def is_usable(self, max_attempts: int) -> bool:
        return (
            self.consumed_at is None
            and not self.is_expired()
            and self.attempts < max_attempts
        )

    def verify(self, code: str) -> bool:
        try:
            return _hasher.verify(self.otp_hash, code)
        except VerifyMismatchError:
            return False
