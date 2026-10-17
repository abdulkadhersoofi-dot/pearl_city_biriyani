"""Second-hand goods (margin scheme) as its own module: the dealer's own
purchase voucher (document type margin_purchase, series SHP), what kind a
purchase bill is and who sold it, and the e-way bill's "other value" (the
non-margin part of a margin-scheme sale). No data changes - every existing
purchase bill is a "bill".

Revision ID: e7a1c5d9b234
Revises: d5f2b8c94e13
Create Date: 2026-10-17 21:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'e7a1c5d9b234'
down_revision = 'd5f2b8c94e13'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            # Postgres stores this enum by member name (as the earlier migrations add it).
            op.execute("ALTER TYPE document_type ADD VALUE IF NOT EXISTS 'MARGIN_PURCHASE'")
    op.add_column('purchase_bills', sa.Column('kind', sa.String(length=20), server_default='bill', nullable=False))
    op.add_column('purchase_bills', sa.Column('seller_details', sa.JSON(), nullable=True))
    op.add_column('eway_bills', sa.Column('other_value', sa.Numeric(precision=14, scale=2), server_default='0',
                                          nullable=False))


def downgrade():
    # (A value can't be dropped from a Postgres enum; 'MARGIN_PURCHASE' stays.)
    op.drop_column('eway_bills', 'other_value')
    op.drop_column('purchase_bills', 'seller_details')
    op.drop_column('purchase_bills', 'kind')
