"""Seeds a demo tenant with a GSTIN, a Client Admin login, a couple of
products and a customer - useful for trying the Invoicing module locally
without going through the onboarding form by hand.

Usage:
    export FLASK_APP=wsgi.py
    flask shell < scripts/seed_demo.py
or:
    python -m scripts.seed_demo
"""

from app import create_app
from app.extensions import db
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole

DEMO_ADMIN_EMAIL = "demo-admin@example.com"
DEMO_ADMIN_PASSWORD = "DemoPass123!"


def run():
    if User.query.filter_by(email=DEMO_ADMIN_EMAIL).first():
        print(f"Demo tenant already seeded ({DEMO_ADMIN_EMAIL}).")
        return

    tenant = Tenant(legal_name="Demo Traders Pvt Ltd", trade_name="Demo Traders", registration_type=RegistrationType.REGULAR)
    db.session.add(tenant)
    db.session.flush()

    db.session.add(
        Gstin(
            tenant_id=tenant.id,
            gstin="27AAAAA0000A1Z5",
            state_code="27",
            state_name="Maharashtra",
            registered_address="123 Demo Street, Pune",
            is_primary=True,
        )
    )

    admin = User(
        tenant_id=tenant.id,
        name="Demo Admin",
        email=DEMO_ADMIN_EMAIL,
        role=UserRole.CLIENT_ADMIN,
        must_change_password=False,
    )
    admin.set_password(DEMO_ADMIN_PASSWORD)
    db.session.add(admin)

    db.session.add_all([
        Product(tenant_id=tenant.id, name="Chicken Biriyani", hsn_or_sac_code="996331", gst_rate=5, unit="plate", default_price=220),
        Product(tenant_id=tenant.id, name="Mutton Biriyani", hsn_or_sac_code="996331", gst_rate=5, unit="plate", default_price=280),
        Product(tenant_id=tenant.id, name="Delivery charge", hsn_or_sac_code="996812", gst_rate=18, unit="pcs", default_price=40),
    ])
    db.session.add(
        Customer(tenant_id=tenant.id, name="Walk-in Customer", state_code="27", state_name="Maharashtra", city="Pune")
    )

    db.session.commit()
    print(f"Demo tenant ready. Sign in as {DEMO_ADMIN_EMAIL} / {DEMO_ADMIN_PASSWORD}")


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        run()
