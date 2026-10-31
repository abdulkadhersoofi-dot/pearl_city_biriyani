"""The one catalogue (console cleanup, stage 1): every switch in the app,
sorted into the four kinds of thing it is, so the package editor, a
client's page in the console, onboarding and the client's own Settings >
Modules all show the same list in the same order.

  1. Business type  - what the client is (retail, pharmacy, ...). Picked
                      once; not a module. It brings its own trade pack(s)
                      and suggests a starting package.
  2. Trade packs    - tools for one trade (restaurant tables, the drug
                      register, jewellery rates, ...). A client can have any
                      number, whatever their business type.
  3. Modules        - tools any business can use, in six areas: Selling,
                      Buying & suppliers, Stock, Accounts, Reports, GST
                      compliance. An add-on sits under the module it needs.
  4. Counter options - how the counter behaves (favourites row, day open /
                      close, cashier PINs, offline billing).

Only the arrangement lives here. What each switch is called and does stays
in app/utils/features.py; whether a client has it stays in
app/utils/entitlements.py. Nothing here changes what any client has.
"""

from dataclasses import dataclass

from app.models.tenant import BusinessType
from app.utils.features import AVAILABLE_FEATURES, BUSINESS_TYPE_LABELS, BUSINESS_TYPE_PRESETS, FEATURES

TRADE = "trade"
MODULE = "module"
COUNTER = "counter"

# The kinds a switch can be, in the order every screen shows them. The
# business type is the first kind, but it's a setting, not a switch.
KINDS = {
    TRADE: ("Trade packs", "Tools for one trade. The business type brings its own; any other can be added."),
    MODULE: ("Modules", "Tools any business can use, in six areas."),
    COUNTER: ("Counter options", "How the counter behaves."),
}

AREAS = {
    "selling": "Selling",
    "buying": "Buying & suppliers",
    "stock": "Stock",
    "accounts": "Accounts",
    "reports": "Reports",
    "gst": "GST compliance",
}


@dataclass(frozen=True)
class Entry:
    key: str                  # the feature flag (app/utils/features.py)
    kind: str                 # TRADE / MODULE / COUNTER
    icon: str
    area: str | None = None   # modules only
    # Switches it only does its job with: it sits under the first one, and
    # screens warn when one of them is off. Never enforced - a client's
    # switches stay exactly as they are.
    needs: tuple = ()
    setup: str | None = None  # endpoint of its set-up screen
    setup_kwargs: tuple = ()  # ((name, value), ...) for url_for
    setup_label: str | None = None
    short: str | None = None  # a trade pack's own short name


