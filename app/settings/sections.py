"""Settings in sections (redesign stage 2): the sections' menu, and the
module switches grouped by purpose with each module's "Set up" link.

A "Set up" link only shows once its module is on - its screen is a 404
until then, like every switched-off module."""

from flask import url_for

from app.utils.features import AVAILABLE_FEATURES

# key, label, icon, endpoint, one-line description
SECTIONS = [
    ("business", "Business", "store", "settings.index",
     "Your type of business, turnover and how new items are priced."),
    ("appearance", "Appearance", "sparkles", "settings.appearance",
     "Logo, colours and font - on your screens, sign-in page and prints."),
    ("modules", "Modules", "layout-grid", "settings.features",
     "Switch on only what your business uses - everything else stays out of your menus."),
    ("counter", "Counter", "monitor", "settings.counter",
     "How bills are paid at the counter, and the counter's own setup."),
    ("documents", "Documents & printing", "printer", "settings.documents",
     "Print sizes and the wording printed on your documents."),
    ("security", "Sign-in & security", "shield-check", "settings.security",
     "Two-step sign-in, idle sign-out and the counter lock."),
    ("subscription", "Subscription", "banknote", "subscriptions.mine",
     "Your invoices for this software."),
]

# (group label, [(feature key, icon, set-up endpoint or None, set-up kwargs, set-up label)])
MODULE_GROUPS = [
    ("Selling", [
        ("credit_sales", "users", None, {}, None),
        ("sales_documents", "file-text", "sales.documents", {}, "Quotes & orders"),
        ("recurring_invoices", "repeat", "sales.recurring", {}, "Recurring invoices"),
        ("multi_price_list", "list", "products.price_lists", {}, "Price lists"),
        ("loyalty", "gift", "settings.modules", {}, "Points rules"),
    ]),
    ("Stock & buying", [
        ("categories", "list", "products.categories", {}, "Categories"),
        ("inventory", "warehouse", "inventory.locations", {}, "Stock locations"),
        ("purchases", "truck", "purchases.suppliers", {}, "Suppliers"),
        ("expenses", "wallet", "expenses.categories", {}, "Expense categories"),
        ("variants", "package", None, {}, None),
        ("batch_expiry", "calendar-clock", "inventory.expiry", {}, "Expiry"),
        ("serial_imei", "scan-line", "inventory.serials", {}, "Serial numbers"),
    ]),
    ("Money & reports", [
        ("receivables", "landmark", "accounts.receivables_index", {}, "Customer accounts"),
        ("payables", "landmark", "accounts.payables_index", {}, "Supplier accounts"),
        ("cash_bank_book", "wallet", "accounts.book", {"book": "cash"}, "Cash book"),
        ("business_reports", "chart-column", "reports.dashboard", {}, "Dashboard"),
        ("daily_summary", "file-text", "reports.owner_summary", {}, "Today's summary"),
    ]),
    ("GST", [
        ("einvoice", "file-check", "einvoice.settings", {}, "e-Invoice login"),
        ("eway_bill", "truck", "einvoice.settings", {}, "e-Way Bill login"),
        ("margin_scheme", "coins", "settings.documents", {}, "Declaration wording"),
    ]),
    ("Your industry", [
        ("restaurant_mode", "utensils-crossed", "restaurant.setup", {}, "Tables, order types & charges"),
        ("weighing", "scale", "settings.modules", {}, "Scale label format"),
        ("pharmacy", "pill", "pharmacy.register", {}, "Drug register"),
        ("electronics", "monitor", "jobs.warranty", {}, "Warranty check"),
        ("jewellery", "gem", "jewellery.rates", {}, "Today's rates"),
        ("job_cards", "wrench", "jobs.index", {}, "Job cards"),
        ("wholesale", "boxes", "wholesale.index", {}, "Customer groups & salesmen"),
    ]),
    ("At the counter", [
        ("favourites", "sparkles", None, {}, None),
        ("day_close", "clock", None, {}, None),
        ("offline_pos", "cloud-off", "pos.offline_bills", {}, "Counter devices & offline bills"),
        ("cashier_pin", "key-round", "branches.cashiers", {}, "Cashiers & PINs"),
    ]),
]


# Set-up screens guarded by more than their own module's flag.
SETUP_ALSO_NEEDS = {"batch_expiry": ("inventory",), "serial_imei": ("inventory",)}


def section_menu(active: str) -> list[dict]:
    return [{"key": key, "label": label, "icon": icon, "url": url_for(endpoint), "active": key == active,
             "description": description}
            for key, label, icon, endpoint, description in SECTIONS]


def section_info(key: str) -> dict:
    for k, label, icon, _endpoint, description in SECTIONS:
        if k == key:
            return {"key": k, "label": label, "icon": icon, "description": description}
    raise KeyError(key)


def module_groups(tenant) -> list[dict]:
    """The Modules section: every built module once, in its group. A module
    added to app.utils.features without a group here still shows, under
    "Other", so a switch can never go missing."""
    features = {f.key: f for f in AVAILABLE_FEATURES}
    placed = set()
    groups = []
    for label, entries in MODULE_GROUPS:
        items = []
        for key, icon, endpoint, kwargs, setup_label in entries:
            feature = features.get(key)
            if feature is None:
                continue
            placed.add(key)
            on = tenant.has_feature(key)
            reachable = on and all(tenant.has_feature(k) for k in SETUP_ALSO_NEEDS.get(key, ()))
            items.append({"key": key, "label": feature.label, "description": feature.description, "icon": icon,
                          "on": on, "setup_url": url_for(endpoint, **kwargs) if (endpoint and reachable) else None,
                          "setup_label": setup_label})
        if items:
            groups.append({"label": label, "items": items})
    rest = [f for f in AVAILABLE_FEATURES if f.key not in placed]
    if rest:
        groups.append({"label": "Other", "items": [
            {"key": f.key, "label": f.label, "description": f.description, "icon": "layout-grid",
             "on": tenant.has_feature(f.key), "setup_url": None, "setup_label": None} for f in rest]})
    return groups
