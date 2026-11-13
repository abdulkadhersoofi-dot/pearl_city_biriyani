"""Purchase orders billed in parts: how much of each order line is billed,
which order line a bill line settles, and a "partly billed" state.

Revision ID: d5a9c3e7f842
Revises: c8e4b2f6a731
Create Date: 2026-11-14 01:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd5a9c3e7f842'
down_revision = 'c8e4b2f6a731'
branch_labels = None
depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE purchase_order_status ADD VALUE IF NOT EXISTS 'PARTLY'")
    with op.batch_alter_table('purchase_order_lines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('billed_qty', sa.Numeric(12, 3), server_default='0', nullable=False))
    with op.batch_alter_table('purchase_bill_lines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('order_line_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_purchase_bill_lines_order_line_id', ['order_line_id'])
        batch_op.create_foreign_key('fk_purchase_bill_lines_order_line', 'purchase_order_lines',
                                    ['order_line_id'], ['id'])
    # Orders already closed by a bill were billed whole, the only way there was.
    op.execute("""
        UPDATE purchase_order_lines SET billed_qty = qty
        WHERE order_id IN (SELECT id FROM purchase_orders WHERE status = 'CONVERTED')
    """)


def downgrade():
    op.execute("UPDATE purchase_orders SET status = 'OPEN' WHERE status = 'PARTLY'")
    with op.batch_alter_table('purchase_bill_lines', schema=None) as batch_op:
        batch_op.drop_constraint('fk_purchase_bill_lines_order_line', type_='foreignkey')
        batch_op.drop_index('ix_purchase_bill_lines_order_line_id')
        batch_op.drop_column('order_line_id')
    with op.batch_alter_table('purchase_order_lines', schema=None) as batch_op:
        batch_op.drop_column('billed_qty')