ENTRIES = [
    # --- trade packs ---
    Entry("restaurant_mode", TRADE, "utensils-crossed", needs=("pos",), setup="restaurant.setup",
          setup_label="Tables, order types & charges", short="Restaurant"),
    Entry("pharmacy", TRADE, "pill", needs=("batch_expiry",), setup="pharmacy.register", setup_label="Drug register",
          short="Pharmacy"),
    Entry("electronics", TRADE, "monitor", needs=("serial_imei",), setup="jobs.warranty", setup_label="Warranty check",
          short="Mobiles & electronics"),
    Entry("job_cards", TRADE, "wrench", setup="jobs.index", setup_label="Job cards", short="Repairs"),
    Entry("jewellery", TRADE, "gem", setup="jewellery.rates", setup_label="Today's rates", short="Jewellery"),
    Entry("wholesale", TRADE, "boxes", setup="wholesale.index", setup_label="Customer groups & salesmen",
          short="Wholesale"),
    Entry("weighing", TRADE, "scale", needs=("pos",), setup="settings.modules", setup_label="Scale label format",
          short="Weighing scale"),
    Entry("margin_scheme", TRADE, "coins", setup="margin.index", setup_label="Second-hand goods",
          short="Second-hand goods"),
    # --- modules: selling ---
    Entry("pos", MODULE, "monitor", "selling", setup="pos.terminal", setup_label="Open the counter"),
    Entry("credit_sales", MODULE, "users", "selling"),
    Entry("sales_documents", MODULE, "file-text", "selling", setup="sales.documents", setup_label="Quotes & orders"),
    Entry("recurring_invoices", MODULE, "repeat", "selling", setup="sales.recurring", setup_label="Recurring invoices"),
    Entry("multi_price_list", MODULE, "list", "selling", setup="products.price_lists", setup_label="Price lists"),
    Entry("loyalty", MODULE, "gift", "selling", setup="settings.modules", setup_label="Points rules"),
    # --- buying & suppliers ---
    Entry("purchases", MODULE, "truck", "buying", setup="purchases.suppliers", setup_label="Suppliers"),
    Entry("payables", MODULE, "landmark", "buying", setup="accounts.payables_index", setup_label="Supplier accounts"),
    Entry("expenses", MODULE, "wallet", "buying", setup="expenses.categories", setup_label="Expense categories"),
    Entry("doc_scan", MODULE, "scan-line", "buying", setup="scan.index", setup_label="Scan bills"),
    # --- stock ---
    Entry("inventory", MODULE, "warehouse", "stock", setup="inventory.locations", setup_label="Stock locations"),
    Entry("batch_expiry", MODULE, "calendar-clock", "stock", needs=("inventory",), setup="inventory.expiry",
          setup_label="Expiry"),
    Entry("serial_imei", MODULE, "scan-line", "stock", needs=("inventory",), setup="inventory.serials",
          setup_label="Serial numbers"),
    Entry("variants", MODULE, "package", "stock"),
    Entry("categories", MODULE, "list", "stock", setup="products.categories", setup_label="Categories"),
    # --- accounts ---
    Entry("receivables", MODULE, "landmark", "accounts", setup="accounts.receivables_index",
          setup_label="Customer accounts"),
    Entry("cash_bank_book", MODULE, "wallet", "accounts", setup="accounts.book", setup_kwargs=(("book", "cash"),),
          setup_label="Cash book"),
    # --- reports ---
    Entry("business_reports", MODULE, "chart-column", "reports", setup="reports.dashboard", setup_label="Dashboard"),
    Entry("daily_summary", MODULE, "file-text", "reports", setup="reports.owner_summary", setup_label="Today's summary"),
    # --- GST compliance ---
    Entry("einvoice", MODULE, "file-check", "gst", setup="einvoice.settings", setup_label="e-Invoice login"),
    Entry("eway_bill", MODULE, "truck", "gst", setup="einvoice.settings", setup_label="e-Way Bill login"),
    # --- counter options ---
    Entry("favourites", COUNTER, "sparkles", needs=("pos",)),
    Entry("day_close", COUNTER, "clock", needs=("pos",)),
    Entry("cashier_pin", COUNTER, "key-round", needs=("pos",), setup="branches.cashiers",
          setup_label="Cashiers & PINs"),
    Entry("offline_pos", COUNTER, "cloud-off", needs=("pos",), setup="pos.offline_bills",
          setup_label="Counter devices & offline bills"),
]
CATALOGUE = {e.key: e for e in ENTRIES}

# --- business types ------------------------------------------------------------------------------

# The trade packs each business type brings, and the package it's offered
# first. Picking a type pre-sets these (with the rest of its preset in
# app/utils/features.py) - every choice stays editable afterwards.
BUSINESS_TYPE_PACKS: dict[BusinessType, tuple] = {
    BusinessType.RETAIL: (),
    BusinessType.WHOLESALE: ("wholesale",),
    BusinessType.RESTAURANT: ("restaurant_mode",),
    BusinessType.MOBILE_ELECTRONICS: ("electronics", "job_cards", "margin_scheme"),
    BusinessType.PHARMACY: ("pharmacy",),
    BusinessType.APPAREL: (),
    BusinessType.HARDWARE: (),
    BusinessType.JEWELLERY: ("jewellery",),
    BusinessType.BAKERY: ("weighing",),
    BusinessType.SERVICES: ("job_cards",),
    BusinessType.OTHER: (),
}

# Which of ARFA's starting packages (by key) a new client of each type is
# offered first.
BUSINESS_TYPE_PACKAGE = {
    BusinessType.RETAIL: "retail", BusinessType.WHOLESALE: "wholesale", BusinessType.RESTAURANT: "restaurant",
    BusinessType.MOBILE_ELECTRONICS: "electronics", BusinessType.PHARMACY: "pharmacy",
    BusinessType.APPAREL: "retail", BusinessType.HARDWARE: "wholesale", BusinessType.JEWELLERY: "retail",
    BusinessType.BAKERY: "retail", BusinessType.SERVICES: "billing", BusinessType.OTHER: "current",
}


