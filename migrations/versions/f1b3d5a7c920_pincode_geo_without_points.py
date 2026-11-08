"""The pincode table may hold pincodes without a point (India Post's 2019
directory has district and state, no coordinates): lat/lng nullable.

Revision ID: f1b3d5a7c920
Revises: e2a7c9d4b318
Create Date: 2026-11-09 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f1b3d5a7c920'
down_revision = 'e2a7c9d4b318'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('pincode_geo', schema=None) as batch_op:
        batch_op.alter_column('lat', existing_type=sa.Float(), nullable=True)
        batch_op.alter_column('lng', existing_type=sa.Float(), nullable=True)


def downgrade():
    op.execute("DELETE FROM pincode_geo WHERE lat IS NULL OR lng IS NULL")
    with op.batch_alter_table('pincode_geo', schema=None) as batch_op:
        batch_op.alter_column('lng', existing_type=sa.Float(), nullable=False)
        batch_op.alter_column('lat', existing_type=sa.Float(), nullable=False)
