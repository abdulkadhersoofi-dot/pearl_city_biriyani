"""The seven doors: the simple menu and the hub page behind each door.

    Home   Sell   Buy   Stock   Money   Reports   Setup

A door gathers screens that used to be separate menu entries and tabs. Its
hub page has the same three parts every time:

    actions   the one thing done most (a big button), and a few beside it
    waiting   what is waiting on the owner in this area, with counts
    tiles     every screen behind the door, in short named sections

Nothing here adds or removes a screen, and no web address changes: a tile is
a link to a screen that already exists, shown only when its module is on -
the same rules as the classic menu (app/navigation.py), whose Link this
reuses. The classic menu stays available (navigation.menu_style).

Which door a page belongs to (so the right one lights up) is decided here
too: by the page's endpoint first, then its blueprint.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import request, url_for

from app.navigation import Link, _offline_review
from app.utils.features import current_tenant_has


@dataclass(frozen=True)
class Door:
    key: str
    label: str
    icon: str
    endpoint: str
    intro: str


DOORS = (
    Door("home", "Home", "house", "hubs.home", "Today at a glance, and what needs you."),
    Door("sell", "Sell", "receipt-text", "hubs.sell", "Bills, orders and quotes - everything you sell."),
    Door("buy", "Buy", "truck", "hubs.buy", "Supplier bills, orders and expenses - everything you buy."),
    Door("stock", "Stock", "package", "hubs.stock", "Your items and what is where."),
    Door("money", "Money", "landmark", "hubs.money", "Who owes you, whom you owe, cash and bank."),
    Door("reports", "Reports", "chart-column", "reports.index", "The answers, and GST."),
    Door("setup", "Setup", "settings", "hubs.setup", "Branches, phones, modules and settings."),
)
BY_KEY = {d.key: d for d in DOORS}

# --- which door is this page behind? ----------------------------------------------------------------

_BY_ENDPOINT = {
    "reports.dashboard": "home", "hubs.home": "home",
    "hubs.sell": "sell", "hubs.buy": "buy", "hubs.stock": "stock", "hubs.money": "money", "hubs.setup": "setup",
    "jewellery.rates": "stock",
    "inventory.locations": "setup", "inventory.deactivate_location": "setup",
    "warehouse.logins": "setup", "scan.teach": "setup", "scan.teach_bill": "setup",
    "scan.teach_read_again": "setup", "subscriptions.mine": "setup",
    "warehouse.financiers": "money", "warehouse.settlement": "money",
}
_BY_BLUEPRINT = {
    "live": "home", "sap": "setup",
    "sell": "sell",
    "pos": "sell", "restaurant": "sell", "invoicing": "sell", "notes": "sell", "sales": "sell", "jobs": "sell",
    "jewellery": "sell", "margin": "sell",
    "purchases": "buy", "scan": "buy", "expenses": "buy",
    "products": "stock", "inventory": "stock", "warehouse": "stock",
    "customers": "money", "accounts": "money", "wholesale": "money", "loyalty": "money",
    "reports": "reports", "pharmacy": "reports", "einvoice": "reports",
    "branches": "setup", "settings": "setup", "apps": "setup", "subscriptions": "setup",
}
_BULK_KIND = {"products": "stock", "customers": "money", "suppliers": "buy"}


def current_door() -> str | None:
    endpoint = request.endpoint or ""
    if endpoint in _BY_ENDPOINT:
        return _BY_ENDPOINT[endpoint]
    if request.blueprint == "bulk":
        return _BULK_KIND.get((request.view_args or {}).get("kind"))
    return _BY_BLUEPRINT.get(request.blueprint or "")


def _buy_is_on() -> bool:
    return any(current_tenant_has(f) for f in ("purchases", "doc_scan", "expenses"))


def home_url() -> str:
    return url_for("reports.dashboard") if current_tenant_has("business_reports") else url_for("hubs.home")


def visible_doors() -> list[Door]:
    return [d for d in DOORS if d.key != "buy" or _buy_is_on()]


def menu_links() -> list[dict]:
    """The simple side menu: one entry a door."""
    here = current_door()
    out = []
    for d in visible_doors():
        url = home_url() if d.key == "home" else url_for(d.endpoint)
        out.append({"label": d.label, "url": url, "icon": d.icon, "active": d.key == here, "key": d.key})
    return out


# --- what waits behind each door ----------------------------------------------------------------------

def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _from_bell(tenant, prefixes) -> list[dict]:
    """The bell's items (navigation.needs_you) that belong to this door."""
    from app.navigation import needs_you

    return [i for i in needs_you(tenant) if i["url"].startswith(prefixes)]


