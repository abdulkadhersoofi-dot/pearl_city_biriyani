"""ARFA Counter app, stage 1: one-time pairing codes that sign a phone in
as a branch login (device_pairings). No existing data changes.

Revision ID: a4d9e2f71c58
Revises: f3c8d2a6b417
Create Date: 2026-10-18 14:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'a4d9e2f71c58'
down_revision = 'f3c8d2a6b417'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'device_pairings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('device_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_device_pairings_user'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_device_pairings_created_by'),
        sa.ForeignKeyConstraint(['device_id'], ['counter_devices.id'], name='fk_device_pairings_device'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_device_pairings_tenant_id', 'device_pairings', ['tenant_id'])
    op.create_index('ix_device_pairings_user_id', 'device_pairings', ['user_id'])
    op.create_index('ix_device_pairings_code_hash', 'device_pairings', ['code_hash'])


def downgrade():
    op.drop_index('ix_device_pairings_code_hash', table_name='device_pairings')
    op.drop_index('ix_device_pairings_user_id', table_name='device_pairings')
    op.drop_index('ix_device_pairings_tenant_id', table_name='device_pairings')
    op.drop_table('device_pairings')
