"""The app's menus in one place: the grouped side menu, the "New" menu,
the search palette (Ctrl K) and the bell.

Each entry names the feature flag and/or permission that guards its
screen, so a switched-off module never shows up anywhere - the same rule
the routes enforce with feature_required / permission_required. A group
with nothing left in it is dropped, so a kirana shop sees a few short
groups and a wholesale client sees all six.

Client Admins and the firm's own logins (Ultra Admin, Auditors,
Sub-Auditors) get a menu. A branch (Staff) login gets a short one of its
own once its business has screens beyond the counter (a restaurant's
Tables and Kitchen, Job cards) - only what the Client Admin left on for
that login (its modules and permissions); a branch with nothing but the
counter keeps the simple layout with no menu at all."""

from dataclasses import dataclass, field

from flask import request, url_for
from markupsafe import Markup, escape

from app.utils.features import current_tenant_has


@dataclass(frozen=True)
class Link:
    label: str
    endpoint: str
    icon: str = "arrow-right"
    feature: str | None = None
    any_feature: tuple = ()
    # What lights the menu entry up: whole blueprints, and/or endpoints.
    blueprints: tuple = ()
    endpoints: tuple = ()
    kwargs: dict = field(default_factory=dict)
    keywords: str = ""
    # A console power the signed-in auditor must hold (packages and powers).
    power: str | None = None
    # A counter permission the signed-in login must hold (a branch's menu).
    permission: str | None = None
    # Other modules it also needs, all on (a restaurant's tables need the counter).
    also: tuple = ()
    # Anything else that decides it, asked last (a callable, no arguments).
    check: object = None

    def visible(self) -> bool:
        if self.feature and not current_tenant_has(self.feature):
            return False
        if self.also and not all(current_tenant_has(f) for f in self.also):
            return False
        if self.permission:
            from flask_login import current_user

            from app.utils.permissions import user_can

            if not user_can(current_user, self.permission):
                return False
        if self.power:
            from flask_login import current_user

            from app.utils.entitlements import auditor_can

            if not auditor_can(current_user, self.power):
                return False
        if self.any_feature and not any(current_tenant_has(f) for f in self.any_feature):
            return False
        if self.check is not None and not self.check():
            return False
        return True

    @property
    def url(self) -> str:
        return url_for(self.endpoint, **self.kwargs)

    def is_active(self) -> bool:
        endpoint = request.endpoint or ""
        if endpoint in self.endpoints:
            return True
        return bool(request.blueprint and request.blueprint in self.blueprints) and endpoint not in _CLAIMED

    def as_entry(self, group: str) -> dict:
        return {"label": self.label, "group": group, "url": self.url, "icon": self.icon,
                "keywords": self.keywords}


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    links: tuple


# Endpoints a specific menu entry claims, so the blueprint-wide match of
# a sibling (Reports owns the `reports` blueprint) doesn't also light up.
_CLAIMED = {
    "reports.dashboard", "reports.gstr1_view", "reports.gstr3b_view", "reports.filing_status",
    "jewellery.rates",
}


