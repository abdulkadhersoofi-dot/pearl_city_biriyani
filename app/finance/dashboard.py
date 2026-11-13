"""The Client Admin's home screen (feature "business_reports"): today and
this month at a glance. Each tile reads from the module that owns the
figure, and only appears when that module is on."""

from datetime import date, timedelta
from decimal import Decimal

from app.finance import sales_figures
from app.finance.pnl import ZERO, day_start
from app.models.accounts import ChequeStatus, CustomerReceipt, VoucherStatus
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus, POSPayment

NO_MONEY = (PaymentMode.CREDIT, PaymentMode.EXCHANGE)


def _sales(tenant, start: date, end: date) -> Decimal:
    """Sales for the period, tax included - the one definition every screen
    uses (app/finance/sales_figures.py)."""
    return sales_figures.total(tenant, start, end)


def daily_sales(tenant, start: date, end: date) -> list[tuple[date, Decimal]]:
    """Sales for each day from start to end, by the same rule as _sales.
    The days add up to _sales over the same range."""
    return sales_figures.by_day(tenant, start, end)


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
