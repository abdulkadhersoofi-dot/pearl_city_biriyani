"""accounts and sales documents: customer receipts (incl. advance receipt
vouchers) and their allocations, refund vouchers, supplier payments and
their allocations, quotations / proforma / sales orders / delivery
challans, recurring invoices and their runs; customer credit fields,
opening-balance dates and the tenant's books opening balances.

Hand-reviewed:
  - the seven new enum types are created once, up front, and every column
    refers to them with create_type=False; document_type and
    expense_frequency already exist and are only referenced. Downgrade
    drops the new ones.
  - document_type gains PAYMENT_RECEIPT, SUPPLIER_PAYMENT, QUOTATION,
    PROFORMA_INVOICE, SALES_ORDER (ADD VALUE IF NOT EXISTS, autocommit
    block); left in place on downgrade.
  - expense voucher series move from prefix 'EXP' (the export invoice's)
    to 'EV' so the two never print the same number; vouchers already
    issued keep theirs. Downgrade moves them back.
  - every new boolean / amount column has a server_default - existing
    customers, suppliers and tenants read as zero opening balance and no
    credit limit, no backfill.
  - every foreign key is named.

Revision ID: 830e40b7b9d8
Revises: 8c735c88aaa0
Create Date: 2026-10-15 16:05:27.458031

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '830e40b7b9d8'
down_revision = '8c735c88aaa0'
branch_labels = None
depends_on = None


DOC_TYPES = ['TAX_INVOICE', 'BILL_OF_SUPPLY', 'EXPORT_INVOICE_IGST', 'EXPORT_INVOICE_LUT', 'RCM_INVOICE',
             'DELIVERY_CHALLAN', 'RECEIPT_VOUCHER', 'REFUND_VOUCHER', 'CREDIT_NOTE', 'DEBIT_NOTE', 'POS_BILL',
             'POS_RETURN', 'PURCHASE_BILL', 'PURCHASE_RETURN', 'PURCHASE_ORDER', 'STOCK_TRANSFER', 'STOCK_ADJUSTMENT',
             'EXPENSE', 'MARGIN_SCHEME_INVOICE', 'PAYMENT_RECEIPT', 'SUPPLIER_PAYMENT', 'QUOTATION',
             'PROFORMA_INVOICE', 'SALES_ORDER']
NEW_DOC_TYPES = ['PAYMENT_RECEIPT', 'SUPPLIER_PAYMENT', 'QUOTATION', 'PROFORMA_INVOICE', 'SALES_ORDER']
DOCUMENT_TYPE_ENUM = postgresql.ENUM(*DOC_TYPES, name='document_type', create_type=False)
FREQUENCY_ENUM = postgresql.ENUM('WEEKLY', 'MONTHLY', 'QUARTERLY', 'YEARLY', name='expense_frequency', create_type=False)
MONEY_MODE_ENUM = postgresql.ENUM('CASH', 'UPI', 'CARD', 'BANK', 'CHEQUE', name='money_mode', create_type=False)
VOUCHER_STATUS_ENUM = postgresql.ENUM('POSTED', 'VOIDED', name='voucher_status', create_type=False)
CHEQUE_STATUS_ENUM = postgresql.ENUM('PENDING', 'CLEARED', 'BOUNCED', name='cheque_status', create_type=False)
SALES_KIND_ENUM = postgresql.ENUM('QUOTATION', 'PROFORMA', 'SALES_ORDER', 'DELIVERY_CHALLAN', name='sales_document_kind', create_type=False)
SALES_STATUS_ENUM = postgresql.ENUM('OPEN', 'CONVERTED', 'RETURNED', 'CANCELLED', name='sales_document_status', create_type=False)
CHALLAN_PURPOSE_ENUM = postgresql.ENUM('JOB_WORK', 'SALE_ON_APPROVAL', 'STOCK_TRANSFER', 'OTHER', name='challan_purpose', create_type=False)
RUN_STATUS_ENUM = postgresql.ENUM('PENDING', 'ISSUED', 'SKIPPED', name='recurring_run_status', create_type=False)
NEW_ENUMS = [MONEY_MODE_ENUM, VOUCHER_STATUS_ENUM, CHEQUE_STATUS_ENUM, SALES_KIND_ENUM, SALES_STATUS_ENUM,
             CHALLAN_PURPOSE_ENUM, RUN_STATUS_ENUM]


def upgrade():
    with op.get_context().autocommit_block():
        for value in NEW_DOC_TYPES:
            op.execute(f"ALTER TYPE document_type ADD VALUE IF NOT EXISTS '{value}'")
    bind = op.get_bind()
    for enum in NEW_ENUMS:
        postgresql.ENUM(*enum.enums, name=enum.name).create(bind, checkfirst=True)

    op.create_table('customer_receipts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gstin_id', sa.Integer(), nullable=False),
    sa.Column('invoice_series_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=64), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('voucher_date', sa.Date(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('status', VOUCHER_STATUS_ENUM, nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('void_reason', sa.Text(), nullable=True),
    sa.Column('voided_by_id', sa.Integer(), nullable=True),
    sa.Column('is_advance', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('for_services', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('place_of_supply_state_code', sa.String(length=2), nullable=True),
    sa.Column('gst_rate', sa.Numeric(precision=5, scale=2), server_default='0', nullable=False),
    sa.Column('taxable_value', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('cgst_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('sgst_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('igst_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('mode', MONEY_MODE_ENUM, nullable=False),
    sa.Column('reference', sa.String(length=100), nullable=True),
    sa.Column('cheque_no', sa.String(length=30), nullable=True),
    sa.Column('cheque_date', sa.Date(), nullable=True),
    sa.Column('bank_name', sa.String(length=100), nullable=True),
    sa.Column('cheque_status', CHEQUE_STATUS_ENUM, nullable=True),
    sa.Column('cleared_on', sa.Date(), nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_customer_receipts_created_by'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name='fk_customer_receipts_customer'),
    sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_customer_receipts_gstin'),
    sa.ForeignKeyConstraint(['invoice_series_id'], ['invoice_series.id'], name='fk_customer_receipts_series'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_customer_receipts_tenant'),
    sa.ForeignKeyConstraint(['voided_by_id'], ['users.id'], name='fk_customer_receipts_voided_by'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'number', name='uq_customer_receipts_tenant_number')
    )
    with op.batch_alter_table('customer_receipts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_receipts_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_receipts_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('recurring_invoices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('gstin_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('document_type', DOCUMENT_TYPE_ENUM, nullable=False),
    sa.Column('place_of_supply_state_code', sa.String(length=2), nullable=False),
    sa.Column('frequency', FREQUENCY_ENUM, nullable=False),
    sa.Column('anchor_day', sa.Integer(), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('next_due_date', sa.Date(), nullable=False),
    sa.Column('lines', sa.JSON(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_recurring_invoices_created_by'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name='fk_recurring_invoices_customer'),
    sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_recurring_invoices_gstin'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_recurring_invoices_tenant'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('recurring_invoices', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_recurring_invoices_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_recurring_invoices_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('supplier_payments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gstin_id', sa.Integer(), nullable=False),
    sa.Column('invoice_series_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=64), nullable=False),
    sa.Column('supplier_id', sa.Integer(), nullable=False),
    sa.Column('voucher_date', sa.Date(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('status', VOUCHER_STATUS_ENUM, nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('void_reason', sa.Text(), nullable=True),
    sa.Column('voided_by_id', sa.Integer(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('mode', MONEY_MODE_ENUM, nullable=False),
    sa.Column('reference', sa.String(length=100), nullable=True),
    sa.Column('cheque_no', sa.String(length=30), nullable=True),
    sa.Column('cheque_date', sa.Date(), nullable=True),
    sa.Column('bank_name', sa.String(length=100), nullable=True),
    sa.Column('cheque_status', CHEQUE_STATUS_ENUM, nullable=True),
    sa.Column('cleared_on', sa.Date(), nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_supplier_payments_created_by'),
    sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_supplier_payments_gstin'),
    sa.ForeignKeyConstraint(['invoice_series_id'], ['invoice_series.id'], name='fk_supplier_payments_series'),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name='fk_supplier_payments_supplier'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_supplier_payments_tenant'),
    sa.ForeignKeyConstraint(['voided_by_id'], ['users.id'], name='fk_supplier_payments_voided_by'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'number', name='uq_supplier_payments_tenant_number')
    )
    with op.batch_alter_table('supplier_payments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_supplier_payments_supplier_id'), ['supplier_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_supplier_payments_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('recurring_invoice_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('recurring_id', sa.Integer(), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=False),
    sa.Column('status', RUN_STATUS_ENUM, nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=True),
    sa.Column('skip_reason', sa.Text(), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_by_id', sa.Integer(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['closed_by_id'], ['users.id'], name='fk_recurring_invoice_runs_closed_by'),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], name='fk_recurring_invoice_runs_invoice'),
    sa.ForeignKeyConstraint(['recurring_id'], ['recurring_invoices.id'], name='fk_recurring_invoice_runs_recurring'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_recurring_invoice_runs_tenant'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recurring_id', 'due_date', name='uq_recurring_invoice_runs_due')
    )
    with op.batch_alter_table('recurring_invoice_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_recurring_invoice_runs_recurring_id'), ['recurring_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_recurring_invoice_runs_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('refund_vouchers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gstin_id', sa.Integer(), nullable=False),
    sa.Column('invoice_series_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=64), nullable=False),
    sa.Column('receipt_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('voucher_date', sa.Date(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('mode', MONEY_MODE_ENUM, nullable=False),
    sa.Column('reference', sa.String(length=100), nullable=True),
    sa.Column('taxable_value', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('cgst_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('sgst_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('igst_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('status', VOUCHER_STATUS_ENUM, nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('void_reason', sa.Text(), nullable=True),
    sa.Column('voided_by_id', sa.Integer(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_refund_vouchers_created_by'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name='fk_refund_vouchers_customer'),
    sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_refund_vouchers_gstin'),
    sa.ForeignKeyConstraint(['invoice_series_id'], ['invoice_series.id'], name='fk_refund_vouchers_series'),
    sa.ForeignKeyConstraint(['receipt_id'], ['customer_receipts.id'], name='fk_refund_vouchers_receipt'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_refund_vouchers_tenant'),
    sa.ForeignKeyConstraint(['voided_by_id'], ['users.id'], name='fk_refund_vouchers_voided_by'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'number', name='uq_refund_vouchers_tenant_number')
    )
    with op.batch_alter_table('refund_vouchers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_refund_vouchers_receipt_id'), ['receipt_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_refund_vouchers_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('sales_documents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', SALES_KIND_ENUM, nullable=False),
    sa.Column('gstin_id', sa.Integer(), nullable=False),
    sa.Column('invoice_series_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=64), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('customer_snapshot', sa.JSON(), nullable=False),
    sa.Column('place_of_supply_state_code', sa.String(length=2), nullable=False),
    sa.Column('doc_date', sa.Date(), nullable=False),
    sa.Column('valid_until', sa.Date(), nullable=True),
    sa.Column('invoice_document_type', DOCUMENT_TYPE_ENUM, nullable=False),
    sa.Column('challan_purpose', CHALLAN_PURPOSE_ENUM, nullable=True),
    sa.Column('status', SALES_STATUS_ENUM, nullable=False),
    sa.Column('subtotal', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('total_discount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('total_taxable_value', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('total_cgst', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('total_sgst', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('total_igst', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('round_off', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('grand_total', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('terms', sa.Text(), nullable=True),
    sa.Column('converted_invoice_id', sa.Integer(), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.Column('closed_by_id', sa.Integer(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['closed_by_id'], ['users.id'], name='fk_sales_documents_closed_by'),
    sa.ForeignKeyConstraint(['converted_invoice_id'], ['invoices.id'], name='fk_sales_documents_invoice'),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_sales_documents_created_by'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name='fk_sales_documents_customer'),
    sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_sales_documents_gstin'),
    sa.ForeignKeyConstraint(['invoice_series_id'], ['invoice_series.id'], name='fk_sales_documents_series'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_sales_documents_tenant'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'number', name='uq_sales_documents_tenant_number')
    )
    with op.batch_alter_table('sales_documents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sales_documents_converted_invoice_id'), ['converted_invoice_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_documents_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_documents_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_documents_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('receipt_allocations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('receipt_id', sa.Integer(), nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=True),
    sa.Column('pos_bill_id', sa.Integer(), nullable=True),
    sa.Column('is_opening', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('allocated_on', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], name='fk_receipt_allocations_invoice'),
    sa.ForeignKeyConstraint(['pos_bill_id'], ['pos_bills.id'], name='fk_receipt_allocations_pos_bill'),
    sa.ForeignKeyConstraint(['receipt_id'], ['customer_receipts.id'], name='fk_receipt_allocations_receipt'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('receipt_allocations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_receipt_allocations_invoice_id'), ['invoice_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_receipt_allocations_pos_bill_id'), ['pos_bill_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_receipt_allocations_receipt_id'), ['receipt_id'], unique=False)

    op.create_table('sales_document_lines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=True),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('hsn_or_sac_code', sa.String(length=8), nullable=True),
    sa.Column('qty', sa.Numeric(precision=12, scale=3), nullable=False),
    sa.Column('unit', sa.String(length=20), nullable=False),
    sa.Column('rate', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('discount_percent', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.Column('discount_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('taxable_value', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('gst_rate', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.Column('price_includes_tax', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('cgst_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('sgst_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('igst_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('line_total', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['sales_documents.id'], name='fk_sales_document_lines_document'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name='fk_sales_document_lines_product'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sales_document_lines', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sales_document_lines_document_id'), ['document_id'], unique=False)

    op.create_table('payment_allocations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('payment_id', sa.Integer(), nullable=False),
    sa.Column('purchase_bill_id', sa.Integer(), nullable=True),
    sa.Column('expense_id', sa.Integer(), nullable=True),
    sa.Column('is_opening', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['expense_id'], ['expenses.id'], name='fk_payment_allocations_expense'),
    sa.ForeignKeyConstraint(['payment_id'], ['supplier_payments.id'], name='fk_payment_allocations_payment'),
    sa.ForeignKeyConstraint(['purchase_bill_id'], ['purchase_bills.id'], name='fk_payment_allocations_bill'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('payment_allocations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_payment_allocations_expense_id'), ['expense_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payment_allocations_payment_id'), ['payment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payment_allocations_purchase_bill_id'), ['purchase_bill_id'], unique=False)

    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('opening_balance', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('opening_balance_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('credit_limit', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column('credit_days', sa.Integer(), nullable=True))

    with op.batch_alter_table('suppliers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('opening_balance_date', sa.Date(), nullable=True))

    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('books_opening_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('cash_opening_balance', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('bank_opening_balance', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False))

    op.execute("UPDATE invoice_series SET prefix = 'EV' WHERE document_type = 'EXPENSE' AND prefix = 'EXP'")


def downgrade():
    op.execute("UPDATE invoice_series SET prefix = 'EXP' WHERE document_type = 'EXPENSE' AND prefix = 'EV'")
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('bank_opening_balance')
        batch_op.drop_column('cash_opening_balance')
        batch_op.drop_column('books_opening_date')

    with op.batch_alter_table('suppliers', schema=None) as batch_op:
        batch_op.drop_column('opening_balance_date')

    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_column('credit_days')
        batch_op.drop_column('credit_limit')
        batch_op.drop_column('opening_balance_date')
        batch_op.drop_column('opening_balance')

    with op.batch_alter_table('payment_allocations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_payment_allocations_purchase_bill_id'))
        batch_op.drop_index(batch_op.f('ix_payment_allocations_payment_id'))
        batch_op.drop_index(batch_op.f('ix_payment_allocations_expense_id'))

    op.drop_table('payment_allocations')
    with op.batch_alter_table('sales_document_lines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_document_lines_document_id'))

    op.drop_table('sales_document_lines')
    with op.batch_alter_table('receipt_allocations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_receipt_allocations_receipt_id'))
        batch_op.drop_index(batch_op.f('ix_receipt_allocations_pos_bill_id'))
        batch_op.drop_index(batch_op.f('ix_receipt_allocations_invoice_id'))

    op.drop_table('receipt_allocations')
    with op.batch_alter_table('sales_documents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_documents_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_sales_documents_kind'))
        batch_op.drop_index(batch_op.f('ix_sales_documents_customer_id'))
        batch_op.drop_index(batch_op.f('ix_sales_documents_converted_invoice_id'))

    op.drop_table('sales_documents')
    with op.batch_alter_table('refund_vouchers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_refund_vouchers_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_refund_vouchers_receipt_id'))

    op.drop_table('refund_vouchers')
    with op.batch_alter_table('recurring_invoice_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_recurring_invoice_runs_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_recurring_invoice_runs_recurring_id'))

    op.drop_table('recurring_invoice_runs')
    with op.batch_alter_table('supplier_payments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_supplier_payments_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_supplier_payments_supplier_id'))

    op.drop_table('supplier_payments')
    with op.batch_alter_table('recurring_invoices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_recurring_invoices_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_recurring_invoices_customer_id'))

    op.drop_table('recurring_invoices')
    with op.batch_alter_table('customer_receipts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customer_receipts_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_customer_receipts_customer_id'))

    op.drop_table('customer_receipts')
    bind = op.get_bind()
    for enum in NEW_ENUMS:
        postgresql.ENUM(name=enum.name).drop(bind, checkfirst=True)
    # The new document_type values stay: Postgres can't drop an enum value.
