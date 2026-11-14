import os
import time

import pytest

# The app runs on Indian time (app._set_process_clock). Set the same clock
# before any test module reads today's date at import, or between midnight
# and 5:30 AM in India a test's "today" is a day behind the app's.
os.environ["TZ"] = os.environ.get("APP_TIMEZONE_POSIX", "IST-5:30")
if hasattr(time, "tzset"):
    time.tzset()

from app import create_app  # noqa: E402
from app.config import TestingConfig  # noqa: E402
from app.extensions import db as _db  # noqa: E402
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402


@pytest.fixture()
def app():
    application = create_app(TestingConfig)
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def super_admin(db):
    user = User(name="Firm Admin", email="firm@example.com", role=UserRole.SUPER_ADMIN, must_change_password=False)
    user.set_password("SuperSecret123")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def tenant(db, super_admin):
    t = Tenant(
        legal_name="Acme Traders",
        registration_type=RegistrationType.REGULAR,
        onboarded_by_id=super_admin.id,
        # Already-verified, like every tenant that existed before the
        # Auditor/verification workflow - not pending.
        billing_cycle=BillingCycle.MONTHLY,
    )
    db.session.add(t)
    db.session.flush()
    gstin = Gstin(
        tenant_id=t.id, gstin="27AAAAA0000A1Z5", state_code="27", state_name="Maharashtra", is_primary=True
    )
    db.session.add(gstin)
    db.session.commit()
    return t


@pytest.fixture()
def client_admin(db, tenant):
    user = User(
        tenant_id=tenant.id,
        name="Client Admin",
        email="admin@acme.example.com",
        role=UserRole.CLIENT_ADMIN,
        must_change_password=False,
    )
    user.set_password("ClientSecret123")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def staff(db, tenant):
    user = User(
        tenant_id=tenant.id,
        name="Cashier",
        email="cashier@acme.example.com",
        role=UserRole.STAFF,
        must_change_password=False,
    )
    user.set_password("CashierSecret123")
    db.session.add(user)
    db.session.commit()
    return user