def _sell_waiting(tenant) -> list[dict]:
    items = []
    if current_tenant_has("warehouse_dispatch") and current_tenant_has("sales_documents"):
        from app.models.sales import SalesDocument, SalesDocumentKind, SalesDocumentStatus
        from app.models.warehouse import DispatchStatus

        base = SalesDocument.query.filter(SalesDocument.tenant_id == tenant.id,
                                          SalesDocument.kind == SalesDocumentKind.SALES_ORDER,
                                          SalesDocument.status == SalesDocumentStatus.OPEN)
        ready = base.filter(SalesDocument.dispatch_status == DispatchStatus.PICKED).count()
        at_warehouse = base.filter(SalesDocument.dispatch_status.in_(
            [DispatchStatus.TO_PICK, DispatchStatus.PICKING])).count()
        if ready:
            items.append({"label": f"{_plural(ready, 'order')} picked and ready to bill",
                          "detail": "Make the invoice", "url": url_for("warehouse.queue"), "icon": "circle-check",
                          "tone": "warn"})
        if at_warehouse:
            items.append({"label": f"{_plural(at_warehouse, 'order')} at the warehouse",
                          "detail": "Waiting to be picked", "icon": "truck", "tone": "info",
                          "url": url_for("sales.documents", kind="sales_order")})
    return items + _from_bell(tenant, ("/pos/", "/sales/"))


def _buy_waiting(tenant) -> list[dict]:
    items = []
    if current_tenant_has("doc_scan"):
        from app.models.scan import TO_CHECK, ScanDocument

        to_check = ScanDocument.query.filter_by(tenant_id=tenant.id, status=TO_CHECK).count()
        if to_check:
            items.append({"label": f"{_plural(to_check, 'scanned bill')} to check and post",
                          "detail": "Open Scan bills", "url": url_for("scan.index"), "icon": "scan-line",
                          "tone": "warn"})
    if current_tenant_has("purchases"):
        from app.models.purchase import PurchaseOrder, PurchaseOrderStatus

        open_orders = PurchaseOrder.query.filter(
            PurchaseOrder.tenant_id == tenant.id,
            PurchaseOrder.status.in_((PurchaseOrderStatus.OPEN, PurchaseOrderStatus.PARTLY))).count()
        if open_orders:
            items.append({"label": f"{_plural(open_orders, 'purchase order')} waiting for goods or a bill",
                          "detail": "See the orders", "url": url_for("purchases.orders"), "icon": "file-text",
                          "tone": "info"})
    return items


def _stock_waiting(tenant) -> list[dict]:
    return _from_bell(tenant, ("/inventory/",))


def _money_waiting(tenant) -> list[dict]:
    items = []
    if current_tenant_has("receivables"):
        from datetime import date

        from app.accounts.receivables import customer_balances
        from app.utils.money import inr

        rows = customer_balances(tenant, date.today())
        overdue = sum((r["overdue"] for r in rows), 0)
        late = sum(1 for r in rows if r["overdue"] > 0)
        if overdue:
            items.append({"label": f"Rs. {inr(overdue)} overdue from {_plural(late, 'customer')}",
                          "detail": "See who owes you", "url": url_for("accounts.receivables_index"),
                          "icon": "users", "tone": "warn"})
    return items


def _setup_waiting(tenant) -> list[dict]:
    return []


def _home_waiting(tenant) -> list[dict]:
    from app.navigation import needs_you

    return needs_you(tenant)


