"""Business + date indexes on the high-volume tables (app/models/indexes.py),
and the four single-column number indexes dropped - every lookup by number
is within a business, which the unique (tenant_id, number) index serves.

Built CONCURRENTLY: the tables stay readable and writable while each index
is built (outside a transaction, as Postgres requires). IF NOT EXISTS / IF
EXISTS make a re-run after an interrupted build safe - except that an
interrupted CONCURRENTLY build leaves an INVALID index behind, which the
upgrade drops and builds again.

Revision ID: f3a9c1e7b254
Revises: e8b2c4d6f013
Create Date: 2026-10-24 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f3a9c1e7b254'
down_revision = 'e8b2c4d6f013'
branch_labels = None
depends_on = None

INDEXES = [
    ("ix_pos_bills_tenant_completed", "pos_bills", "completed_at"),
    ("ix_invoices_tenant_date", "invoices", "invoice_date"),
    ("ix_notes_tenant_date", "credit_debit_notes", "note_date"),
    ("ix_pos_returns_tenant_returned", "pos_returns", "returned_at"),
    ("ix_audit_logs_tenant_created", "audit_logs", "created_at"),
    ("ix_stock_movements_tenant_moved", "stock_movements", "moved_at"),
    ("ix_purchase_bills_tenant_date", "purchase_bills", "bill_date"),
    ("ix_expenses_tenant_date", "expenses", "expense_date"),
    ("ix_customer_receipts_tenant_date", "customer_receipts", "voucher_date"),
    ("ix_supplier_payments_tenant_date", "supplier_payments", "voucher_date"),
    ("ix_cash_movements_tenant_created", "cash_movements", "created_at"),
    ("ix_sales_documents_tenant_date", "sales_documents", "doc_date"),
]
REDUNDANT = [
    ("ix_invoices_invoice_number", "invoices", "invoice_number"),
    ("ix_pos_bills_bill_number", "pos_bills", "bill_number"),
    ("ix_credit_debit_notes_note_number", "credit_debit_notes", "note_number"),
    ("ix_pos_returns_return_number", "pos_returns", "return_number"),
]


def upgrade():
    conn = op.get_bind()
    with op.get_context().autocommit_block():
        for name, table, column in INDEXES:
            invalid = conn.execute(sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :n AND NOT i.indisvalid"), {"n": name}).first()
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} (tenant_id, {column})")
        for name, _table, _column in REDUNDANT:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def downgrade():
    with op.get_context().autocommit_block():
        for name, table, column in REDUNDANT:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({column})")
        for name, _table, _column in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
