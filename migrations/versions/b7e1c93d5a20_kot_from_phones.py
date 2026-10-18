"""KOTs sent from the ARFA Counter app: the phone's id for the ticket (sent
once, even if the phone sends it again) and the number the phone printed
on the kitchen slip while offline.

Revision ID: b7e1c93d5a20
Revises: a4d9e2f71c58
Create Date: 2026-10-18 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b7e1c93d5a20'
down_revision = 'a4d9e2f71c58'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('kitchen_tickets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('client_uuid', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('device_ref', sa.String(length=20), nullable=True))
        batch_op.create_index('uq_kitchen_tickets_tenant_uuid', ['tenant_id', 'client_uuid'], unique=True)


def downgrade():
    with op.batch_alter_table('kitchen_tickets', schema=None) as batch_op:
        batch_op.drop_index('uq_kitchen_tickets_tenant_uuid')
        batch_op.drop_column('device_ref')
        batch_op.drop_column('client_uuid')