def _client_groups() -> list[Group]:
    return [
        Group("home", "Home", (
            Link("Dashboard", "reports.dashboard", "layout-dashboard", feature="business_reports",
                 endpoints=("reports.dashboard",), keywords="home today glance"),
            Link("Live", "live.dashboard", "activity", blueprints=("live",), keywords="branches leaderboard now"),
        )),
        Group("sell", "Sell", (
            Link("POS", "pos.terminal", "monitor", feature="pos", blueprints=("pos",),
                 keywords="counter billing bill till"),
            Link("Tables", "restaurant.tables", "utensils-crossed", feature="restaurant_mode", also=("pos",),
                 blueprints=("restaurant",), keywords="restaurant dine kot"),
            # Credit / debit notes are a tab inside Invoices (invoicing/_tabs.html).
            Link("Invoices", "invoicing.list_invoices", "receipt-text", blueprints=("invoicing", "notes"),
                 keywords="tax invoice b2b bill of supply credit note debit note"),
            Link("Quotes & orders", "sales.documents", "file-text", feature="sales_documents",
                 blueprints=("sales",), keywords="quotation proforma sales order challan recurring"),
            # Only when quotes are off - otherwise Recurring sits inside
            # Quotes & orders' own tabs (sales/_nav.html), as it did before.
            *((Link("Recurring invoices", "sales.recurring", "repeat", feature="recurring_invoices",
                    blueprints=("sales",), keywords="amc rent repeat"),)
              if not current_tenant_has("sales_documents") else ()),
            Link("Job cards", "jobs.index", "wrench", feature="job_cards", blueprints=("jobs",),
                 keywords="repair service warranty"),
            Link("Old gold", "jewellery.old_gold_list", "coins", feature="jewellery", blueprints=("jewellery",),
                 keywords="exchange old gold purchase"),
        )),
        Group("secondhand", "Second-hand goods", (
            Link("Overview", "margin.index", "coins", feature="margin_scheme", endpoints=("margin.index",),
                 keywords="margin scheme used second hand rule 32(5)"),
            Link("Buy used goods", "margin.buy_list", "truck", feature="margin_scheme",
                 endpoints=("margin.buy_list", "margin.buy_new", "margin.voucher"),
                 keywords="purchase voucher seller old phone exchange"),
            Link("Used stock", "margin.stock", "package", feature="margin_scheme", endpoints=("margin.stock",),
                 keywords="imei second hand stock"),
            Link("Sell", "margin.sell_list", "receipt-text", feature="margin_scheme",
                 endpoints=("margin.sell_list", "margin.sell_new"), keywords="margin scheme invoice msi"),
            Link("e-Way bills", "margin.eway", "scan-line", feature="margin_scheme", endpoints=("margin.eway",),
                 keywords="eway inward outward"),
        )),
        Group("stock", "Stock & buying", (
            Link("Items", "products.list_products", "package", blueprints=("products",),
                 keywords="products catalogue price list categories"),
            Link("Stock", "inventory.index", "warehouse", feature="inventory", blueprints=("inventory",),
                 keywords="inventory transfer adjustment godown location"),
            Link("Purchases", "purchases.bills", "truck", feature="purchases", blueprints=("purchases",),
                 keywords="supplier purchase bill order gstr-2b"),
            Link("Expenses", "expenses.index", "wallet", feature="expenses", blueprints=("expenses",),
                 keywords="rent salary electricity petty cash"),
            Link("Metal rates", "jewellery.rates", "trending-up", feature="jewellery",
                 endpoints=("jewellery.rates",), keywords="gold silver rate today"),
        )),
        Group("people", "People & money", (
            Link("Customers", "customers.list_customers", "users", blueprints=("customers",),
                 keywords="party client buyer"),
            *_accounts_link(),
            Link("Wholesale", "wholesale.index", "boxes", feature="wholesale", blueprints=("wholesale",),
                 keywords="salesmen customer groups slabs"),
            Link("Loyalty", "loyalty.index", "gift", feature="loyalty", blueprints=("loyalty",),
                 keywords="points rewards"),
        )),
        Group("reports", "Reports & GST", (
            Link("Reports", "reports.index", "chart-column", blueprints=("reports", "pharmacy"),
                 keywords="sales report stock report profit"),
            Link("GST returns", "reports.gstr1_view", "file-check",
                 endpoints=("reports.gstr1_view", "reports.gstr3b_view", "reports.filing_status"),
                 keywords="gstr-1 gstr-3b gstr1 gstr3b filing return"),
            Link("e-Invoice & e-Way bills", "einvoice.index", "scan-line", any_feature=("einvoice", "eway_bill"),
                 blueprints=("einvoice",), keywords="irn e-invoice eway e-way bill"),
        )),
        Group("business", "Business", (
            Link("Branches", "branches.list_branches", "store", blueprints=("branches",),
                 keywords="branch counter login staff cashiers"),
            Link("Settings", "settings.index", "settings", blueprints=("settings",), endpoints=("subscriptions.mine",),
                 keywords="business profile turnover preferences"),
        )),
    ]


def _offline_review() -> bool:
    """The Offline bills page is the owner's (a branch can't open it), with
    offline billing on or once a phone has billed offline."""
    from flask_login import current_user

    from app.pos.offline import offline_review_on

    return bool(current_user.is_client_admin and offline_review_on(current_user.tenant))


