"""Credit and debit notes from suppliers (SupplierNote).

Revision ID: a7b3e5d9c124
Revises: f6a9d4c2e813
Create Date: 2026-11-03 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'a7b3e5d9c124'
down_revision = 'f6a9d4c2e813'
branch_labels = None
depends_on = None


def upgrade():
    status = postgresql.ENUM('POSTED', 'VOIDED', name='purchase_status', create_type=False)
    op.create_table(
        'supplier_notes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('gstin_id', sa.Integer(), nullable=False),
        sa.Column('supplier_id', sa.Integer(), nullable=False),
        sa.Column('supplier_snapshot', sa.JSON(), nullable=False),
        sa.Column('note_number', sa.String(length=50), nullable=False),
        sa.Column('note_date', sa.Date(), nullable=False),
        sa.Column('purchase_bill_id', sa.Integer(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('lines', sa.JSON(), nullable=False),
        sa.Column('total_taxable_value', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_cgst', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_sgst', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_igst', sa.Numeric(12, 2), nullable=False),
        sa.Column('round_off', sa.Numeric(12, 2), nullable=False),
        sa.Column('grand_total', sa.Numeric(12, 2), nullable=False),
        sa.Column('itc_cgst', sa.Numeric(12, 2), nullable=False),
        sa.Column('itc_sgst', sa.Numeric(12, 2), nullable=False),
        sa.Column('itc_igst', sa.Numeric(12, 2), nullable=False),
        sa.Column('status', status, nullable=False),
        sa.Column('attachment_id', sa.Integer(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=False),
        sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('void_reason', sa.Text(), nullable=True),
        sa.Column('voided_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_supplier_notes_gstin'),
        sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name='fk_supplier_notes_supplier'),
        sa.ForeignKeyConstraint(['purchase_bill_id'], ['purchase_bills.id'], name='fk_supplier_notes_bill'),
        sa.ForeignKeyConstraint(['attachment_id'], ['uploaded_images.id'], name='fk_supplier_notes_attachment'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_supplier_notes_created_by'),
        sa.ForeignKeyConstraint(['voided_by_id'], ['users.id'], name='fk_supplier_notes_voided_by'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_supplier_notes_tenant_id', 'supplier_notes', ['tenant_id'])
    op.create_index('ix_supplier_notes_purchase_bill_id', 'supplier_notes', ['purchase_bill_id'])
    op.create_index('ix_supplier_notes_tenant_supplier', 'supplier_notes', ['tenant_id', 'supplier_id'])


def downgrade():
    op.drop_index('ix_supplier_notes_tenant_supplier', table_name='supplier_notes')
    op.drop_index('ix_supplier_notes_purchase_bill_id', table_name='supplier_notes')
    op.drop_index('ix_supplier_notes_tenant_id', table_name='supplier_notes')
    op.drop_table('supplier_notes')
