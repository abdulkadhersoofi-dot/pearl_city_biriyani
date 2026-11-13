"""The SAP link: its connection, code mappings, fetched orders and call log;
a purchase order's source.

Revision ID: e7b1d4f9a205
Revises: d5a9c3e7f842
Create Date: 2026-11-14 03:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e7b1d4f9a205'
down_revision = 'd5a9c3e7f842'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'sap_connections',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('gstin_id', sa.Integer(), nullable=True),
        sa.Column('base_url', sa.String(length=255), nullable=False),
        sa.Column('api', sa.String(length=10), nullable=False),
        sa.Column('auth', sa.String(length=10), nullable=False),
        sa.Column('username', sa.String(length=120), nullable=True),
        sa.Column('secrets', sa.Text(), nullable=True),
        sa.Column('token_url', sa.String(length=255), nullable=True),
        sa.Column('sap_client', sa.String(length=3), nullable=True),
        sa.Column('company_code', sa.String(length=10), nullable=True),
        sa.Column('purchasing_org', sa.String(length=10), nullable=True),
        sa.Column('plant', sa.String(length=10), nullable=True),
        sa.Column('since', sa.Date(), nullable=True),
        sa.Column('lookup_partners', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('cursor', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_pull_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_pull_ok', sa.Boolean(), nullable=True),
        sa.Column('last_pull_message', sa.String(length=500), nullable=True),
        sa.Column('last_pull_counts', sa.JSON(), nullable=True),
        sa.Column('updated_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_sap_connections_gstin'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], name='fk_sap_connections_updated_by'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', name='uq_sap_connections_tenant'),
    )
    op.create_index('ix_sap_connections_tenant_id', 'sap_connections', ['tenant_id'])
    op.create_table(
        'sap_mappings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('sap_code', sa.String(length=40), nullable=False),
        sa.Column('sap_name', sa.String(length=255), nullable=True),
        sa.Column('sap_gstin', sa.String(length=15), nullable=True),
        sa.Column('supplier_id', sa.Integer(), nullable=True),
        sa.Column('product_id', sa.Integer(), nullable=True),
        sa.Column('how', sa.String(length=20), nullable=True),
        sa.Column('mapped_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['mapped_by_id'], ['users.id'], name='fk_sap_mappings_mapped_by'),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], name='fk_sap_mappings_product'),
        sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name='fk_sap_mappings_supplier'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'kind', 'sap_code', name='uq_sap_mappings_code'),
    )
    op.create_index('ix_sap_mappings_tenant_id', 'sap_mappings', ['tenant_id'])
    op.create_table(
        'sap_orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('number', sa.String(length=20), nullable=False),
        sa.Column('supplier_code', sa.String(length=40), nullable=True),
        sa.Column('order_date', sa.Date(), nullable=True),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('note', sa.String(length=300), nullable=True),
        sa.Column('purchase_order_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['purchase_order_id'], ['purchase_orders.id'], name='fk_sap_orders_po'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'number', name='uq_sap_orders_number'),
    )
    op.create_index('ix_sap_orders_tenant_id', 'sap_orders', ['tenant_id'])
    op.create_index('ix_sap_orders_purchase_order_id', 'sap_orders', ['purchase_order_id'])
    op.create_table(
        'sap_calls',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('purpose', sa.String(length=20), nullable=False),
        sa.Column('path', sa.String(length=300), nullable=False),
        sa.Column('status', sa.Integer(), nullable=True),
        sa.Column('ms', sa.Integer(), nullable=True),
        sa.Column('records', sa.Integer(), nullable=True),
        sa.Column('error', sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_sap_calls_user'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sap_calls_tenant_id', 'sap_calls', ['tenant_id'])
    op.create_index('ix_sap_calls_at', 'sap_calls', ['at'])
    with op.batch_alter_table('purchase_orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=10), server_default='arfa', nullable=False))
        batch_op.add_column(sa.Column('external_ref', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('external_synced_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index('ix_purchase_orders_external_ref', ['external_ref'])
    with op.batch_alter_table('purchase_order_lines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('external_ref', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('external_code', sa.String(length=40), nullable=True))


def downgrade():
    with op.batch_alter_table('purchase_order_lines', schema=None) as batch_op:
        batch_op.drop_column('external_code')
        batch_op.drop_column('external_ref')
    with op.batch_alter_table('purchase_orders', schema=None) as batch_op:
        batch_op.drop_index('ix_purchase_orders_external_ref')
        batch_op.drop_column('external_synced_at')
        batch_op.drop_column('external_ref')
        batch_op.drop_column('source')
    op.drop_table('sap_calls')
    op.drop_table('sap_orders')
    op.drop_table('sap_mappings')
    op.drop_table('sap_connections')