def _business_type(value) -> BusinessType | None:
    if isinstance(value, BusinessType):
        return value
    try:
        return BusinessType(value)
    except ValueError:
        return None


def trade_packs_for(business_type) -> tuple:
    bt = _business_type(business_type)
    return BUSINESS_TYPE_PACKS.get(bt, ()) if bt else ()


def business_types() -> list[dict]:
    """Each business type: its name, the trade packs it brings, the package
    it suggests and the modules it pre-sets - for onboarding and the
    package editor."""
    out = []
    for bt in BusinessType:
        packs = BUSINESS_TYPE_PACKS.get(bt, ())
        out.append({"value": bt.value, "label": BUSINESS_TYPE_LABELS[bt],
                    "packs": [CATALOGUE[k].short for k in packs],
                    "pack_keys": list(packs),
                    "package_key": BUSINESS_TYPE_PACKAGE.get(bt),
                    "presets": sorted(BUSINESS_TYPE_PRESETS.get(bt, set()))})
    return out


# --- reading ---------------------------------------------------------------------------------------

def entry(key: str) -> Entry | None:
    return CATALOGUE.get(key)


def kind_of(key: str) -> str:
    e = CATALOGUE.get(key)
    return e.kind if e else MODULE


def label_of(key: str) -> str:
    feature = FEATURES.get(key)
    return feature.label if feature else key


def missing_needs(key: str, is_on) -> list[str]:
    """The labels of what `key` needs that's off, by `is_on(key) -> bool`."""
    e = CATALOGUE.get(key)
    return [label_of(k) for k in (e.needs if e else ()) if not is_on(k)]


def _ordered(entries):
    """Each entry followed by the add-ons that sit under it (depth 1)."""
    keys = {e.key for e in entries}
    children = {}
    for e in entries:
        parent = e.needs[0] if e.needs and e.needs[0] in keys else None
        if parent:
            children.setdefault(parent, []).append(e)
    out = []
    for e in entries:
        parent = e.needs[0] if e.needs and e.needs[0] in keys else None
        if parent:
            continue
        out.append((e, 0))
        out.extend((child, 1) for child in children.get(e.key, []))
    return out


def groups() -> list[dict]:
    """Every built switch once, by kind then area:

      [{"kind", "kind_title", "kind_hint", "first" (first group of its
        kind), "key", "title" (area, or the kind's own for one-group
        kinds), "items": [{"feature", "entry", "depth", "needs"}]}]

    A switch added to app/utils/features.py but not placed here still
    shows, under Modules > Other, so one can never go missing."""
    available = {f.key: f for f in AVAILABLE_FEATURES}
    placed = [e for e in ENTRIES if e.key in available]
    sections = [(TRADE, None, [e for e in placed if e.kind == TRADE])]
    sections += [(MODULE, area, [e for e in placed if e.kind == MODULE and e.area == area]) for area in AREAS]
    rest = [f for f in AVAILABLE_FEATURES if f.key not in CATALOGUE]
    sections.append((COUNTER, None, [e for e in placed if e.kind == COUNTER]))
    out, seen_kinds = [], set()
    for kind, area, entries in sections:
        if not entries:
            continue
        out.append(_group(kind, area, AREAS[area] if area else KINDS[kind][0], _ordered(entries), seen_kinds))
    if rest:
        items = [(Entry(f.key, MODULE, "layout-grid", "other"), 0) for f in rest]
        group = _group(MODULE, "other", "Other", items, seen_kinds)
        # Keep it with the modules, before the counter options.
        index = next((i for i, g in enumerate(out) if g["kind"] == COUNTER), len(out))
        out.insert(index, group)
    return out


def _group(kind, area, title, ordered, seen_kinds) -> dict:
    first = kind not in seen_kinds
    seen_kinds.add(kind)
    return {"kind": kind, "kind_title": KINDS[kind][0], "kind_hint": KINDS[kind][1], "first": first,
            "key": area or kind, "title": title,
            "items": [{"feature": FEATURES[e.key], "entry": e, "depth": depth,
                       "needs": [label_of(k) for k in e.needs]} for e, depth in ordered]}


def ordered_keys() -> list[str]:
    return [item["feature"].key for g in groups() for item in g["items"]]