def _branch_groups() -> list[Group]:
    """A branch login's menu: its counter and the screens around it. Every
    entry follows the business's modules, what is hidden at this counter
    and the login's permissions - the same rules as the routes."""
    return [
        Group("counter", "Counter", (
            Link("Counter", "pos.terminal", "monitor", feature="pos", endpoints=("pos.terminal",),
                 keywords="pos billing bill"),
            Link("Tables", "restaurant.tables", "utensils-crossed", feature="restaurant_mode", also=("pos",),
                 endpoints=("restaurant.tables",), keywords="restaurant dine kot"),
            Link("Kitchen", "restaurant.kitchen", "clipboard-list", feature="restaurant_mode", also=("pos",),
                 endpoints=("restaurant.kitchen",), keywords="kot orders ready"),
            Link("Sold out today", "restaurant.menu", "package-minus", feature="restaurant_mode", also=("pos",),
                 endpoints=("restaurant.menu",), keywords="menu"),
            Link("Job cards", "jobs.index", "wrench", feature="job_cards",
                 endpoints=("jobs.index", "jobs.new", "jobs.view"), keywords="repair service"),
            Link("Warranty check", "jobs.warranty", "shield-check", feature="electronics",
                 endpoints=("jobs.warranty",), keywords="imei serial"),
        )),
        Group("today", "Today", (
            Link("Day-book", "pos.day_book", "list", feature="pos", permission="view_reports",
                 endpoints=("pos.day_book",), keywords="today bills reprint refund"),
            Link("Z-report", "pos.z_report_view", "file-text", feature="pos", permission="view_reports",
                 endpoints=("pos.z_report_view",), keywords="end of day"),
            Link("Close the day", "pos.day_close", "clock", feature="day_close", also=("pos",),
                 endpoints=("pos.day_close",), keywords="cash count drawer"),
            Link("Offline bills", "pos.offline_bills", "cloud-off", feature="pos", check=_offline_review,
                 endpoints=("pos.offline_bills",), keywords="sync queue"),
        )),
    ]


def _branch_has_screens() -> bool:
    """Has this branch login anything to open beside the counter itself?"""
    return any(link.visible() for link in _branch_groups()[0].links[1:])


def _groups_for(user) -> list[Group]:
    if user.is_client_admin:
        return _client_groups()
    if user.is_staff:
        return _branch_groups()
    return _admin_groups(user)


def _accounts_link() -> tuple:
    for feature, endpoint, kwargs in (("receivables", "accounts.receivables_index", {}),
                                      ("payables", "accounts.payables_index", {}),
                                      ("cash_bank_book", "accounts.book", {"book": "cash"})):
        if current_tenant_has(feature):
            return (Link("Accounts", endpoint, "landmark", blueprints=("accounts",), kwargs=kwargs,
                         keywords="receivables payables receipts payments cash book bank book ledger"),)
    return ()


