"""margin scheme (Rule 32(5)): product / purchase-line / sale-line flags,
margin columns on sale lines, margin lots (margin_allocations), the
margin-scheme invoice series and the tenant's declaration text.

Hand-reviewed:
  - document_type gains 'MARGIN_SCHEME_INVOICE' (ADD VALUE IF NOT
    EXISTS, in an autocommit block). Left in place on downgrade, as
    Postgres can't drop an enum value.
  - every new boolean / amount column has a server_default, so existing
    rows read as "not margin scheme" with zero margin - no backfill.
  - every foreign key is named.

Revision ID: 8c735c88aaa0
Revises: 415fbed454ee
Create Date: 2026-10-15 13:55:34.042088

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8c735c88aaa0'
down_revision = '415fbed454ee'
branch_labels = None
depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE document_type ADD VALUE IF NOT EXISTS 'MARGIN_SCHEME_INVOICE'")
    op.create_table('margin_allocations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source_type', sa.String(length=20), nullable=False),
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.Column('source_line_id', sa.Integer(), nullable=False),
    sa.Column('document_number', sa.String(length=64), nullable=True),
    sa.Column('sold_on', sa.Date(), nullable=False),
    sa.Column('purchase_line_id', sa.Integer(), nullable=False),
    sa.Column('serial_id', sa.Integer(), nullable=True),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('qty', sa.Numeric(precision=12, scale=3), nullable=False),
    sa.Column('returned_qty', sa.Numeric(precision=12, scale=3), server_default='0', nullable=False),
    sa.Column('purchase_value', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('sale_value', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('margin', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('taxable_value', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('cgst_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('sgst_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('igst_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name='fk_margin_allocations_product'),
    sa.ForeignKeyConstraint(['purchase_line_id'], ['purchase_bill_lines.id'], name='fk_margin_allocations_purchase_line'),
    sa.ForeignKeyConstraint(['serial_id'], ['product_serials.id'], name='fk_margin_allocations_serial'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_margin_allocations_tenant'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('margin_allocations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_margin_allocations_purchase_line_id'), ['purchase_line_id'], unique=False)
        batch_op.create_index('ix_margin_allocations_source', ['source_type', 'source_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_margin_allocations_tenant_id'), ['tenant_id'], unique=False)

    with op.batch_alter_table('invoice_lines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_margin_scheme', sa.Boolean(), server_default=sa.text('false'), nullable=False))
        batch_op.add_column(sa.Column('margin_purchase_value', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('margin_value', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False))

    with op.batch_alter_table('pos_bill_lines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_margin_scheme', sa.Boolean(), server_default=sa.text('false'), nullable=False))
        batch_op.add_column(sa.Column('margin_purchase_value', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('margin_value', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False))

    with op.batch_alter_table('pos_bills', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_margin_scheme', sa.Boolean(), server_default=sa.text('false'), nullable=False))

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_margin_scheme', sa.Boolean(), server_default=sa.text('false'), nullable=False))

    with op.batch_alter_table('purchase_bill_lines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_margin_scheme', sa.Boolean(), server_default=sa.text('false'), nullable=False))

    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('margin_scheme_declaration', sa.Text(), nullable=True))



def downgrade():
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('margin_scheme_declaration')

    with op.batch_alter_table('purchase_bill_lines', schema=None) as batch_op:
        batch_op.drop_column('is_margin_scheme')

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('is_margin_scheme')

    with op.batch_alter_table('pos_bills', schema=None) as batch_op:
        batch_op.drop_column('is_margin_scheme')

    with op.batch_alter_table('pos_bill_lines', schema=None) as batch_op:
        batch_op.drop_column('margin_value')
        batch_op.drop_column('margin_purchase_value')
        batch_op.drop_column('is_margin_scheme')

    with op.batch_alter_table('invoice_lines', schema=None) as batch_op:
        batch_op.drop_column('margin_value')
        batch_op.drop_column('margin_purchase_value')
        batch_op.drop_column('is_margin_scheme')

    with op.batch_alter_table('margin_allocations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_margin_allocations_tenant_id'))
        batch_op.drop_index('ix_margin_allocations_source')
        batch_op.drop_index(batch_op.f('ix_margin_allocations_purchase_line_id'))

    op.drop_table('margin_allocations')
    # 'MARGIN_SCHEME_INVOICE' stays in document_type.
