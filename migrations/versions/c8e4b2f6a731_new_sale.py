"""New sale: one reference shared by a sale's invoice and its order(s), and
a sales order that is waiting for stock.

Revision ID: c8e4b2f6a731
Revises: b3f7a1d9c642
Create Date: 2026-11-13 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c8e4b2f6a731'
down_revision = 'b3f7a1d9c642'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sales_documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sale_ref', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('awaiting_stock', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.create_index('ix_sales_documents_sale_ref', ['sale_ref'])
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sale_ref', sa.String(length=16), nullable=True))
        batch_op.create_index('ix_invoices_sale_ref', ['sale_ref'])


def downgrade():
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_index('ix_invoices_sale_ref')
        batch_op.drop_column('sale_ref')
    with op.batch_alter_table('sales_documents', schema=None) as batch_op:
        batch_op.drop_index('ix_sales_documents_sale_ref')
        batch_op.drop_column('awaiting_stock')
        batch_op.drop_column('sale_ref')