def _admin_groups(user) -> list[Group]:
    """The console's side menu (console cleanup, stage 2): Today first,
    then the day's work, then the firm's own setup."""
    today = Link("Today", "tenants.today", "house", endpoints=("tenants.today",), keywords="home needs you")
    clients = Link("Clients", "tenants.directory", "building-2",
                   endpoints=("tenants.directory", "tenants.detail", "tenants.onboard", "tenants.client_changes_view"),
                   keywords="tenants businesses")
    filing = ("tenants.filing_tracker", "tenants.gst_exports")
    if user.is_super_admin:
        return [
            Group("work", "Work", (
                today, clients,
                Link("Auditors", "tenants.auditors_directory", "user-cog",
                     endpoints=("tenants.auditors_directory", "tenants.auditor_detail", "tenants.new_auditor")),
                Link("Filing & GST", "tenants.filing_tracker", "calendar-clock", endpoints=filing,
                     keywords="returns gstr exports monthly pack tally"),
            )),
            Group("platform", "Platform", (
                Link("Packages", "tenants.packages", "layout-grid",
                     endpoints=("tenants.packages", "tenants.package_new", "tenants.package_edit", "tenants.packages_compare"),
                     keywords="plans modules subscription tiers"),
                Link("Sign-in domains", "tenants.sign_in_domains", "key-round",
                     endpoints=("tenants.sign_in_domains", "tenants.sign_in_domain"),
                     keywords="vvip custom domain login page"),
                Link("Billing", "subscriptions.index", "banknote", blueprints=("subscriptions",),
                     keywords="subscriptions invoices fees"),
                Link("Audit log", "tenants.audit_log", "list", endpoints=("tenants.audit_log",)),
            )),
        ]
    if user.is_auditor:
        return [
            Group("work", "Work", (
                today, clients,
                Link("Sub-Auditors", "tenants.auditors_directory", "user-cog",
                     endpoints=("tenants.auditors_directory", "tenants.auditor_detail", "tenants.new_auditor"),
                     power="create_sub_auditors"),
                Link("Filing", "tenants.filing_tracker", "calendar-clock", endpoints=filing, power="month_end"),
            )),
            Group("platform", "Account", (
                Link("Subscription", "subscriptions.mine", "banknote", blueprints=("subscriptions",)),
            )),
        ]
    return [
        Group("work", "Work", (
            today, clients,
            Link("Filing", "tenants.filing_tracker", "calendar-clock", endpoints=filing, power="month_end"),
        )),
    ]


