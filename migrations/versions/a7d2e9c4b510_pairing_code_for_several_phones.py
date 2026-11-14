"""A pairing code may pair several phones of one branch: max_uses (how many
the owner asked for) and uses (how many have paired with it so far).
Existing codes stay one-phone codes.

Revision ID: a7d2e9c4b510
Revises: f1b3d5a7c920
Create Date: 2026-11-13 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a7d2e9c4b510'
down_revision = 'f1b3d5a7c920'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('device_pairings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('max_uses', sa.Integer(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('uses', sa.Integer(), nullable=False, server_default='0'))
    op.execute("UPDATE device_pairings SET uses = 1 WHERE used_at IS NOT NULL")


def downgrade():
    with op.batch_alter_table('device_pairings', schema=None) as batch_op:
        batch_op.drop_column('uses')
        batch_op.drop_column('max_uses')
