"""The Reports hub (redesign stage 4): every report in one place, grouped
by what it answers - Sales, Profit, Stock, GST, Accounts & registers, and
the business-type modules' own registers - each behind the same feature
flag as its route, so a switched-off module's reports never show."""

from dataclasses import dataclass, field

from flask import url_for

from app.utils.features import current_tenant_has


@dataclass(frozen=True)
class Report:
    title: str
    description: str
    endpoint: str
    icon: str
    features: tuple = ()
    kwargs: dict = field(default_factory=dict)

    def visible(self) -> bool:
        return all(current_tenant_has(f) for f in self.features)


GROUPS = [
    ("sales", "Sales", "receipt-text", [
        Report("Sales report", "Every invoice and counter bill across your GSTINs - by day, month or any range, "
               "by branch. Excel or PDF.", "reports.sales_view", "receipt-text"),
        Report("Sales by item", "Which items sold, how many and for how much, with the same period and branch "
               "filters.", "reports.items_view", "package"),
        Report("Daily sales summary", "Payment modes, sales by hour and by cashier, discounts, voids and returns.",
               "reports.daily_summary_view", "clock", ("business_reports",)),
        Report("Owner's day summary", "The day in one message - share it on WhatsApp or get it by email.",
               "reports.owner_summary", "file-text", ("daily_summary",)),
        Report("Live", "Branches racing on today's sales, items selling now and stock moving.",
               "live.dashboard", "activity"),
        Report("Sales by salesman", "Sales and collections for each salesman.", "wholesale.salesmen_report",
               "users", ("wholesale",)),
    ]),
    ("profit", "Profit", "trending-up", [
        Report("Profit & loss", "Revenue, cost of goods sold, gross profit and expenses for any period - all "
               "locations or one - with last period or last year beside it.", "reports.profit_loss", "trending-up",
               ("business_reports",)),
        Report("Gross profit by item", "What each item and category sold for against what it cost you.",
               "reports.item_profit", "chart-column", ("business_reports",)),
        Report("Expense report", "Expenses by category, branch and period, with the GST you can claim.",
               "expenses.report", "wallet", ("expenses",)),
    ]),
    ("stock", "Stock", "warehouse", [
        Report("Stock report", "How much stock went to each branch and how much it sold, over any period.",
               "reports.stock_view", "warehouse"),
        Report("Stock on hand", "What's in each store, godown and branch right now, and its value.",
               "inventory.index", "boxes", ("inventory",)),
        Report("Fast, slow and dead stock", "Which items sell quickly, which barely move and which haven't sold.",
               "inventory.movement", "activity", ("inventory",)),
        Report("Reorder list", "Items at or below their minimum, with how much to order and from whom.",
               "inventory.reorder", "triangle-alert", ("inventory",)),
        Report("Expiry", "Batches already expired or expiring soon, by location.", "inventory.expiry",
               "calendar-clock", ("inventory", "batch_expiry")),
    ]),
    ("gst", "GST", "file-check", [
        Report("GST filing", "Tell your accountant a month's data is complete, and see when GSTR-1 and GSTR-3B "
               "were filed.", "reports.filing_status", "calendar-clock", ("business_reports",)),
        Report("Margin register", "Every second-hand unit sold under the margin scheme (Rule 32(5)) - purchase "
               "and sale, margin and GST. Excel or PDF.", "reports.margin_register", "coins", ("margin_scheme",)),
        Report("e-Invoice log", "Every IRN generated or cancelled, with the portal's replies.", "einvoice.log",
               "scan-line", ("einvoice",)),
    ]),
    ("accounts", "Accounts & registers", "landmark", [
        Report("Customer accounts", "Who owes you, how long it's been due, and statements to send.",
               "accounts.receivables_index", "users", ("receivables",)),
        Report("Supplier accounts", "What you owe each supplier and when it's due.", "accounts.payables_index",
               "truck", ("payables",)),
        Report("Cash book", "Cash in hand day by day - sales, receipts, payments and expenses.", "accounts.book",
               "wallet", ("cash_bank_book",), {"book": "cash"}),
        Report("Bank book", "Your bank balance day by day.", "accounts.book", "landmark", ("cash_bank_book",),
               {"book": "bank"}),
    ]),
    ("industry", "Your industry", "layout-grid", [
        Report("Scheduled drug register", "Schedule H, H1 and X sales with the doctor's and patient's details.",
               "pharmacy.register", "pill", ("pharmacy",)),
        Report("Old gold vouchers", "Old gold bought from customers, with weight, purity and value.",
               "jewellery.old_gold_list", "coins", ("jewellery",)),
        Report("Warranty check", "Look up a sold unit by IMEI or serial and see its warranty.", "jobs.warranty",
               "shield-check", ("electronics",)),
        Report("Loyalty points", "Points earned and spent, customer by customer.", "loyalty.index", "gift",
               ("loyalty",)),
    ]),
]


def hub_groups() -> list[dict]:
    """[{key, label, icon, reports: [{title, description, url, icon}]}] -
    only reports whose modules are on; empty groups dropped."""
    out = []
    for key, label, icon, reports in GROUPS:
        visible = [{"title": r.title, "description": r.description, "url": url_for(r.endpoint, **r.kwargs),
                    "icon": r.icon} for r in reports if r.visible()]
        # GST also holds the GSTR-1 / 3B working papers, and Accounts the
        # monthly purchase / expense registers - so they can show with no
        # report tiles of their own.
        keep = key == "gst" or (key == "accounts" and (current_tenant_has("purchases") or current_tenant_has("expenses")))
        if visible or keep:
            out.append({"key": key, "label": label, "icon": icon, "reports": visible})
    return out
