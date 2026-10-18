"""The kitchen screen (ARFA Counter app): when the kitchen marked a KOT
ready, and who.

Revision ID: c2f4a8e6b913
Revises: b7e1c93d5a20
Create Date: 2026-10-18 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c2f4a8e6b913'
down_revision = 'b7e1c93d5a20'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('kitchen_tickets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ready_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('ready_by_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_kitchen_tickets_ready_by', 'users', ['ready_by_id'], ['id'])


def downgrade():
    with op.batch_alter_table('kitchen_tickets', schema=None) as batch_op:
        batch_op.drop_constraint('fk_kitchen_tickets_ready_by', type_='foreignkey')
        batch_op.drop_column('ready_by_id')
        batch_op.drop_column('ready_at')
