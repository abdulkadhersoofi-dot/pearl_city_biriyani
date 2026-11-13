"""Books check results: one row per run, per business and month.

Revision ID: b3f7a1d9c642
Revises: a7d2e9c4b510
Create Date: 2026-11-13 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b3f7a1d9c642'
down_revision = 'a7d2e9c4b510'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'books_check_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('period', sa.String(length=7), nullable=False),
        sa.Column('ran_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False),
        sa.Column('failed_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('results', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_books_check_runs_tenant_period', 'books_check_runs', ['tenant_id', 'period', 'ran_at'])
    op.create_index('ix_books_check_runs_tenant_id', 'books_check_runs', ['tenant_id'])


def downgrade():
    op.drop_index('ix_books_check_runs_tenant_id', table_name='books_check_runs')
    op.drop_index('ix_books_check_runs_tenant_period', table_name='books_check_runs')
    op.drop_table('books_check_runs')
