import enum

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask_login import UserMixin

from app.extensions import db
from app.models.mixins import TimestampMixin

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