# --- the hubs -----------------------------------------------------------------------------------------

def _L(label, endpoint, icon, **kw) -> Link:
    return Link(label, endpoint, icon, **kw)


def _sell() -> dict:
    return {
        "actions": [
            _L("New sale", "sell.new_sale", "receipt-text"),
            _L("Counter bill", "pos.terminal", "monitor", feature="pos"),
        ],
        "waiting": _sell_waiting,
        "sections": [
            ("Bills", [
                _L("Invoices", "invoicing.list_invoices", "receipt-text"),
                _L("Invoice form (classic)", "invoicing.new_invoice", "receipt-text"),
                _L("Credit & debit notes", "notes.list_notes", "file-minus"),
                _L("Counter (POS)", "pos.terminal", "monitor", feature="pos"),
                _L("Day-book", "pos.day_book", "list", feature="pos"),
                _L("Z-report", "pos.z_report_view", "file-text", feature="pos"),
                _L("Close the day", "pos.day_close", "clock", feature="day_close", also=("pos",)),
                _L("Offline bills", "pos.offline_bills", "cloud-off", feature="pos", check=_offline_review),
            ]),
            ("Orders and quotes", [
                _L("Sales orders", "sales.documents", "file-text", feature="sales_documents",
                   kwargs={"kind": "sales_order"}),
                _L("Ready to bill", "warehouse.queue", "circle-check", feature="warehouse_dispatch"),
                _L("Quotations", "sales.documents", "file-text", feature="sales_documents",
                   kwargs={"kind": "quotation"}),
                _L("Proforma invoices", "sales.documents", "file-text", feature="sales_documents",
                   kwargs={"kind": "proforma"}),
                _L("Delivery challans", "sales.documents", "truck", feature="sales_documents",
                   kwargs={"kind": "delivery_challan"}),
                _L("Recurring invoices", "sales.recurring", "repeat", feature="recurring_invoices"),
                _L("Quote or order form (classic)", "sales.new_document", "file-text", feature="sales_documents"),
            ]),
            ("Your trade", [
                _L("Tables", "restaurant.tables", "utensils-crossed", feature="restaurant_mode", also=("pos",)),
                _L("Restaurant menu", "restaurant.menu", "utensils-crossed", feature="restaurant_mode", also=("pos",)),
                _L("Restaurant setup", "restaurant.setup", "settings", feature="restaurant_mode", also=("pos",)),
                _L("Job cards", "jobs.index", "wrench", feature="job_cards"),
                _L("Warranty check", "jobs.warranty", "shield-check", feature="electronics"),
                _L("Old gold", "jewellery.old_gold_list", "coins", feature="jewellery"),
                _L("Second-hand goods", "margin.index", "coins", feature="margin_scheme"),
                _L("Buy used goods", "margin.buy_list", "truck", feature="margin_scheme"),
                _L("Used stock", "margin.stock", "package", feature="margin_scheme"),
                _L("Second-hand sales", "margin.sell_list", "receipt-text", feature="margin_scheme"),
                _L("Second-hand e-Way bills", "margin.eway", "scan-line", feature="margin_scheme"),
                _L("Second-hand settings", "margin.settings", "settings", feature="margin_scheme"),
            ]),
        ],
    }


