"""expenses: categories, expense vouchers, recurring expenses

Hand-reviewed:
  - the three new enum types are created once, up front, and every
    column refers to them with create_type=False; itc_eligibility already
    exists (Phase 3) and is only referenced. Downgrade drops the new ones.
  - document_type gains 'EXPENSE' (ADD VALUE IF NOT EXISTS, in an
    autocommit block - it can't run inside a transaction). Like every
    enum value, it is left in place on downgrade.
  - every foreign key and unique constraint is named.

Revision ID: 415fbed454ee
Revises: ded5dc02d2e6
Create Date: 2026-10-15 12:57:25.904595

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '415fbed454ee'
down_revision = 'ded5dc02d2e6'
branch_labels = None
depends_on = None


EXPENSE_STATUS_ENUM = postgresql.ENUM('DRAFT', 'POSTED', 'VOIDED', name='expense_status', create_type=False)
EXPENSE_PAYMENT_MODE_ENUM = postgresql.ENUM('CASH', 'UPI', 'CARD', 'BANK', 'CHEQUE', 'UNPAID', name='expense_payment_mode', create_type=False)
EXPENSE_FREQUENCY_ENUM = postgresql.ENUM('WEEKLY', 'MONTHLY', 'QUARTERLY', 'YEARLY', name='expense_frequency', create_type=False)
ITC_ELIGIBILITY_ENUM = postgresql.ENUM('ELIGIBLE', 'INELIGIBLE', 'BLOCKED', name='itc_eligibility', create_type=False)
NEW_ENUMS = [EXPENSE_STATUS_ENUM, EXPENSE_PAYMENT_MODE_ENUM, EXPENSE_FREQUENCY_ENUM]


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE document_type ADD VALUE IF NOT EXISTS 'EXPENSE'")
    bind = op.get_bind()
    for enum in NEW_ENUMS:
        postgresql.ENUM(*enum.enums, name=enum.name).create(bind, checkfirst=True)

    op.create_table('expense_categories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_expense_categories_tenant'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'name', name='uq_expense_categories_tenant_name')
    )
    with op.batch_alter_table('expense_categories', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_expense_categories_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('recurring_expenses',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('category_id', sa.Integer(), nullable=False),
    sa.Column('payee_name', sa.String(length=255), nullable=True),
    sa.Column('supplier_id', sa.Integer(), nullable=True),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('payment_mode', EXPENSE_PAYMENT_MODE_ENUM, nullable=False),
    sa.Column('branch_user_id', sa.Integer(), nullable=True),
    sa.Column('frequency', EXPENSE_FREQUENCY_ENUM, nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('next_due_date', sa.Date(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['branch_user_id'], ['users.id'], name='fk_recurring_expenses_branch_user'),
    sa.ForeignKeyConstraint(['category_id'], ['expense_categories.id'], name='fk_recurring_expenses_category'),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_recurring_expenses_created_by'),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name='fk_recurring_expenses_supplier'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_recurring_expenses_tenant'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('recurring_expenses', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_recurring_expenses_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('expenses',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('invoice_series_id', sa.Integer(), nullable=True),
    sa.Column('number', sa.String(length=64), nullable=True),
    sa.Column('status', EXPENSE_STATUS_ENUM, nullable=False),
    sa.Column('expense_date', sa.Date(), nullable=False),
    sa.Column('category_id', sa.Integer(), nullable=False),
    sa.Column('payee_name', sa.String(length=255), nullable=True),
    sa.Column('supplier_id', sa.Integer(), nullable=True),
    sa.Column('payment_mode', EXPENSE_PAYMENT_MODE_ENUM, nullable=False),
    sa.Column('reference', sa.String(length=100), nullable=True),
    sa.Column('branch_user_id', sa.Integer(), nullable=True),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('is_gst_bill', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('gstin_id', sa.Integer(), nullable=True),
    sa.Column('supplier_gstin', sa.String(length=15), nullable=True),
    sa.Column('supplier_state_code', sa.String(length=2), nullable=True),
    sa.Column('bill_number', sa.String(length=50), nullable=True),
    sa.Column('is_rcm', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('gst_rate', sa.Numeric(precision=5, scale=2), server_default='0', nullable=False),
    sa.Column('taxable_value', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('cgst_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('sgst_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('igst_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('itc_eligibility', ITC_ELIGIBILITY_ENUM, nullable=False),
    sa.Column('itc_cgst', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('itc_sgst', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('itc_igst', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('attachment_id', sa.Integer(), nullable=True),
    sa.Column('recurring_id', sa.Integer(), nullable=True),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('cash_movement_id', sa.Integer(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('void_reason', sa.Text(), nullable=True),
    sa.Column('voided_by_id', sa.Integer(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['attachment_id'], ['uploaded_images.id'], name='fk_expenses_attachment'),
    sa.ForeignKeyConstraint(['branch_user_id'], ['users.id'], name='fk_expenses_branch_user'),
    sa.ForeignKeyConstraint(['cash_movement_id'], ['cash_movements.id'], name='fk_expenses_cash_movement'),
    sa.ForeignKeyConstraint(['category_id'], ['expense_categories.id'], name='fk_expenses_category'),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_expenses_created_by'),
    sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_expenses_gstin'),
    sa.ForeignKeyConstraint(['invoice_series_id'], ['invoice_series.id'], name='fk_expenses_series'),
    sa.ForeignKeyConstraint(['recurring_id'], ['recurring_expenses.id'], name='fk_expenses_recurring'),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name='fk_expenses_supplier'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_expenses_tenant'),
    sa.ForeignKeyConstraint(['voided_by_id'], ['users.id'], name='fk_expenses_voided_by'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('cash_movement_id', name='uq_expenses_cash_movement'),
    sa.UniqueConstraint('recurring_id', 'due_date', name='uq_expenses_recurring_due'),
    sa.UniqueConstraint('tenant_id', 'number', name='uq_expenses_tenant_number')
    )
    with op.batch_alter_table('expenses', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_expenses_branch_user_id'), ['branch_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_expenses_recurring_id'), ['recurring_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_expenses_tenant_id'), ['tenant_id'], unique=False)



def downgrade():
    with op.batch_alter_table('expenses', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_expenses_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_expenses_recurring_id'))
        batch_op.drop_index(batch_op.f('ix_expenses_branch_user_id'))

    op.drop_table('expenses')
    with op.batch_alter_table('recurring_expenses', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_recurring_expenses_tenant_id'))

    op.drop_table('recurring_expenses')
    with op.batch_alter_table('expense_categories', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_expense_categories_tenant_id'))

    op.drop_table('expense_categories')
    bind = op.get_bind()
    for enum in NEW_ENUMS:
        postgresql.ENUM(name=enum.name).drop(bind, checkfirst=True)
    # 'EXPENSE' stays in document_type: Postgres can't drop an enum value.
