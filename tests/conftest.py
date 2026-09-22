import pytest

from app import create_app
from app.config import TestingConfig
from app.extensions import db as _db
from app.models.tenant import Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole


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