def _buy() -> dict:
    scan = current_tenant_has("doc_scan")
    return {
        "actions": [
            *([_L("Add a bill", "scan.index", "scan-line", feature="doc_scan")] if scan else []),
            _L("Type a bill by hand" if scan else "Add a bill", "purchases.new_bill", "truck", feature="purchases"),
            _L("Add an expense", "expenses.new_expense", "wallet", feature="expenses"),
        ],
        "waiting": _buy_waiting,
        "sections": [
            ("Bills", [
                _L("Scan bills", "scan.index", "scan-line", feature="doc_scan"),
                _L("Purchase bills", "purchases.bills", "truck", feature="purchases"),
                _L("Returns / debit notes", "purchases.returns", "file-minus", feature="purchases"),
                _L("Supplier credit / debit notes", "purchases.supplier_notes", "file-minus", feature="purchases"),
                _L("GSTR-2B match", "purchases.gstr2b_imports", "file-check", feature="purchases"),
            ]),
            ("Orders and suppliers", [
                _L("Purchase orders", "purchases.orders", "file-text", feature="purchases"),
                _L("Goods received", "warehouse.receipts", "package", feature="warehouse_dispatch",
                   also=("purchases",)),
                _L("Suppliers", "purchases.suppliers", "users", feature="purchases"),
                _L("Add suppliers in bulk", "bulk.index", "file-spreadsheet", feature="purchases",
                   kwargs={"kind": "suppliers"}),
            ]),
            ("Expenses", [
                _L("Expenses", "expenses.index", "wallet", feature="expenses"),
                _L("Recurring expenses", "expenses.recurring", "repeat", feature="expenses"),
                _L("Expense categories", "expenses.categories", "list", feature="expenses"),
                _L("Expense report", "expenses.report", "chart-column", feature="expenses"),
            ]),
        ],
    }


def _stock() -> dict:
    return {
        "actions": [
            _L("Add an item", "products.new_product", "package"),
            _L("Add items in bulk", "bulk.index", "file-spreadsheet", kwargs={"kind": "products"}),
            _L("Move stock", "inventory.new_transfer", "truck", feature="inventory"),
            _L("Adjust stock", "inventory.new_adjustment", "package-minus", feature="inventory"),
        ],
        "waiting": _stock_waiting,
        "sections": [
            ("Items", [
                _L("Items", "products.list_products", "package"),
                _L("Item categories", "products.categories", "list", feature="categories"),
                _L("Price lists", "products.price_lists", "list", feature="multi_price_list"),
                _L("Metal rates", "jewellery.rates", "trending-up", feature="jewellery"),
            ]),
            ("What is where", [
                _L("Stock on hand", "inventory.index", "warehouse", feature="inventory"),
                _L("Stock by place", "inventory.by_location", "store", feature="inventory"),
                _L("Serial numbers", "inventory.serials", "scan-line", feature="inventory",
                   any_feature=("serial_imei",)),
                _L("Transfers", "inventory.transfers", "truck", feature="inventory"),
                _L("Adjustments", "inventory.adjustments", "package-minus", feature="inventory"),
                _L("Barcode labels", "inventory.labels", "printer", feature="inventory"),
            ]),
            ("Alerts", [
                _L("Reorder list", "inventory.reorder", "triangle-alert", feature="inventory"),
                _L("Fast, slow and dead stock", "inventory.movement", "activity", feature="inventory"),
                _L("Expiry", "inventory.expiry", "calendar-clock", feature="inventory",
                   any_feature=("batch_expiry",)),
            ]),
            ("Warehouse", [
                _L("Ready to invoice", "warehouse.queue", "circle-check", feature="warehouse_dispatch"),
                _L("Receiving", "warehouse.receipts", "package", feature="warehouse_dispatch"),
                _L("Racks", "warehouse.racks", "boxes", feature="warehouse_dispatch"),
                _L("Warehouse reports", "warehouse.reports", "chart-column", feature="warehouse_dispatch"),
            ]),
        ],
    }


