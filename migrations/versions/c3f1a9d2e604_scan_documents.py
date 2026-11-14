"""Scanned bills: scan_documents (app/models/scan.py, module doc_scan).

Revision ID: c3f1a9d2e604
Revises: b7e2d9c4a815
Create Date: 2026-10-31 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c3f1a9d2e604'
down_revision = 'b7e2d9c4a815'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'scan_documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('uploaded_by_id', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Integer(), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=50), nullable=False),
        sa.Column('file_hash', sa.String(length=64), nullable=False),
        sa.Column('file_data', sa.LargeBinary(), nullable=False),
        sa.Column('preview', sa.LargeBinary(), nullable=True),
        sa.Column('page_count', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='uploaded', nullable=False),
        sa.Column('category', sa.String(length=40), nullable=True),
        sa.Column('extracted', sa.JSON(), nullable=True),
        sa.Column('validation', sa.JSON(), nullable=True),
        sa.Column('engine', sa.JSON(), nullable=True),
        sa.Column('rejection_code', sa.String(length=30), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('gstin_id', sa.Integer(), nullable=True),
        sa.Column('duplicate_of', sa.String(length=120), nullable=True),
        sa.Column('posted_type', sa.String(length=20), nullable=True),
        sa.Column('posted_id', sa.Integer(), nullable=True),
        sa.Column('posted_by_id', sa.Integer(), nullable=True),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.ForeignKeyConstraint(['uploaded_by_id'], ['users.id'], name='fk_scan_documents_uploaded_by'),
        sa.ForeignKeyConstraint(['parent_id'], ['scan_documents.id'], name='fk_scan_documents_parent'),
        sa.ForeignKeyConstraint(['gstin_id'], ['gstins.id'], name='fk_scan_documents_gstin'),
        sa.ForeignKeyConstraint(['posted_by_id'], ['users.id'], name='fk_scan_documents_posted_by'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_scan_documents_tenant_id', 'scan_documents', ['tenant_id'])
    op.create_index('ix_scan_documents_parent_id', 'scan_documents', ['parent_id'])
    op.create_index('ix_scan_documents_file_hash', 'scan_documents', ['file_hash'])
    op.create_index('ix_scan_documents_status', 'scan_documents', ['status'])
    op.create_index('ix_scan_documents_tenant_created', 'scan_documents', ['tenant_id', 'created_at'])


def downgrade():
    op.drop_index('ix_scan_documents_tenant_created', table_name='scan_documents')
    op.drop_index('ix_scan_documents_status', table_name='scan_documents')
    op.drop_index('ix_scan_documents_file_hash', table_name='scan_documents')
    op.drop_index('ix_scan_documents_parent_id', table_name='scan_documents')
    op.drop_index('ix_scan_documents_tenant_id', table_name='scan_documents')
    op.drop_table('scan_documents')
