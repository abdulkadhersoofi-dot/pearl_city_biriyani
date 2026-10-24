"""Indexes for the high-volume tables, by business and date: the day-book,
GSTR-1/3B, registers, P&L and statements all ask "this business, between
these dates". With only a business index, every such page read the
business's whole history; these read just the days asked for. Kept in one
place so the list can be read at a glance (migration f3a9c1e7b254 builds
them on existing databases)."""

from app.extensions import db
from app.models.accounts import CustomerReceipt, SupplierPayment
from app.models.audit_log import AuditLog
from app.models.cash_session import CashMovement
from app.models.expense import Expense
from app.models.inventory import StockMovement
from app.models.invoice import Invoice
from app.models.note import CreditDebitNote
from app.models.pos_bill import POSBill
from app.models.pos_return import POSReturn
from app.models.purchase import PurchaseBill
from app.models.sales import SalesDocument

# (index name, model, the date column) - each index is (tenant_id, date).
TENANT_DATE_INDEXES = [
    ("ix_pos_bills_tenant_completed", POSBill, "completed_at"),
    ("ix_invoices_tenant_date", Invoice, "invoice_date"),
    ("ix_notes_tenant_date", CreditDebitNote, "note_date"),
    ("ix_pos_returns_tenant_returned", POSReturn, "returned_at"),
    ("ix_audit_logs_tenant_created", AuditLog, "created_at"),
    ("ix_stock_movements_tenant_moved", StockMovement, "moved_at"),
    ("ix_purchase_bills_tenant_date", PurchaseBill, "bill_date"),
    ("ix_expenses_tenant_date", Expense, "expense_date"),
    ("ix_customer_receipts_tenant_date", CustomerReceipt, "voucher_date"),
    ("ix_supplier_payments_tenant_date", SupplierPayment, "voucher_date"),
    ("ix_cash_movements_tenant_created", CashMovement, "created_at"),
    ("ix_sales_documents_tenant_date", SalesDocument, "doc_date"),
]

for _name, _model, _column in TENANT_DATE_INDEXES:
    db.Index(_name, _model.tenant_id, getattr(_model, _column))