def _money() -> dict:
    return {
        "actions": [
            _L("Receive money", "accounts.new_receipt", "banknote", feature="receivables"),
            _L("Pay a supplier", "accounts.new_payment", "banknote", feature="payables"),
            _L("Add a customer", "customers.new_customer", "users"),
        ],
        "waiting": _money_waiting,
        "sections": [
            ("Money in", [
                _L("Customers", "customers.list_customers", "users"),
                _L("Customers owe you", "accounts.receivables_index", "landmark", feature="receivables"),
                _L("Receipts", "accounts.receipts", "banknote", feature="receivables"),
                _L("Add customers in bulk", "bulk.index", "file-spreadsheet", kwargs={"kind": "customers"}),
            ]),
            ("Money out", [
                _L("You owe suppliers", "accounts.payables_index", "landmark", feature="payables"),
                _L("Payments", "accounts.payments", "banknote", feature="payables"),
            ]),
            ("Cash and bank", [
                _L("Cash book", "accounts.book", "wallet", feature="cash_bank_book", kwargs={"book": "cash"}),
                _L("Bank book", "accounts.book", "landmark", feature="cash_bank_book", kwargs={"book": "bank"}),
            ]),
            ("More", [
                _L("Finance companies (EMI)", "warehouse.financiers", "landmark", feature="warehouse_dispatch"),
                _L("Wholesale", "wholesale.index", "boxes", feature="wholesale"),
                _L("Salesmen report", "wholesale.salesmen_report", "users", feature="wholesale"),
                _L("Loyalty", "loyalty.index", "gift", feature="loyalty"),
            ]),
        ],
    }


def _setup() -> dict:
    return {
        "actions": [
            _L("Add a branch", "branches.new_branch", "store"),
            _L("Switch modules on or off", "settings.features", "layout-grid"),
        ],
        "waiting": _setup_waiting,
        "sections": [
            ("People and phones", [
                _L("Branches", "branches.list_branches", "store"),
                _L("Cashiers & PINs", "branches.cashiers", "key-round", feature="cashier_pin"),
                _L("Phone apps", "apps.index", "smartphone"),
                _L("Warehouse app logins", "warehouse.logins", "smartphone", feature="warehouse_dispatch"),
            ]),
            ("Your business", [
                _L("Business profile", "settings.index", "settings"),
                _L("Appearance", "settings.appearance", "sparkles"),
                _L("Documents & printing", "settings.documents", "printer"),
                _L("Counter settings", "settings.counter", "monitor", feature="pos"),
                _L("Places (stores and godowns)", "inventory.locations", "store", feature="inventory"),
                _L("Printer test", "settings.printer_test", "printer"),
            ]),
            ("Modules and training", [
                _L("Modules", "settings.features", "layout-grid"),
                _L("Scale labels & loyalty points", "settings.modules", "settings",
                   any_feature=("weighing", "loyalty")),
                _L("Bill reader training", "scan.teach", "scan-line", feature="doc_scan"),
                _L("SAP link", "sap.index", "repeat", feature="sap_link"),
            ]),
            ("Account", [
                _L("Sign-in & security", "settings.security", "shield-check"),
                _L("Subscription", "subscriptions.mine", "banknote"),
            ]),
        ],
    }


def _home() -> dict:
    return {"actions": [], "waiting": _home_waiting, "sections": []}


_HUBS = {"home": _home, "sell": _sell, "buy": _buy, "stock": _stock, "money": _money, "setup": _setup}


def hub(key: str, tenant) -> dict:
    """What a hub page shows: {door, actions, waiting, sections}, with only
    what this business has switched on. Each waiting list is worked out on
    its own - one that fails is left out, it never takes the page down."""
    spec = _HUBS[key]()
    actions = [{"label": a.label, "url": a.url, "icon": a.icon} for a in spec["actions"] if a.visible()]
    sections = []
    for title, links in spec["sections"]:
        tiles = [{"label": l.label, "url": l.url, "icon": l.icon} for l in links if l.visible()]
        if tiles:
            sections.append({"title": title, "tiles": tiles})
    try:
        waiting = spec["waiting"](tenant)
    except Exception:  # noqa: BLE001
        import logging

        from app.extensions import db

        db.session.rollback()
        logging.getLogger(__name__).exception("hub %s: waiting list failed", key)
        waiting = []
    return {"door": BY_KEY[key], "actions": actions, "waiting": waiting, "sections": sections}


def all_tiles() -> list[tuple[str, Link]]:
    """Every tile of every hub, with its door's label - for the search box."""
    out = []
    for key, build in _HUBS.items():
        spec = build()
        for link in spec["actions"]:
            out.append((BY_KEY[key].label, link))
        for _title, links in spec["sections"]:
            for link in links:
                out.append((BY_KEY[key].label, link))
    return out
