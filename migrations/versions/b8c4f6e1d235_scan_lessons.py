"""Teach ARFA: what the bill reader has learnt (corrections) and been told (notes).

Revision ID: b8c4f6e1d235
Revises: a7b3e5d9c124
Create Date: 2026-11-04 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b8c4f6e1d235'
down_revision = 'a7b3e5d9c124'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'scan_lessons',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('supplier_gstin', sa.String(length=15), nullable=True),
        sa.Column('supplier_name', sa.String(length=255), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('example', sa.JSON(), nullable=True),
        sa.Column('source_doc_id', sa.Integer(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('active', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('uses', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_scan_lessons_tenant'),
        sa.ForeignKeyConstraint(['source_doc_id'], ['scan_documents.id'], name='fk_scan_lessons_doc'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_scan_lessons_created_by'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_scan_lessons_tenant_id', 'scan_lessons', ['tenant_id'])
    op.create_index('ix_scan_lessons_supplier_gstin', 'scan_lessons', ['supplier_gstin'])


def downgrade():
    op.drop_index('ix_scan_lessons_supplier_gstin', table_name='scan_lessons')
    op.drop_index('ix_scan_lessons_tenant_id', table_name='scan_lessons')
    op.drop_table('scan_lessons')
