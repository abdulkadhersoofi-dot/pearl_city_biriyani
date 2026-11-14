"""Warehouse dispatch, stage 5B - EMI (finance) sales: financiers, the finance
split on orders and invoices, racks (bin_code), delivery scans and serial swaps.

Revision ID: d4e8b2c6f159
Revises: c9d5a7f2e346
Create Date: 2026-11-08 20:28:26.004739

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'd4e8b2c6f159'
down_revision = 'c9d5a7f2e346'
branch_labels = None
depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE dispatch_status ADD VALUE IF NOT EXISTS 'DELIVERED'")
        op.execute("ALTER TYPE pick_method ADD VALUE IF NOT EXISTS 'SHOWROOM'")
    postgresql.ENUM('PENDING', 'APPROVED', 'REJECTED', name='swap_status').create(op.get_bind(), checkfirst=True)
    op.create_table('financiers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('short_code', sa.String(length=20), nullable=True),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name='fk_financiers_customer'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'name', name='uq_financiers_tenant_name')
    )
    with op.batch_alter_table('financiers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_financiers_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('delivery_scans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('sales_document_id', sa.Integer(), nullable=False),
    sa.Column('sales_line_id', sa.Integer(), nullable=True),
    sa.Column('serial_id', sa.Integer(), nullable=True),
    sa.Column('serial_no', sa.String(length=64), nullable=True),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('login_id', sa.Integer(), nullable=True),
    sa.Column('client_scan_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['login_id'], ['warehouse_logins.id'], name='fk_delivery_scans_login'),
    sa.ForeignKeyConstraint(['sales_document_id'], ['sales_documents.id'], name='fk_delivery_scans_doc'),
    sa.ForeignKeyConstraint(['sales_line_id'], ['sales_document_lines.id'], name='fk_delivery_scans_line'),
    sa.ForeignKeyConstraint(['serial_id'], ['product_serials.id'], name='fk_delivery_scans_serial'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'client_scan_id', name='uq_delivery_scans_tenant_client')
    )
    with op.batch_alter_table('delivery_scans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_delivery_scans_sales_document_id'), ['sales_document_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_delivery_scans_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('serial_swaps',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('sales_document_id', sa.Integer(), nullable=False),
    sa.Column('sales_line_id', sa.Integer(), nullable=False),
    sa.Column('old_serial_id', sa.Integer(), nullable=False),
    sa.Column('new_serial_id', sa.Integer(), nullable=False),
    sa.Column('reason', sa.String(length=255), nullable=False),
    sa.Column('status', postgresql.ENUM('PENDING', 'APPROVED', 'REJECTED', name='swap_status', create_type=False), nullable=False),
    sa.Column('requested_by_id', sa.Integer(), nullable=True),
    sa.Column('decided_by_id', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['decided_by_id'], ['users.id'], name='fk_serial_swaps_decided_by'),
    sa.ForeignKeyConstraint(['new_serial_id'], ['product_serials.id'], name='fk_serial_swaps_new'),
    sa.ForeignKeyConstraint(['old_serial_id'], ['product_serials.id'], name='fk_serial_swaps_old'),
    sa.ForeignKeyConstraint(['requested_by_id'], ['warehouse_logins.id'], name='fk_serial_swaps_requested_by'),
    sa.ForeignKeyConstraint(['sales_document_id'], ['sales_documents.id'], name='fk_serial_swaps_doc'),
    sa.ForeignKeyConstraint(['sales_line_id'], ['sales_document_lines.id'], name='fk_serial_swaps_line'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('serial_swaps', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_serial_swaps_sales_document_id'), ['sales_document_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_serial_swaps_tenant_id'), ['tenant_id'], unique=False)

    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('financier_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('finance_amount', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column('loan_ref', sa.String(length=60), nullable=True))
        batch_op.create_foreign_key('fk_invoices_financier', 'financiers', ['financier_id'], ['id'])

    with op.batch_alter_table('product_serials', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bin_code', sa.String(length=32), nullable=True))
        batch_op.create_index(batch_op.f('ix_product_serials_bin_code'), ['bin_code'], unique=False)

    with op.batch_alter_table('received_units', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bin_code', sa.String(length=32), nullable=True))

    with op.batch_alter_table('sales_documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('delivered_by_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('is_finance_sale', sa.Boolean(), server_default=sa.text('false'), nullable=False))
        batch_op.add_column(sa.Column('financier_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('loan_ref', sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column('down_payment', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column('finance_amount', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column('priority', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('invoiced_at_showroom', sa.Boolean(), server_default=sa.text('false'), nullable=False))
        batch_op.create_foreign_key('fk_sales_documents_delivered_by', 'warehouse_logins', ['delivered_by_id'], ['id'])
        batch_op.create_foreign_key('fk_sales_documents_financier', 'financiers', ['financier_id'], ['id'])

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('sales_documents', schema=None) as batch_op:
        batch_op.drop_constraint('fk_sales_documents_financier', type_='foreignkey')
        batch_op.drop_constraint('fk_sales_documents_delivered_by', type_='foreignkey')
        batch_op.drop_column('invoiced_at_showroom')
        batch_op.drop_column('priority')
        batch_op.drop_column('finance_amount')
        batch_op.drop_column('down_payment')
        batch_op.drop_column('loan_ref')
        batch_op.drop_column('financier_id')
        batch_op.drop_column('is_finance_sale')
        batch_op.drop_column('delivered_by_id')
        batch_op.drop_column('delivered_at')

    with op.batch_alter_table('received_units', schema=None) as batch_op:
        batch_op.drop_column('bin_code')

    with op.batch_alter_table('product_serials', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_product_serials_bin_code'))
        batch_op.drop_column('bin_code')

    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_constraint('fk_invoices_financier', type_='foreignkey')
        batch_op.drop_column('loan_ref')
        batch_op.drop_column('finance_amount')
        batch_op.drop_column('financier_id')

    with op.batch_alter_table('serial_swaps', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_serial_swaps_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_serial_swaps_sales_document_id'))

    op.drop_table('serial_swaps')
    with op.batch_alter_table('delivery_scans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_delivery_scans_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_delivery_scans_sales_document_id'))

    op.drop_table('delivery_scans')
    with op.batch_alter_table('financiers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_financiers_tenant_id'))

    op.drop_table('financiers')
    # ### end Alembic commands ###
    postgresql.ENUM(name='swap_status').drop(op.get_bind(), checkfirst=True)
