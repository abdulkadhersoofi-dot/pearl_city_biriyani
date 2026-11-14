"""The ARFA Scan phone app: its sign-in tokens, and what the phone read.

Revision ID: f6a9d4c2e813
Revises: e5f8c3b1d702
Create Date: 2026-11-03 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f6a9d4c2e813'
down_revision = 'e5f8c3b1d702'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'scan_app_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('device_name', sa.String(length=120), nullable=True),
        sa.Column('password_stamp', sa.String(length=16), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_scan_app_tokens_tenant'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_scan_app_tokens_user'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash', name='uq_scan_app_tokens_token_hash'),
    )
    op.create_index('ix_scan_app_tokens_tenant_id', 'scan_app_tokens', ['tenant_id'])
    op.create_index('ix_scan_app_tokens_user_id', 'scan_app_tokens', ['user_id'])
    with op.batch_alter_table('scan_documents') as batch:
        batch.add_column(sa.Column('phone_read', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('scan_documents') as batch:
        batch.drop_column('phone_read')
    op.drop_index('ix_scan_app_tokens_user_id', table_name='scan_app_tokens')
    op.drop_index('ix_scan_app_tokens_tenant_id', table_name='scan_app_tokens')
    op.drop_table('scan_app_tokens')
