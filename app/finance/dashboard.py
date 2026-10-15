"""The Client Admin's home screen (feature "business_reports"): today and
this month at a glance. Each tile reads from the module that owns the
figure, and only appears when that module is on."""

from datetime import date, timedelta
from decimal import Decimal

from app.finance.pnl import NOT_SALES, ZERO, day_start
from app.models.accounts import ChequeStatus, CustomerReceipt, VoucherStatus
from app.models.invoice import Invoice, InvoiceStatus
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus, POSPayment
from app.models.pos_return import POSReturn, POSReturnStatus

NO_MONEY = (PaymentMode.CREDIT, PaymentMode.EXCHANGE)


def _sales(tenant, start: date, end: date) -> Decimal:
    lo, hi = day_start(start), day_start(end + timedelta(days=1))
    pos = sum((Decimal(b.grand_total) for b in POSBill.query.filter(
        POSBill.tenant_id == tenant.id, POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
        POSBill.completed_at >= lo, POSBill.completed_at < hi)), ZERO)
    refunds = sum((Decimal(b.refunded_amount or 0) for b in POSBill.query.filter(
        POSBill.tenant_id == tenant.id, POSBill.status == POSBillStatus.REFUNDED,
        POSBill.refunded_at >= lo, POSBill.refunded_at < hi)), ZERO)
    returns = sum((Decimal(r.grand_total) for r in POSReturn.query.filter(
        POSReturn.tenant_id == tenant.id, POSReturn.status == POSReturnStatus.COMPLETED,
        POSReturn.returned_at >= lo, POSReturn.returned_at < hi)), ZERO)
    invoices = sum((Decimal(i.grand_total) for i in Invoice.query.filter(
        Invoice.tenant_id == tenant.id, Invoice.status == InvoiceStatus.ISSUED,
        Invoice.document_type.notin_(NOT_SALES), Invoice.invoice_date >= start, Invoice.invoice_date <= end)), ZERO)
    return pos - refunds - returns + invoices


def _collections(tenant, start: date, end: date) -> Decimal:
    """Money actually received: counter payments (not credit / exchange)
    and receipts from customers (a bounced cheque isn't money)."""
    lo, hi = day_start(start), day_start(end + timedelta(days=1))
    counter = sum((Decimal(p.amount) for p in POSPayment.query.join(POSBill).filter(
        POSBill.tenant_id == tenant.id, POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
        POSBill.completed_at >= lo, POSBill.completed_at < hi, POSPayment.mode.notin_(NO_MONEY))), ZERO)
    receipts = ZERO
    if tenant.has_feature("receivables"):
        receipts = sum((Decimal(r.amount) for r in CustomerReceipt.query.filter(
            CustomerReceipt.tenant_id == tenant.id, CustomerReceipt.status == VoucherStatus.POSTED,
            CustomerReceipt.voucher_date >= start, CustomerReceipt.voucher_date <= end)
            if r.cheque_status != ChequeStatus.BOUNCED), ZERO)
    return counter + receipts


def dashboard(tenant, today: date | None = None) -> dict:
    today = today or date.today()
    month_start = today.replace(day=1)
    data = {
        "today": today,
        "sales_today": _sales(tenant, today, today),
        "sales_month": _sales(tenant, month_start, today),
        "collections_today": _collections(tenant, today, today),
        "collections_month": _collections(tenant, month_start, today),
    }
    if tenant.has_feature("receivables"):
        from app.accounts.receivables import customer_balances

        rows = customer_balances(tenant, today)
        data["receivable"] = sum((r["balance"] for r in rows if r["balance"] > 0), ZERO)
        data["overdue"] = sum((r["overdue"] for r in rows), ZERO)
        data["top_debtors"] = sorted((r for r in rows if r["balance"] > 0), key=lambda r: -r["balance"])[:5]
    if tenant.has_feature("payables"):
        from app.accounts.payables import supplier_balances

        data["payable"] = sum((r["balance"] for r in supplier_balances(tenant, today) if r["balance"] > 0), ZERO)
    from app.finance.sales_analysis import item_gross_profit

    items = item_gross_profit(tenant, month_start, today)["rows"]
    data["top_items"] = sorted(items, key=lambda r: -r.sales)[:5]
    if tenant.has_feature("inventory"):
        from app.inventory.reports import low_stock

        data["low_stock"] = low_stock(tenant.id)[:8]
    if tenant.has_feature("batch_expiry"):
        from app.inventory.reports import expiry_report

        expired, soon = expiry_report(tenant.id, 30)
        data["expiring"] = (expired + soon)[:8]
    if tenant.has_feature("cash_bank_book"):
        from app.accounts.books import book

        data["cash_in_hand"] = book(tenant, "cash", today, today)["closing"]
        data["cash_label"] = "Cash in hand (cash book)"
    else:
        lo, hi = day_start(today), day_start(today + timedelta(days=1))
        cash = sum((Decimal(p.amount) for p in POSPayment.query.join(POSBill).filter(
            POSBill.tenant_id == tenant.id, POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
            POSBill.completed_at >= lo, POSBill.completed_at < hi, POSPayment.mode == PaymentMode.CASH)), ZERO)
        data["cash_in_hand"] = cash
        data["cash_label"] = "Cash taken at the counter today"
    return data
