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


def daily_sales(tenant, start: date, end: date) -> list[tuple[date, Decimal]]:
    """Sales for each day from start to end - the same rule as _sales
    (counter bills less refunds and returns, plus issued sales invoices),
    split by local day, read in four queries instead of four per day. The
    days add up to _sales over the same range."""
    lo, hi = day_start(start), day_start(end + timedelta(days=1))
    days = {start + timedelta(days=i): ZERO for i in range((end - start).days + 1)}

    def add(when, amount):
        day = when.astimezone().date() if hasattr(when, "astimezone") else when
        if day in days:
            days[day] += Decimal(amount or 0)

    for bill in POSBill.query.filter(
            POSBill.tenant_id == tenant.id, POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
            POSBill.completed_at >= lo, POSBill.completed_at < hi):
        add(bill.completed_at, bill.grand_total)
    for bill in POSBill.query.filter(
            POSBill.tenant_id == tenant.id, POSBill.status == POSBillStatus.REFUNDED,
            POSBill.refunded_at >= lo, POSBill.refunded_at < hi):
        add(bill.refunded_at, -Decimal(bill.refunded_amount or 0))
    for ret in POSReturn.query.filter(
            POSReturn.tenant_id == tenant.id, POSReturn.status == POSReturnStatus.COMPLETED,
            POSReturn.returned_at >= lo, POSReturn.returned_at < hi):
        add(ret.returned_at, -Decimal(ret.grand_total))
    for inv in Invoice.query.filter(
            Invoice.tenant_id == tenant.id, Invoice.status == InvoiceStatus.ISSUED,
            Invoice.document_type.notin_(NOT_SALES), Invoice.invoice_date >= start, Invoice.invoice_date <= end):
        add(inv.invoice_date, inv.grand_total)
    return sorted(days.items())


def change(now: Decimal, before: Decimal) -> dict | None:
    """How `now` compares with `before`: percent (rounded), direction. None
    when there's nothing to compare with."""
    if not before:
        return None
    pct = ((now - before) / abs(before) * 100).quantize(Decimal("1"))
    return {"pct": abs(pct), "up": now >= before, "same": pct == 0}


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
    # The last 14 days for the trend, and what each headline figure is
    # compared with: the same weekday last week, and the same days of last
    # month.
    data["series"] = daily_sales(tenant, today - timedelta(days=13), today)
    week_ago = today - timedelta(days=7)
    data["week_ago"] = week_ago
    data["sales_vs_week"] = change(data["sales_today"], dict(data["series"])[week_ago])
    data["collections_vs_week"] = change(data["collections_today"], _collections(tenant, week_ago, week_ago))
    prev_end = month_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    prev_same = prev_start.replace(day=min(today.day, prev_end.day))
    data["sales_vs_last_month"] = change(data["sales_month"], _sales(tenant, prev_start, prev_same))
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
