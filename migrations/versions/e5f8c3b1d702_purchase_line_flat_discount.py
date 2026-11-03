"""Purchase bill lines: a discount in rupees, as printed, besides the
percentage.

Revision ID: e5f8c3b1d702
Revises: d4e7b2a9c1f3
Create Date: 2026-11-03 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e5f8c3b1d702'
down_revision = 'd4e7b2a9c1f3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('purchase_bill_lines') as batch:
        batch.add_column(sa.Column('discount_flat', sa.Numeric(12, 2), nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('purchase_bill_lines') as batch:
        batch.drop_column('discount_flat')
