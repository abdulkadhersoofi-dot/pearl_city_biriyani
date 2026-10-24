"""A client's own sign-in address (tenants.login_domain) and the one-time
hand-over from it to the main address (login_handoffs) -
app/auth/login_domain.py.

Revision ID: a4c8e2f61d37
Revises: f3a9c1e7b254
Create Date: 2026-10-24 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a4c8e2f61d37'
down_revision = 'f3a9c1e7b254'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tenants', sa.Column('login_domain', sa.String(length=253), nullable=True))
    op.create_unique_constraint('uq_tenants_login_domain', 'tenants', ['login_domain'])
    op.create_table(
        'login_handoffs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('from_host', sa.String(length=253), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_login_handoffs_user'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_login_handoffs_user_id', 'login_handoffs', ['user_id'])
    op.create_index('ix_login_handoffs_expires_at', 'login_handoffs', ['expires_at'])


def downgrade():
    op.drop_index('ix_login_handoffs_expires_at', table_name='login_handoffs')
    op.drop_index('ix_login_handoffs_user_id', table_name='login_handoffs')
    op.drop_table('login_handoffs')
    op.drop_constraint('uq_tenants_login_domain', 'tenants', type_='unique')
    op.drop_column('tenants', 'login_domain')
