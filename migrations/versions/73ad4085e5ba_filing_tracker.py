"""filing tracker: one row per client GSTIN, month and return (GSTR-1 /
GSTR-3B) - data ready (client), filed with date and ARN (firm).

Hand-reviewed:
  - the new enum type filing_return_type is created once, up front, and
    the column refers to it with create_type=False; downgrade drops it.
  - every foreign key and the unique constraint are named; booleans have
    server defaults.

Revision ID: 73ad4085e5ba
Revises: 830e40b7b9d8
Create Date: 2026-10-15 18:35:36.046631

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

RETURN_TYPE = postgresql.ENUM('GSTR1', 'GSTR3B', name='filing_return_type', create_type=False)


# revision identifiers, used by Alembic.
revision = '73ad4085e5ba'
down_revision = '830e40b7b9d8'
branch_labels = None
depends_on = None


def upgrade():
    postgresql.ENUM(*RETURN_TYPE.enums, name=RETURN_TYPE.name).create(op.get_bind(), checkfirst=True)
    op.create_table('filing_statuses',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gstin_id', sa.Integer(), nullable=False),
    sa.Column('period', sa.String(length=7), nullable=False),
    sa.Column('return_type', RETURN_TYPE, nullable=False),
    sa.Column('data_ready', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('data_ready_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('data_ready_by_id', sa.Integer(), nullable=True),
    sa.Column('filed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('filed_on', sa.Date(), nullable=True),
    sa.Column('arn', sa.String(length=30), nullable=True),
    sa.Column('filed_by_id', sa.Integer(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['data_ready_by_id'], ['users.id'], name='fk_filing_statuses_ready_by'),
    sa.ForeignKeyConstraint(['filed_by_id'], ['users.id'], name='fk_filing_statuses_filed_by'),
    sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_filing_statuses_gstin'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_filing_statuses_tenant'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'gstin_id', 'period', 'return_type', name='uq_filing_statuses_return')
    )
    with op.batch_alter_table('filing_statuses', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_filing_statuses_tenant_id'), ['tenant_id'], unique=False)



def downgrade():
    with op.batch_alter_table('filing_statuses', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_filing_statuses_tenant_id'))

    op.drop_table('filing_statuses')
    postgresql.ENUM(name=RETURN_TYPE.name).drop(op.get_bind(), checkfirst=True)
