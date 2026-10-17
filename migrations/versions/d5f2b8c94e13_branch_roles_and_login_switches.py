"""Packages and powers, stage 5: branch roles, modules hidden at a branch,
the Ultra Admin's per-login switches, counter permissions and sign-in
minimums per package. No data changes - every existing branch login keeps
its six permissions, every package allows all counter permissions and sets
no minimums.

Revision ID: d5f2b8c94e13
Revises: c4e8a1f07d52
Create Date: 2026-10-17 18:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'd5f2b8c94e13'
down_revision = 'c4e8a1f07d52'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('packages', sa.Column('counter_permissions', sa.JSON(), nullable=True))
    op.add_column('packages', sa.Column('security', sa.JSON(), server_default='{}', nullable=False))
    op.add_column('tenant_packages', sa.Column('counter_permissions_override', sa.JSON(), nullable=True))
    op.add_column('tenant_packages', sa.Column('security_overrides', sa.JSON(), server_default='{}', nullable=False))
    op.add_column('users', sa.Column('branch_role', sa.String(length=20), nullable=True))
    op.add_column('users', sa.Column('hidden_modules', sa.JSON(), nullable=True))
    op.create_table('login_grants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('hidden_modules', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('permissions', sa.JSON(), server_default='{}', nullable=False),
    sa.Column('max_discount_percent', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('role', sa.String(length=20), nullable=True),
    sa.Column('set_by_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_login_grants_tenant'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_login_grants_user'),
    sa.ForeignKeyConstraint(['set_by_id'], ['users.id'], name='fk_login_grants_set_by'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_login_grants_tenant_id', 'login_grants', ['tenant_id'], unique=False)
    op.create_index('ix_login_grants_user_id', 'login_grants', ['user_id'], unique=True)


def downgrade():
    op.drop_index('ix_login_grants_user_id', table_name='login_grants')
    op.drop_index('ix_login_grants_tenant_id', table_name='login_grants')
    op.drop_table('login_grants')
    op.drop_column('users', 'hidden_modules')
    op.drop_column('users', 'branch_role')
    op.drop_column('tenant_packages', 'security_overrides')
    op.drop_column('tenant_packages', 'counter_permissions_override')
    op.drop_column('packages', 'security')
    op.drop_column('packages', 'counter_permissions')
