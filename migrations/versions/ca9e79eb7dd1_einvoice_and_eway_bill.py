"""einvoice and eway bill: per-GSTIN provider credentials (secrets
encrypted), a log of every IRP / e-way bill call, e-way bills, IRN columns
on credit / debit notes, IRN cancellation on invoices, seller city / PIN on
GSTINs.

Hand-reviewed:
  - the new enum eway_bill_status is created once, up front, and the
    column refers to it with create_type=False; downgrade drops it.
  - every foreign key is named; new booleans / amounts have server
    defaults; all new columns on existing tables are nullable, so existing
    rows need no backfill.

Revision ID: ca9e79eb7dd1
Revises: 73ad4085e5ba
Create Date: 2026-10-16 04:23:44.652072

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

EWB_STATUS = postgresql.ENUM('DRAFT', 'GENERATED', 'CANCELLED', name='eway_bill_status', create_type=False)


# revision identifiers, used by Alembic.
revision = 'ca9e79eb7dd1'
down_revision = '73ad4085e5ba'
branch_labels = None
depends_on = None


def upgrade():
    postgresql.ENUM(*EWB_STATUS.enums, name=EWB_STATUS.name).create(op.get_bind(), checkfirst=True)
    op.create_table('einvoice_credentials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gstin_id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=30), nullable=False),
    sa.Column('username', sa.String(length=100), nullable=False),
    sa.Column('secrets', sa.Text(), nullable=False),
    sa.Column('session', sa.Text(), nullable=True),
    sa.Column('token_expiry', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('updated_by_id', sa.Integer(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_einvoice_credentials_gstin'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_einvoice_credentials_tenant'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], name='fk_einvoice_credentials_updated_by'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('gstin_id', name='uq_einvoice_credentials_gstin')
    )
    with op.batch_alter_table('einvoice_credentials', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_einvoice_credentials_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('eway_bills',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gstin_id', sa.Integer(), nullable=False),
    sa.Column('source_type', sa.String(length=30), nullable=False),
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.Column('document_number', sa.String(length=64), nullable=False),
    sa.Column('document_date', sa.Date(), nullable=False),
    sa.Column('doc_type', sa.String(length=3), nullable=False),
    sa.Column('supply_type', sa.String(length=1), server_default='O', nullable=False),
    sa.Column('sub_supply_type', sa.String(length=2), nullable=False),
    sa.Column('sub_supply_desc', sa.String(length=50), nullable=True),
    sa.Column('from_party', sa.JSON(), nullable=False),
    sa.Column('to_party', sa.JSON(), nullable=False),
    sa.Column('items', sa.JSON(), nullable=False),
    sa.Column('total_value', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('cgst_value', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False),
    sa.Column('sgst_value', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False),
    sa.Column('igst_value', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False),
    sa.Column('total_invoice_value', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('trans_mode', sa.String(length=1), server_default='1', nullable=False),
    sa.Column('distance_km', sa.Integer(), server_default='0', nullable=False),
    sa.Column('transporter_id', sa.String(length=15), nullable=True),
    sa.Column('transporter_name', sa.String(length=100), nullable=True),
    sa.Column('trans_doc_no', sa.String(length=15), nullable=True),
    sa.Column('trans_doc_date', sa.Date(), nullable=True),
    sa.Column('vehicle_no', sa.String(length=20), nullable=True),
    sa.Column('vehicle_type', sa.String(length=1), server_default='R', nullable=False),
    sa.Column('status', EWB_STATUS, nullable=False),
    sa.Column('ewb_no', sa.String(length=20), nullable=True),
    sa.Column('ewb_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('valid_upto', sa.DateTime(timezone=True), nullable=True),
    sa.Column('generated_via', sa.String(length=10), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_eway_bills_created_by'),
    sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_eway_bills_gstin'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_eway_bills_tenant'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('eway_bills', schema=None) as batch_op:
        batch_op.create_index('ix_eway_bills_source', ['tenant_id', 'source_type', 'source_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_eway_bills_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('gov_api_calls',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gstin_id', sa.Integer(), nullable=True),
    sa.Column('provider', sa.String(length=30), nullable=False),
    sa.Column('action', sa.String(length=30), nullable=False),
    sa.Column('document_type', sa.String(length=30), nullable=True),
    sa.Column('document_id', sa.Integer(), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=False),
    sa.Column('success', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('error_code', sa.String(length=30), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('plain_message', sa.Text(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_gov_api_calls_created_by'),
    sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_gov_api_calls_gstin'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_gov_api_calls_tenant'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('gov_api_calls', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_gov_api_calls_tenant_id'), ['tenant_id'], unique=False)

    with op.batch_alter_table('credit_debit_notes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('irn', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('irn_ack_number', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('irn_ack_date', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('qr_code_data', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('irn_cancelled_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('irn_cancel_reason', sa.Text(), nullable=True))

    with op.batch_alter_table('gstins', schema=None) as batch_op:
        batch_op.add_column(sa.Column('city', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('pincode', sa.String(length=6), nullable=True))

    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('irn_cancelled_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('irn_cancel_reason', sa.Text(), nullable=True))



def downgrade():
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_column('irn_cancel_reason')
        batch_op.drop_column('irn_cancelled_at')

    with op.batch_alter_table('gstins', schema=None) as batch_op:
        batch_op.drop_column('pincode')
        batch_op.drop_column('city')

    with op.batch_alter_table('credit_debit_notes', schema=None) as batch_op:
        batch_op.drop_column('irn_cancel_reason')
        batch_op.drop_column('irn_cancelled_at')
        batch_op.drop_column('qr_code_data')
        batch_op.drop_column('irn_ack_date')
        batch_op.drop_column('irn_ack_number')
        batch_op.drop_column('irn')

    with op.batch_alter_table('gov_api_calls', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_gov_api_calls_tenant_id'))

    op.drop_table('gov_api_calls')
    with op.batch_alter_table('eway_bills', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_eway_bills_tenant_id'))
        batch_op.drop_index('ix_eway_bills_source')

    op.drop_table('eway_bills')
    with op.batch_alter_table('einvoice_credentials', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_einvoice_credentials_tenant_id'))

    op.drop_table('einvoice_credentials')
    postgresql.ENUM(name=EWB_STATUS.name).drop(op.get_bind(), checkfirst=True)
