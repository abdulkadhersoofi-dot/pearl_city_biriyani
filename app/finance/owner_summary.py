"""The owner's end-of-day summary (feature "daily_summary"): one day at a
glance - sales, how it was paid, each counter, the top items, stock that
is low or has gone below zero - on the dashboard, as a WhatsApp message
(wa.me click-to-chat, no WhatsApp API) and, when SMTP is configured, as
an evening email (`flask owner-summary-send`).

Every figure comes from the module that owns it (daily_summary,
item_gross_profit, the stock ledger); nothing is computed twice."""

import smtplib
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from email.message import EmailMessage
from urllib.parse import quote

from flask import current_app

from app.finance.dashboard import _sales
from app.finance.pnl import ZERO, day_start
from app.models.pos_bill import POSBill, POSBillStatus
from app.models.user import User, UserRole


def owner_summary(tenant, day: date | None = None) -> dict:
    from app.finance.sales_analysis import daily_summary, item_gross_profit

    day = day or date.today()
    daily = daily_summary(tenant, day, day)
    lo, hi = day_start(day), day_start(day + timedelta(days=1))
    counters = defaultdict(lambda: {"bills": 0, "total": ZERO})
    names = {u.id: u.name for u in User.query.filter_by(tenant_id=tenant.id)}
    for bill in POSBill.query.filter(POSBill.tenant_id == tenant.id,
                                     POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
                                     POSBill.completed_at >= lo, POSBill.completed_at < hi):
        row = counters[names.get(bill.created_by_id, "-")]
        row["bills"] += 1
        row["total"] += Decimal(bill.grand_total)
    items = item_gross_profit(tenant, day, day)["rows"]
    data = {
        "day": day,
        "net_sales": _sales(tenant, day, day),
        "bill_count": daily["bill_count"], "bill_total": daily["bill_total"],
        "invoice_count": daily["invoice_count"], "invoice_total": daily["invoice_total"],
        "modes": [m for m in daily["modes"] if m["sales"] or m["refunds"]],
        "counters": sorted(({"name": k, **v} for k, v in counters.items()), key=lambda r: -r["total"]),
        "top_items": sorted((r for r in items if r.sales), key=lambda r: -r.sales)[:5],
        "voids": len(daily["voids"]), "returns": len(daily["returns"]),
        "low_stock": [], "oversold": [], "offline_problems": 0,
    }
    if tenant.has_feature("inventory"):
        from app.inventory.reports import low_stock

        data["low_stock"] = low_stock(tenant.id)[:8]
        data["oversold"] = oversold(tenant)[:8]
    if tenant.has_feature("offline_pos"):
        from app.models.platform import OfflineBill, OfflineBillStatus

        data["offline_problems"] = OfflineBill.query.filter_by(tenant_id=tenant.id,
                                                               status=OfflineBillStatus.FAILED).count()
    return data


def oversold(tenant) -> list[dict]:
    """Items whose stock has gone below zero somewhere - sold before it was
    received (allowed for items that may sell at zero, and for offline
    bills) - by location, most negative first."""
    from app.inventory import ledger
    from app.models.product import Product
    from app.models.inventory import StockLocation

    rows = [(key, qty) for key, qty in ledger.stock_by_location(tenant.id).items() if qty < 0]
    if not rows:
        return []
    products = {p.id: p for p in Product.query.filter(Product.tenant_id == tenant.id,
                                                      Product.id.in_({pid for (pid, _), _ in rows}))}
    locations = {loc.id: loc for loc in StockLocation.query.filter_by(tenant_id=tenant.id)}
    out = []
    for (pid, loc_id), qty in sorted(rows, key=lambda r: r[1]):
        product = products.get(pid)
        if product is None:
            continue
        out.append({"name": product.name, "unit": product.unit, "qty": qty,
                    "location": locations[loc_id].name if loc_id in locations else "-"})
    return out


def _rs(value) -> str:
    return f"Rs. {Decimal(value or 0):,.2f}"


def summary_text(tenant, data: dict) -> str:
    """The summary as a short plain-text message (WhatsApp / email)."""
    lines = [f"{tenant.display_name} - {data['day'].strftime('%a %d %b %Y')}",
             f"Sales: {_rs(data['net_sales'])}",
             f"Counter bills: {data['bill_count']} ({_rs(data['bill_total'])})"]
    if data["invoice_count"]:
        lines.append(f"Invoices: {data['invoice_count']} ({_rs(data['invoice_total'])})")
    if data["modes"]:
        lines.append("Paid by: " + ", ".join(f"{m['label']} {_rs(m['net'])}" for m in data["modes"]))
    if len(data["counters"]) > 1:
        lines.append("Counters: " + ", ".join(f"{c['name']} {_rs(c['total'])}" for c in data["counters"]))
    if data["top_items"]:
        lines.append("Top items: " + ", ".join(f"{r.name} ({_rs(r.sales)})" for r in data["top_items"][:3]))
    if data["voids"] or data["returns"]:
        lines.append(f"Voided bills: {data['voids']}, returns: {data['returns']}")
    if data["low_stock"]:
        lines.append("Low stock: " + ", ".join(r.product.name for r in data["low_stock"][:5]))
    if data["oversold"]:
        lines.append("Below zero: " + ", ".join(f"{r['name']} ({r['qty'].normalize():f} {r['unit']})" for r in data["oversold"][:5]))
    if data["offline_problems"]:
        lines.append(f"Offline bills waiting for you: {data['offline_problems']}")
    return "\n".join(lines)


def whatsapp_link(text: str, phone: str | None = None) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    return f"https://wa.me/{digits}?text={quote(text)}" if digits else f"https://wa.me/?text={quote(text)}"


def email_ready() -> bool:
    return bool(current_app.config.get("SMTP_HOST") and current_app.config.get("SMTP_FROM"))


def owner_emails(tenant) -> list[str]:
    return [u.email for u in User.query.filter_by(tenant_id=tenant.id, role=UserRole.CLIENT_ADMIN, is_active=True)
            if u.email]


def send_email(tenant, data: dict, recipients: list[str] | None = None) -> list[str]:
    """Emails the summary to the client's admins over the configured SMTP
    server. Returns who it went to; nothing is sent without SMTP set up."""
    if not email_ready():
        return []
    recipients = recipients or owner_emails(tenant)
    if not recipients:
        return []
    cfg = current_app.config
    message = EmailMessage()
    message["Subject"] = f"{tenant.display_name}: day summary {data['day'].strftime('%d %b %Y')}"
    message["From"] = cfg["SMTP_FROM"]
    message["To"] = ", ".join(recipients)
    message.set_content(summary_text(tenant, data) + "\n\n- " + cfg["FIRM_NAME"])
    with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=20) as smtp:
        if cfg.get("SMTP_USE_TLS"):
            smtp.starttls()
        if cfg.get("SMTP_USERNAME"):
            smtp.login(cfg["SMTP_USERNAME"], cfg.get("SMTP_PASSWORD") or "")
        smtp.send_message(message)
    return recipients
