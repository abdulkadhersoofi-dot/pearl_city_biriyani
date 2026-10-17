"""Who a package is for (console cleanup, stage 4): its business type
(a BusinessType value; empty = any business). ARFA's starting packages get
the type they were made for; nothing about what any client has changes.

Revision ID: f3c8d2a6b417
Revises: e7a1c5d9b234
Create Date: 2026-10-18 10:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'f3c8d2a6b417'
down_revision = 'e7a1c5d9b234'
branch_labels = None
depends_on = None

STARTING = {"retail": "retail", "restaurant": "restaurant", "pharmacy": "pharmacy",
            "electronics": "mobile_electronics", "wholesale": "wholesale"}


def upgrade():
    op.add_column('packages', sa.Column('business_type', sa.String(length=30), nullable=True))
    packages = sa.table('packages', sa.column('key', sa.String), sa.column('business_type', sa.String))
    for key, business_type in STARTING.items():
        op.execute(packages.update().where(packages.c.key == key).values(business_type=business_type))


def downgrade():
    op.drop_column('packages', 'business_type')
