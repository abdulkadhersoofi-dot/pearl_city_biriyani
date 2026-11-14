"""Purchase bills: TDS deducted, and the bill's other details (references,
bank details, per-line serial numbers / batch / item codes).

Revision ID: d4e7b2a9c1f3
Revises: c3f1a9d2e604
Create Date: 2026-11-02 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd4e7b2a9c1f3'
down_revision = 'c3f1a9d2e604'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('purchase_bills') as batch:
        batch.add_column(sa.Column('tds_amount', sa.Numeric(12, 2), nullable=False, server_default='0'))
        batch.add_column(sa.Column('tds_section', sa.String(length=10), nullable=True))
        batch.add_column(sa.Column('details', sa.JSON(), nullable=True))
    with op.batch_alter_table('purchase_bill_lines') as batch:
        batch.add_column(sa.Column('details', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('purchase_bill_lines') as batch:
        batch.drop_column('details')
    with op.batch_alter_table('purchase_bills') as batch:
        batch.drop_column('details')
        batch.drop_column('tds_section')
        batch.drop_column('tds_amount')
