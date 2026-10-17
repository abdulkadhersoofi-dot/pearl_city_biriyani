"""Packages and powers, stage 3: clients asking ARFA to add a module
("Ask ARFA to add" in Settings > Modules). No data changes.

Revision ID: c4e8a1f07d52
Revises: a7c3f9d21b40
Create Date: 2026-10-17 14:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'c4e8a1f07d52'
down_revision = 'a7c3f9d21b40'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('module_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('feature_key', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='pending', nullable=False),
    sa.Column('requested_by_id', sa.Integer(), nullable=True),
    sa.Column('resolved_by_id', sa.Integer(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_module_requests_tenant'),
    sa.ForeignKeyConstraint(['requested_by_id'], ['users.id'], name='fk_module_requests_requested_by'),
    sa.ForeignKeyConstraint(['resolved_by_id'], ['users.id'], name='fk_module_requests_resolved_by'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_module_requests_tenant_id', 'module_requests', ['tenant_id'], unique=False)
    op.create_index('ix_module_requests_status', 'module_requests', ['status'], unique=False)


def downgrade():
    op.drop_index('ix_module_requests_status', table_name='module_requests')
    op.drop_index('ix_module_requests_tenant_id', table_name='module_requests')
    op.drop_table('module_requests')