def has_menu(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_staff:
        return _branch_has_screens()
    return bool(user.is_admin_hierarchy or user.is_client_admin)


def menu_groups(user) -> list[dict]:
    """The side menu: [{key, label, links: [{label, url, icon, active}]}],
    empty groups dropped."""
    if not has_menu(user):
        return []
    groups = _groups_for(user)
    out = []
    for group in groups:
        links = [{"label": link.label, "url": link.url, "icon": link.icon, "active": link.is_active()}
                 for link in group.links if link.visible()]
        if links:
            out.append({"key": group.key, "label": group.label, "links": links})
    return out


def _new_actions() -> list[Link]:
    return [
        Link("Counter bill", "pos.terminal", "monitor", feature="pos", keywords="pos sale"),
        Link("Tax invoice", "invoicing.new_invoice", "receipt-text", keywords="invoice"),
        # (Credit / debit notes start from an issued invoice, not from here.)
        Link("Quotation or order", "sales.new_document", "file-text", feature="sales_documents",
             keywords="proforma sales order challan"),
        Link("Job card", "jobs.new", "wrench", feature="job_cards", keywords="repair"),
        Link("Item", "products.new_product", "package", keywords="product"),
        Link("Customer", "customers.new_customer", "users"),
        Link("Purchase bill", "purchases.new_bill", "truck", feature="purchases"),
        Link("Buy used goods", "margin.buy_new", "coins", feature="margin_scheme", keywords="second hand margin"),
        Link("Second-hand sale", "margin.sell_new", "receipt-text", feature="margin_scheme",
             keywords="margin scheme invoice"),
        Link("Expense", "expenses.new_expense", "wallet", feature="expenses"),
        Link("Receipt from a customer", "accounts.new_receipt", "banknote", feature="receivables"),
        Link("Payment to a supplier", "accounts.new_payment", "banknote", feature="payables"),
        Link("Stock adjustment", "inventory.new_adjustment", "package-minus", feature="inventory"),
        Link("Stock transfer", "inventory.new_transfer", "truck", feature="inventory"),
    ]


def new_menu(user) -> list[dict]:
    """The green "New" button's menu (Client Admins only)."""
    if not (getattr(user, "is_authenticated", False) and user.is_client_admin):
        return []
    return [{"label": a.label, "url": a.url, "icon": a.icon} for a in _new_actions() if a.visible()]


def _more_screens() -> list[tuple[str, Link]]:
    """Screens that live one level down (tabs, report cards) - reachable
    from search so nobody has to remember where a tab sits."""
    s, b, r, p = "Sell", "Stock & buying", "Reports & GST", "People & money"
    return [
        (s, Link("Credit & debit notes", "notes.list_notes", "file-minus", keywords="credit note debit note return")),
        (s, Link("Day-book", "pos.day_book", "list", feature="pos", keywords="today bills reprint refund")),
        (s, Link("Z-report", "pos.z_report_view", "file-text", feature="pos", keywords="end of day x report")),
        (s, Link("Close the day", "pos.day_close", "clock", feature="day_close", also=("pos",),
                 keywords="cash count drawer")),
        (s, Link("Offline bills", "pos.offline_bills", "cloud-off", feature="pos", check=_offline_review,
                 keywords="sync queue")),
        (s, Link("Restaurant menu", "restaurant.menu", "utensils-crossed", feature="restaurant_mode", also=("pos",),
                 keywords="sold out")),
        (s, Link("Restaurant setup", "restaurant.setup", "utensils-crossed", feature="restaurant_mode",
                 also=("pos",), keywords="tables sections charges")),
        (s, Link("Recurring invoices", "sales.recurring", "repeat", feature="recurring_invoices",
                 keywords="amc rent repeat")),
        (s, Link("Warranty check", "jobs.warranty", "shield-check", feature="electronics", keywords="imei serial")),
        (b, Link("Item categories", "products.categories", "list", feature="categories")),
        (b, Link("Price lists", "products.price_lists", "list", feature="multi_price_list", keywords="rates")),
        (b, Link("Stock by location", "inventory.by_location", "warehouse", feature="inventory")),
        (b, Link("Stock adjustments", "inventory.adjustments", "package-minus", feature="inventory",
                 keywords="damage wastage count")),
        (b, Link("Stock transfers", "inventory.transfers", "truck", feature="inventory", keywords="challan")),
        (b, Link("Reorder list", "inventory.reorder", "triangle-alert", feature="inventory", keywords="low stock")),
        (b, Link("Barcode labels", "inventory.labels", "printer", feature="inventory")),
        (b, Link("Stock locations", "inventory.locations", "store", feature="inventory", keywords="godown")),
        # These two screens need Inventory on as well as their own module.
        (b, Link("Expiry", "inventory.expiry", "calendar-clock", feature="inventory", any_feature=("batch_expiry",),
                 keywords="batch expired")),
        (b, Link("Serial numbers", "inventory.serials", "scan-line", feature="inventory", any_feature=("serial_imei",),
                 keywords="imei")),
        (b, Link("Suppliers", "purchases.suppliers", "truck", feature="purchases", keywords="vendor")),
        (b, Link("Purchase orders", "purchases.orders", "file-text", feature="purchases")),
        (b, Link("Purchase returns", "purchases.returns", "file-minus", feature="purchases", keywords="debit note")),
        (b, Link("GSTR-2B matching", "purchases.gstr2b_imports", "file-check", feature="purchases",
                 keywords="itc 2b")),
        (b, Link("Expense categories", "expenses.categories", "list", feature="expenses")),
        (b, Link("Recurring expenses", "expenses.recurring", "repeat", feature="expenses", keywords="rent salary")),
        (b, Link("Expense report", "expenses.report", "chart-column", feature="expenses")),
        (p, Link("Receipts", "accounts.receipts", "banknote", feature="receivables")),
        (p, Link("Receivables", "accounts.receivables_index", "landmark", feature="receivables",
                 keywords="outstanding ageing debtors")),
        (p, Link("Payables", "accounts.payables_index", "landmark", feature="payables",
                 keywords="creditors suppliers due")),
        (p, Link("Payments", "accounts.payments", "banknote", feature="payables")),
        (p, Link("Cash book", "accounts.book", "wallet", feature="cash_bank_book", kwargs={"book": "cash"})),
        (p, Link("Bank book", "accounts.book", "landmark", feature="cash_bank_book", kwargs={"book": "bank"})),
        (p, Link("Salesmen report", "wholesale.salesmen_report", "users", feature="wholesale")),
        (r, Link("Sales report", "reports.sales_view", "chart-column")),
        (r, Link("Items report", "reports.items_view", "chart-column")),
        (r, Link("Stock report", "reports.stock_view", "chart-column")),
        (r, Link("GSTR-1", "reports.gstr1_view", "file-check", keywords="outward b2b b2c")),
        (r, Link("GSTR-3B", "reports.gstr3b_view", "file-check", keywords="summary itc")),
        (r, Link("Filing status", "reports.filing_status", "calendar-clock", feature="business_reports")),
        (r, Link("Profit & loss", "reports.profit_loss", "trending-up", feature="business_reports",
                 keywords="p&l pnl profit")),
        (r, Link("Item profit", "reports.item_profit", "trending-up", feature="business_reports",
                 keywords="margin gross profit")),
        (r, Link("Daily summary", "reports.daily_summary_view", "file-text", feature="business_reports")),
        (r, Link("Owner's day summary", "reports.owner_summary", "file-text", feature="daily_summary",
                 keywords="whatsapp email")),
        (r, Link("Margin scheme register", "reports.margin_register", "file-spreadsheet", feature="margin_scheme")),
        ("Second-hand goods", Link("Second-hand settings", "margin.settings", "settings", feature="margin_scheme",
                                   keywords="margin declaration exempt table 8")),
        (r, Link("Drug register", "pharmacy.register", "pill", feature="pharmacy", keywords="schedule h h1")),
        (r, Link("e-Way bills", "einvoice.eway_bills", "truck", feature="eway_bill")),
        ("Settings", Link("Appearance", "settings.appearance", "sparkles", keywords="logo theme colour color font brand")),
        ("Settings", Link("Modules", "settings.features", "layout-grid",
                          keywords="features switch on off enable disable turn")),
        ("Settings", Link("Counter settings", "settings.counter", "monitor", feature="pos", keywords="payment modes upi qr cash card")),
        ("Settings", Link("Documents & printing", "settings.documents", "printer",
                          keywords="print size receipt paper roll margin declaration")),
        ("Settings", Link("Subscription", "subscriptions.mine", "banknote", keywords="software fee plan invoices")),
        ("Business", Link("Cashiers", "branches.cashiers", "key-round", feature="cashier_pin", keywords="pin")),
        ("Settings", Link("Scale labels & loyalty points", "settings.modules", "settings", any_feature=("weighing", "loyalty"),
                          keywords="weighing scale loyalty points")),
        ("Settings", Link("Sign-in & security", "settings.security", "shield-check",
                          keywords="two-step lockout timeout")),
        ("Settings", Link("Printer test", "settings.printer_test", "printer", keywords="thermal receipt")),
    ]


def palette_entries(user) -> list[dict]:
    """Everything the search box (Ctrl K) can jump to: every menu screen,
    every "New" action, the screens one level down, and the account's own
    pages. Built per request, so it only ever lists what this login may
    open."""
    if not has_menu(user):
        return []
    entries = []
    seen = set()

    def add(entry):
        key = (entry["url"], entry["label"])
        if key not in seen:
            seen.add(key)
            entries.append(entry)

    for group in _groups_for(user):
        for link in group.links:
            if link.visible():
                add(link.as_entry(group.label))
    if user.is_client_admin:
        for action in _new_actions():
            if action.visible():
                entry = action.as_entry("New")
                entry["label"] = "New " + action.label[0].lower() + action.label[1:]
                entries.append(entry)
        for group_label, link in _more_screens():
            if link.visible():
                add(link.as_entry(group_label))
    if user.is_client_admin or user.is_admin_hierarchy:
        add({"label": "Account security (two-step sign-in)", "group": "Account",
             "url": url_for("auth.account_security"), "icon": "shield-check", "keywords": "2fa totp password"})
    add({"label": "Change password", "group": "Account", "url": url_for("auth.change_password"),
         "icon": "key-round", "keywords": ""})
    add({"label": "Sign out", "group": "Account", "url": url_for("auth.logout"), "icon": "log-out",
         "keywords": "logout"})
    return entries


def bottom_tabs(user) -> list[dict]:
    """The phone's bottom bar: Home, Sell, (New), Reports, More."""
    if not (getattr(user, "is_authenticated", False) and user.is_client_admin):
        return []
    home = (Link("Home", "reports.dashboard", "house", endpoints=("reports.dashboard",))
            if current_tenant_has("business_reports")
            else Link("Home", "invoicing.list_invoices", "house", blueprints=("invoicing",)))
    sell = (Link("Sell", "pos.terminal", "monitor", blueprints=("pos",)) if current_tenant_has("pos")
            else Link("Sell", "invoicing.list_invoices", "receipt-text", blueprints=("invoicing",)))
    tabs = [home, sell,
            Link("Reports", "reports.index", "chart-column", blueprints=("reports",))]
    return [{"label": t.label, "url": t.url, "icon": t.icon, "active": t.is_active()} for t in tabs]


def icon(name: str, cls: str = "", label: str | None = None) -> Markup:
    """An icon from the vendored sprite (app/static/img/icons.svg). Purely
    decorative unless a label is given."""
    href = url_for("static", filename="img/icons.svg") + "#i-" + name
    aria = f'role="img" aria-label="{escape(label)}"' if label else 'aria-hidden="true"'
    return Markup(f'<svg class="icon {escape(cls)}" {aria} focusable="false"><use href="{escape(href)}"></use></svg>')


def needs_you(tenant) -> list[dict]:
    """The bell: things waiting on the Client Admin, each linking to where
    it is dealt with. Every count comes from the module that owns it and
    only when that module is on."""
    from datetime import date

    items = []
    from app.pos.offline import offline_review_on

    if tenant.has_feature("pos") and offline_review_on(tenant):
        from app.models.platform import OfflineBill, OfflineBillStatus

        failed = OfflineBill.query.filter_by(tenant_id=tenant.id, status=OfflineBillStatus.FAILED).count()
        if failed:
            items.append({"label": f"{failed} offline bill{'s' if failed != 1 else ''} could not be saved",
                          "detail": "Check and save or cancel them", "url": url_for("pos.offline_bills"),
                          "icon": "cloud-off", "tone": "bad"})
    if tenant.has_feature("inventory"):
        from app.inventory.reports import low_stock

        low = len(low_stock(tenant.id))
        if low:
            items.append({"label": f"{low} item{'s' if low != 1 else ''} at or below minimum stock",
                          "detail": "See what to reorder", "url": url_for("inventory.reorder"),
                          "icon": "package-minus", "tone": "warn"})
    if tenant.has_feature("inventory") and tenant.has_feature("batch_expiry"):
        from app.inventory.reports import expiry_report

        expired, soon = expiry_report(tenant.id, 30)
        if expired or soon:
            items.append({"label": f"{len(expired)} batch{'es' if len(expired) != 1 else ''} expired, "
                                   f"{len(soon)} expiring within 30 days",
                          "detail": "Expiry report", "url": url_for("inventory.expiry"),
                          "icon": "calendar-clock", "tone": "bad" if expired else "warn"})
    if tenant.has_feature("recurring_invoices"):
        from app.models.sales import RecurringInvoiceRun, RecurringRunStatus

        due = RecurringInvoiceRun.query.filter(RecurringInvoiceRun.tenant_id == tenant.id,
                                               RecurringInvoiceRun.status == RecurringRunStatus.PENDING,
                                               RecurringInvoiceRun.due_date <= date.today()).count()
        if due:
            items.append({"label": f"{due} recurring invoice{'s' if due != 1 else ''} ready to issue",
                          "detail": "Review the drafts", "url": url_for("sales.recurring"),
                          "icon": "repeat", "tone": "warn"})
    return items


def admin_needs_you() -> list[dict]:
    """The Ultra Admin's bell: clients asking ARFA to add a module to their
    package (Settings > Modules > "Ask ARFA to add"), newest first."""
    from app.models.package import ModuleRequest
    from app.utils.features import FEATURES

    items = []
    for req in (ModuleRequest.query.filter_by(status=ModuleRequest.PENDING)
                .order_by(ModuleRequest.created_at.desc()).limit(20).all()):
        feature = FEATURES.get(req.feature_key)
        items.append({"label": f"{req.tenant.display_name} asks for {feature.label if feature else req.feature_key}",
                      "detail": f"Asked {req.created_at.strftime('%d %b')} - add it or decline",
                      "url": url_for("tenants.detail", tenant_id=req.tenant_id, tab="package", _anchor="sec-package"),
                      "icon": "layout-grid", "tone": "warn"})
    return items
