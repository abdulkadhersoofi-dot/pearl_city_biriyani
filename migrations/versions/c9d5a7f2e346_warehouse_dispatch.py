"""Warehouse dispatch by serial scan: warehouse logins and phones, picks, the push
outbox, receiving and counts; dispatch columns on sales orders and serials.

(serial_status gains PICKED - added in an autocommit block, as Postgres requires.)

Revision ID: c9d5a7f2e346
Revises: b8c4f6e1d235
Create Date: 2026-11-08 18:54:16.759732

"""
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'c9d5a7f2e346'
down_revision = 'b8c4f6e1d235'
branch_labels = None
depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE serial_status ADD VALUE IF NOT EXISTS 'PICKED'")
    postgresql.ENUM('OPEN', 'DONE', 'CANCELLED', name='receipt_status').create(op.get_bind(), checkfirst=True)
    postgresql.ENUM('BARCODE', 'PHOTO', 'MANUAL', 'SCANNER', name='pick_method').create(op.get_bind(), checkfirst=True)
    postgresql.ENUM('OK', 'ADDED', 'WRONG_PRODUCT', 'NOT_IN_STOCK', 'WRONG_LOCATION', 'UNKNOWN', 'DUPLICATE', 'LINE_FULL', name='pick_result').create(op.get_bind(), checkfirst=True)
    postgresql.ENUM('TO_PICK', 'PICKING', 'PICKED', 'INVOICED', name='dispatch_status').create(op.get_bind(), checkfirst=True)
    op.create_table('warehouse_logins',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('login_id', sa.String(length=40), nullable=False),
    sa.Column('mobile', sa.String(length=15), nullable=True),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('pin_hash', sa.String(length=255), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('manual_serial', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('failed_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_warehouse_logins_created_by'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'login_id', name='uq_warehouse_logins_tenant_login')
    )
    with op.batch_alter_table('warehouse_logins', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_warehouse_logins_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('stock_counts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('location_id', sa.Integer(), nullable=False),
    sa.Column('started_by_id', sa.Integer(), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['location_id'], ['stock_locations.id'], name='fk_stock_counts_location'),
    sa.ForeignKeyConstraint(['started_by_id'], ['warehouse_logins.id'], name='fk_stock_counts_started_by'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('stock_counts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_stock_counts_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('warehouse_login_locations',
    sa.Column('login_id', sa.Integer(), nullable=False),
    sa.Column('location_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['location_id'], ['stock_locations.id'], name='fk_wll_location', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['login_id'], ['warehouse_logins.id'], name='fk_wll_login', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('login_id', 'location_id')
    )
    op.create_table('warehouse_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('login_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('device_name', sa.String(length=120), nullable=True),
    sa.Column('fcm_token', sa.String(length=400), nullable=True),
    sa.Column('app_version', sa.String(length=20), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('pin_failures', sa.Integer(), server_default='0', nullable=False),
    sa.Column('generation', sa.Integer(), server_default='0', nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoke_reason', sa.String(length=60), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['login_id'], ['warehouse_logins.id'], name='fk_warehouse_sessions_login'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('warehouse_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_warehouse_sessions_login_id'), ['login_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_warehouse_sessions_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('stock_count_scans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('count_id', sa.Integer(), nullable=False),
    sa.Column('serial_no', sa.String(length=64), nullable=False),
    sa.Column('login_id', sa.Integer(), nullable=True),
    sa.Column('client_scan_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['count_id'], ['stock_counts.id'], name='fk_stock_count_scans_count'),
    sa.ForeignKeyConstraint(['login_id'], ['warehouse_logins.id'], name='fk_stock_count_scans_login'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('count_id', 'serial_no', name='uq_stock_count_scans_count_serial'),
    sa.UniqueConstraint('tenant_id', 'client_scan_id', name='uq_stock_count_scans_tenant_client')
    )
    with op.batch_alter_table('stock_count_scans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_stock_count_scans_count_id'), ['count_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_count_scans_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('warehouse_notices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('location_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('sales_document_id', sa.Integer(), nullable=True),
    sa.Column('title', sa.String(length=120), nullable=True),
    sa.Column('body', sa.String(length=255), nullable=True),
    sa.Column('silent', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('recipients', sa.Integer(), nullable=True),
    sa.Column('last_error', sa.String(length=255), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['location_id'], ['stock_locations.id'], name='fk_warehouse_notices_location'),
    sa.ForeignKeyConstraint(['sales_document_id'], ['sales_documents.id'], name='fk_warehouse_notices_doc'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('warehouse_notices', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_warehouse_notices_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_warehouse_notices_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('warehouse_receipts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('purchase_order_id', sa.Integer(), nullable=False),
    sa.Column('location_id', sa.Integer(), nullable=False),
    sa.Column('status', postgresql.ENUM('OPEN', 'DONE', 'CANCELLED', name='receipt_status', create_type=False), nullable=False),
    sa.Column('purchase_bill_id', sa.Integer(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_warehouse_receipts_created_by'),
    sa.ForeignKeyConstraint(['location_id'], ['stock_locations.id'], name='fk_warehouse_receipts_location'),
    sa.ForeignKeyConstraint(['purchase_bill_id'], ['purchase_bills.id'], name='fk_warehouse_receipts_bill'),
    sa.ForeignKeyConstraint(['purchase_order_id'], ['purchase_orders.id'], name='fk_warehouse_receipts_po'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('warehouse_receipts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_warehouse_receipts_purchase_order_id'), ['purchase_order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_warehouse_receipts_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('received_units',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('receipt_id', sa.Integer(), nullable=False),
    sa.Column('order_line_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('serial_no', sa.String(length=64), nullable=False),
    sa.Column('raw_text', sa.String(length=200), nullable=True),
    sa.Column('method', postgresql.ENUM('BARCODE', 'PHOTO', 'MANUAL', 'SCANNER', name='pick_method', create_type=False), nullable=False),
    sa.Column('photo_id', sa.Integer(), nullable=True),
    sa.Column('login_id', sa.Integer(), nullable=True),
    sa.Column('client_unit_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['login_id'], ['warehouse_logins.id'], name='fk_received_units_login'),
    sa.ForeignKeyConstraint(['order_line_id'], ['purchase_order_lines.id'], name='fk_received_units_line'),
    sa.ForeignKeyConstraint(['photo_id'], ['uploaded_images.id'], name='fk_received_units_photo'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name='fk_received_units_product'),
    sa.ForeignKeyConstraint(['receipt_id'], ['warehouse_receipts.id'], name='fk_received_units_receipt'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('receipt_id', 'serial_no', name='uq_received_units_receipt_serial'),
    sa.UniqueConstraint('tenant_id', 'client_unit_id', name='uq_received_units_tenant_client')
    )
    with op.batch_alter_table('received_units', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_received_units_receipt_id'), ['receipt_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_received_units_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('serial_picks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('sales_document_id', sa.Integer(), nullable=False),
    sa.Column('sales_line_id', sa.Integer(), nullable=False),
    sa.Column('serial_id', sa.Integer(), nullable=True),
    sa.Column('raw_text', sa.String(length=200), nullable=True),
    sa.Column('serial_no', sa.String(length=64), nullable=True),
    sa.Column('method', postgresql.ENUM('BARCODE', 'PHOTO', 'MANUAL', 'SCANNER', name='pick_method', create_type=False), nullable=False),
    sa.Column('result', postgresql.ENUM('OK', 'ADDED', 'WRONG_PRODUCT', 'NOT_IN_STOCK', 'WRONG_LOCATION', 'UNKNOWN', 'DUPLICATE', 'LINE_FULL', name='pick_result', create_type=False), nullable=False),
    sa.Column('login_id', sa.Integer(), nullable=True),
    sa.Column('session_id', sa.Integer(), nullable=True),
    sa.Column('client_pick_id', sa.String(length=64), nullable=True),
    sa.Column('photo_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('undone_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('undone_reason', sa.String(length=60), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['login_id'], ['warehouse_logins.id'], name='fk_serial_picks_login'),
    sa.ForeignKeyConstraint(['photo_id'], ['uploaded_images.id'], name='fk_serial_picks_photo'),
    sa.ForeignKeyConstraint(['sales_document_id'], ['sales_documents.id'], name='fk_serial_picks_doc'),
    sa.ForeignKeyConstraint(['sales_line_id'], ['sales_document_lines.id'], name='fk_serial_picks_line'),
    sa.ForeignKeyConstraint(['serial_id'], ['product_serials.id'], name='fk_serial_picks_serial'),
    sa.ForeignKeyConstraint(['session_id'], ['warehouse_sessions.id'], name='fk_serial_picks_session'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'client_pick_id', name='uq_serial_picks_tenant_client')
    )
    with op.batch_alter_table('serial_picks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_serial_picks_sales_document_id'), ['sales_document_id'], unique=False)
        batch_op.create_index('ix_serial_picks_tenant_created', ['tenant_id', 'created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_serial_picks_tenant_id'), ['tenant_id'], unique=False)

    with op.batch_alter_table('product_serials', schema=None) as batch_op:
        batch_op.add_column(sa.Column('picked_sales_line_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('picked_by_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('picked_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('pick_photo_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('sold_invoice_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('sold_invoice_line_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('added_at_pick', sa.Boolean(), server_default=sa.text('false'), nullable=False))
        batch_op.create_index(batch_op.f('ix_product_serials_picked_sales_line_id'), ['picked_sales_line_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_product_serials_sold_invoice_id'), ['sold_invoice_id'], unique=False)
        batch_op.create_foreign_key('fk_product_serials_pick_photo', 'uploaded_images', ['pick_photo_id'], ['id'])
        batch_op.create_foreign_key('fk_product_serials_picked_by', 'warehouse_logins', ['picked_by_id'], ['id'])
        batch_op.create_foreign_key('fk_product_serials_invoice', 'invoices', ['sold_invoice_id'], ['id'])
        batch_op.create_foreign_key('fk_product_serials_invoice_line', 'invoice_lines', ['sold_invoice_line_id'], ['id'])
        batch_op.create_foreign_key('fk_product_serials_picked_line', 'sales_document_lines', ['picked_sales_line_id'], ['id'])

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('model_no', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_products_tenant_model_no', ['tenant_id', 'model_no'], unique=False)

    with op.batch_alter_table('sales_documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dispatch_location_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('dispatch_status', postgresql.ENUM('TO_PICK', 'PICKING', 'PICKED', 'INVOICED', name='dispatch_status', create_type=False), nullable=True))
        batch_op.add_column(sa.Column('claimed_by_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('dispatch_activity_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('picked_confirmed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('picked_confirmed_by_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_sales_documents_dispatch_location_id'), ['dispatch_location_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_documents_dispatch_status'), ['dispatch_status'], unique=False)
        batch_op.create_foreign_key('fk_sales_documents_claimed_by', 'warehouse_logins', ['claimed_by_id'], ['id'])
        batch_op.create_foreign_key('fk_sales_documents_picked_by', 'warehouse_logins', ['picked_confirmed_by_id'], ['id'])
        batch_op.create_foreign_key('fk_sales_documents_dispatch_location', 'stock_locations', ['dispatch_location_id'], ['id'])

    with op.batch_alter_table('stock_locations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('address_registered', sa.Boolean(), server_default=sa.text('false'), nullable=False))
        batch_op.add_column(sa.Column('default_dispatch_location_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_stock_locations_default_dispatch', 'stock_locations', ['default_dispatch_location_id'], ['id'])

    # ### end Alembic commands ###

    # The Mobiles & Electronics package offers the new module (and the sales
    # orders it works from) as the client's own choice - off until switched on.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, modules FROM packages WHERE key = 'electronics'")).fetchall()
    for row_id, modules in rows:
        modules = dict(modules or {})
        changed = False
        for key in ("sales_documents", "warehouse_dispatch"):
            if key not in modules:
                modules[key] = "choice"
                changed = True
        if changed:
            bind.execute(sa.text("UPDATE packages SET modules = :m WHERE id = :id"),
                         {"m": json.dumps(modules), "id": row_id})


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('stock_locations', schema=None) as batch_op:
        batch_op.drop_constraint('fk_stock_locations_default_dispatch', type_='foreignkey')
        batch_op.drop_column('default_dispatch_location_id')
        batch_op.drop_column('address_registered')

    with op.batch_alter_table('sales_documents', schema=None) as batch_op:
        batch_op.drop_constraint('fk_sales_documents_dispatch_location', type_='foreignkey')
        batch_op.drop_constraint('fk_sales_documents_picked_by', type_='foreignkey')
        batch_op.drop_constraint('fk_sales_documents_claimed_by', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_sales_documents_dispatch_status'))
        batch_op.drop_index(batch_op.f('ix_sales_documents_dispatch_location_id'))
        batch_op.drop_column('picked_confirmed_by_id')
        batch_op.drop_column('picked_confirmed_at')
        batch_op.drop_column('dispatch_activity_at')
        batch_op.drop_column('claimed_at')
        batch_op.drop_column('claimed_by_id')
        batch_op.drop_column('dispatch_status')
        batch_op.drop_column('dispatch_location_id')

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_index('ix_products_tenant_model_no')
        batch_op.drop_column('model_no')

    with op.batch_alter_table('product_serials', schema=None) as batch_op:
        batch_op.drop_constraint('fk_product_serials_picked_line', type_='foreignkey')
        batch_op.drop_constraint('fk_product_serials_invoice_line', type_='foreignkey')
        batch_op.drop_constraint('fk_product_serials_invoice', type_='foreignkey')
        batch_op.drop_constraint('fk_product_serials_picked_by', type_='foreignkey')
        batch_op.drop_constraint('fk_product_serials_pick_photo', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_product_serials_sold_invoice_id'))
        batch_op.drop_index(batch_op.f('ix_product_serials_picked_sales_line_id'))
        batch_op.drop_column('added_at_pick')
        batch_op.drop_column('sold_invoice_line_id')
        batch_op.drop_column('sold_invoice_id')
        batch_op.drop_column('pick_photo_id')
        batch_op.drop_column('picked_at')
        batch_op.drop_column('picked_by_id')
        batch_op.drop_column('picked_sales_line_id')

    with op.batch_alter_table('serial_picks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_serial_picks_tenant_id'))
        batch_op.drop_index('ix_serial_picks_tenant_created')
        batch_op.drop_index(batch_op.f('ix_serial_picks_sales_document_id'))

    op.drop_table('serial_picks')
    with op.batch_alter_table('received_units', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_received_units_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_received_units_receipt_id'))

    op.drop_table('received_units')
    with op.batch_alter_table('warehouse_receipts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_warehouse_receipts_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_warehouse_receipts_purchase_order_id'))

    op.drop_table('warehouse_receipts')
    with op.batch_alter_table('warehouse_notices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_warehouse_notices_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_warehouse_notices_created_at'))

    op.drop_table('warehouse_notices')
    with op.batch_alter_table('stock_count_scans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stock_count_scans_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_stock_count_scans_count_id'))

    op.drop_table('stock_count_scans')
    with op.batch_alter_table('warehouse_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_warehouse_sessions_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_warehouse_sessions_login_id'))

    op.drop_table('warehouse_sessions')
    op.drop_table('warehouse_login_locations')
    with op.batch_alter_table('stock_counts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stock_counts_tenant_id'))

    op.drop_table('stock_counts')
    with op.batch_alter_table('warehouse_logins', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_warehouse_logins_tenant_id'))

    op.drop_table('warehouse_logins')
    # ### end Alembic commands ###
    postgresql.ENUM(name='receipt_status').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='pick_method').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='pick_result').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='dispatch_status').drop(op.get_bind(), checkfirst=True)
