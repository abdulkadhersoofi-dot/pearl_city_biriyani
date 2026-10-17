"""Per-client feature flags - the one switchboard deciding which modules a
client sees, so a kirana store, a pharmacy and a restaurant can share one
app without each wading through the others' screens.

A flag only does anything once its module is built (`available=True`).
Every module is built now; when the last ones shipped (Phase 9) a
migration switched them off for clients that already existed, so nobody
saw a module appear that they hadn't turned on.
"""

from dataclasses import dataclass
from functools import wraps

from flask import abort, g, request
from flask_login import current_user, login_required

from app.models.tenant import BusinessType


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    description: str
    available: bool = False


FEATURES = {
    f.key: f
    for f in [
        Feature(
            "categories",
            "Item categories",
            "Group items into categories and sub-categories (e.g. Biriyani > Chicken). The item form gets "
            "category pickers and the POS screen shows a collapsible category tree. A category is never "
            "required for an item, even with this on.",
            available=True,
        ),
        Feature(
            "inventory",
            "Inventory",
            "Stock levels at each store, godown and branch, adjustments (damage, wastage, opening stock, physical "
            "count), transfers with a delivery challan, low-stock alerts, stock reports and barcode labels. Items "
            "set to stop at zero can no longer be billed once their stock runs out.",
            available=True,
        ),
        Feature(
            "purchases",
            "Purchases",
            "Suppliers, purchase orders, purchase bills with input tax credit and landed cost, returns to "
            "suppliers (debit notes) and GSTR-2B matching.",
            available=True,
        ),
        Feature(
            "expenses",
            "Expenses",
            "Rent, salary, electricity and other expenses - GST bills give input tax credit, rent and salaries "
            "repeat every month, and petty cash from the counter drawer is booked automatically.",
            available=True,
        ),
        Feature(
            "variants",
            "Size / colour variants",
            "One item in several sizes or colours - each with its own code, barcode, price and stock.",
            available=True,
        ),
        Feature(
            "batch_expiry",
            "Batch & expiry",
            "Batch numbers and expiry dates captured on purchase; the counter sells the batch expiring first "
            "and refuses expired stock; near-expiry and expired reports.",
            available=True,
        ),
        Feature(
            "serial_imei",
            "Serial / IMEI",
            "Each unit's serial number or IMEI captured on purchase, picked at billing and printed on the bill, "
            "with warranty dates and a search to find any unit.",
            available=True,
        ),
        Feature(
            "margin_scheme",
            "Margin scheme (second-hand goods)",
            "Used phones, vehicles and other second-hand goods: GST on your margin only (Rule 32(5)), on a "
            "separate margin-scheme invoice, with a margin register. Needs Purchases on to record what each "
            "item cost.",
            available=True,
        ),
        Feature(
            "receivables",
            "Customer accounts",
            "What each customer owes: credit invoices and counter bills, receipts against them (full, part or "
            "on account), cheques, opening balances, ageing, a credit-limit warning at billing, statements and "
            "WhatsApp reminders. Includes advance receipt and refund vouchers with GST on advances for services.",
            available=True,
        ),
        Feature(
            "payables",
            "Supplier accounts",
            "What you owe each supplier: purchase bills and unpaid expenses, payment vouchers against them "
            "(cash, bank, cheque), opening balances and ageing.",
            available=True,
        ),
        Feature(
            "sales_documents",
            "Quotations, orders & challans",
            "Quotations / estimates, proforma invoices, sales orders and delivery challans (job work, sale on "
            "approval) - each turned into an invoice in one click.",
            available=True,
        ),
        Feature(
            "recurring_invoices",
            "Recurring invoices",
            "Monthly AMC, rent or catering contracts: the invoice is drafted on each due date for you to "
            "issue (or skip) in one click.",
            available=True,
        ),
        Feature(
            "cash_bank_book",
            "Cash & bank book",
            "Day-by-day cash in hand and bank balance, built from counter sales, receipts, payments and expenses.",
            available=True,
        ),
        Feature(
            "business_reports",
            "Profit & loss and business reports",
            "A home dashboard (today's and this month's sales, collections, dues, low stock), profit & loss with "
            "cost of goods sold, gross profit by item and category, and a daily sales summary by payment mode, "
            "hour and cashier.",
            available=True,
        ),
        Feature(
            "weighing",
            "Weighing scale items",
            "Price-per-kg items with quick weight buttons, and the barcode labels a weighing scale prints (the "
            "weight or price is inside the barcode) scanned straight into the bill.",
            available=True,
        ),
        Feature(
            "restaurant_mode",
            "Restaurant tables & KOT",
            "Dine-in tables (running orders, move, merge), takeaway / delivery / Swiggy / Zomato each with its own "
            "prices, kitchen order tickets, item notes (\"less spicy\"), packing and service charges and a "
            "\"sold out today\" switch.",
            available=True,
        ),
        Feature(
            "job_cards",
            "Job cards",
            "Repair and service jobs: item received, complaint, estimate, Received -> In progress -> Ready -> "
            "Delivered, parts from stock and labour, one-click invoice and a WhatsApp \"ready for pickup\" message.",
            available=True,
        ),
        Feature(
            "pharmacy",
            "Pharmacy",
            "Batch and expiry on every medicine, Schedule H / H1 / X drugs sold only with the doctor's and "
            "patient's details (with the H1 register), substitutes with the same composition, and selling by the "
            "strip or the tablet.",
            available=True,
        ),
        Feature(
            "electronics",
            "Mobiles & electronics",
            "IMEI / serial picked for every unit sold, customer warranty counted from the sale date, and a "
            "warranty check by IMEI that opens a repair job card.",
            available=True,
        ),
        Feature(
            "jewellery",
            "Jewellery",
            "Today's gold / silver rates, prices worked out from net weight, purity, making charges and stones "
            "(3% GST), making charges on the customer's own gold as job work (5%), and old gold taken in "
            "exchange on a purchase voucher, deducted from the bill after GST.",
            available=True,
        ),
        Feature(
            "wholesale",
            "Wholesale pricing & salesmen",
            "Customer groups with their own price list, quantity-slab prices, and sales and collections by "
            "salesman.",
            available=True,
        ),
        Feature(
            "credit_sales",
            "Credit sales",
            "Sell on account: \"Credit\" becomes a payment option at the counter, for a named customer only.",
            available=True,
        ),
        Feature(
            "multi_price_list",
            "Price lists",
            "Keep more than one selling price per item - wholesale, per branch, per delivery app - and pick "
            "which one each counter bills from.",
            available=True,
        ),
        Feature(
            "favourites",
            "Favourites row at the counter",
            "A row of your best sellers (and any items you pin) at the top of the POS screen.",
            available=True,
        ),
        Feature(
            "day_close",
            "Day open / close",
            "Each counter opens the day with the cash in the drawer, records cash in/out, and closes with a "
            "count - the Z-report shows expected vs actual. A closed day's bills can't be voided.",
            available=True,
        ),
        Feature(
            "einvoice",
            "e-Invoice (IRN)",
            "IRN and signed QR for B2B and export invoices and their credit / debit notes - through your GSP or "
            "NIC, or as JSON for the IRP's bulk-upload tool. Only applies once turnover crosses Rs. 5 crore.",
            available=True,
        ),
        Feature(
            "eway_bill",
            "e-Way Bill",
            "e-way bills for goods moving above Rs. 50,000 - invoices, delivery challans and stock transfers - "
            "through your GSP or NIC, or as JSON for bulk upload on the portal. Vehicle updates and cancellation.",
            available=True,
        ),
        Feature(
            "loyalty",
            "Loyalty points",
            "Customers earn points on every counter bill and spend them at the counter; each customer's purchase "
            "history and points in one place.",
            available=True,
        ),
        Feature(
            "offline_pos",
            "Offline billing",
            "The POS keeps billing when the internet drops: the screen and items are saved on the device, bills "
            "are numbered in that device's own series (O1/2026-27/0001...) and sent to the server when the "
            "connection is back. Plain cash / UPI / card bills only - credit, returns and discounts wait for "
            "the connection.",
            available=True,
        ),
        Feature(
            "cashier_pin",
            "Cashier PINs",
            "Several cashiers share one branch counter: each switches in with their own PIN, every bill shows who "
            "rang it up, and the counter can lock itself when left idle.",
            available=True,
        ),
        Feature(
            "daily_summary",
            "Daily owner summary",
            "The end-of-day picture - sales, payment modes, branches, top items, low and oversold stock - on the "
            "dashboard, shareable on WhatsApp, and emailed each evening when email is set up.",
            available=True,
        ),
    ]
}

AVAILABLE_FEATURES = [f for f in FEATURES.values() if f.available]

# What a new client of each type starts with. Only applied when a client
# is onboarded (or when the Client Admin explicitly asks Settings to reset
# to these), never retroactively.
BUSINESS_TYPE_PRESETS: dict[BusinessType, set[str]] = {
    BusinessType.RETAIL: {"business_reports", "inventory", "purchases", "expenses", "credit_sales", "categories", "favourites"},
    BusinessType.WHOLESALE: {
        "inventory", "purchases", "expenses", "credit_sales", "multi_price_list", "eway_bill", "receivables",
        "payables", "sales_documents", "cash_bank_book", "business_reports", "wholesale",
    },
    BusinessType.RESTAURANT: {"business_reports", "categories", "restaurant_mode", "expenses"},
    BusinessType.MOBILE_ELECTRONICS: {
        "business_reports", "inventory", "purchases", "expenses", "credit_sales", "serial_imei", "margin_scheme", "job_cards",
        "electronics",
    },
    BusinessType.PHARMACY: {"business_reports", "inventory", "purchases", "expenses", "credit_sales", "batch_expiry", "favourites",
                            "pharmacy"},
    BusinessType.APPAREL: {"business_reports", "inventory", "purchases", "expenses", "variants", "categories", "loyalty"},
    BusinessType.HARDWARE: {
        "inventory", "purchases", "expenses", "credit_sales", "multi_price_list", "categories", "receivables",
        "payables", "sales_documents", "business_reports",
    },
    BusinessType.JEWELLERY: {"business_reports", "inventory", "purchases", "expenses", "credit_sales", "jewellery"},
    BusinessType.BAKERY: {
        "business_reports", "inventory", "purchases", "expenses", "weighing", "batch_expiry", "categories", "favourites",
    },
    BusinessType.SERVICES: {"expenses", "credit_sales", "job_cards", "receivables", "sales_documents", "recurring_invoices",
                            "business_reports"},
    BusinessType.OTHER: set(),
}
# Every new client with the dashboard also gets the owner's end-of-day
# summary on it (Phase 10). Offline billing and cashier PINs stay opt-in.
for _preset in BUSINESS_TYPE_PRESETS.values():
    if "business_reports" in _preset:
        _preset.add("daily_summary")

# Whether shelf prices usually already include GST for this kind of
# business (MRP-printed retail goods, menu prices) - the default for new
# items' "price includes GST" box. Wholesale, jewellery and services
# usually quote before tax.
PRICES_INCLUDE_TAX_BY_DEFAULT = {
    BusinessType.RETAIL,
    BusinessType.RESTAURANT,
    BusinessType.MOBILE_ELECTRONICS,
    BusinessType.PHARMACY,
    BusinessType.APPAREL,
    BusinessType.HARDWARE,
    BusinessType.BAKERY,
}

BUSINESS_TYPE_LABELS = {
    BusinessType.RETAIL: "Retail / kirana / general store",
    BusinessType.WHOLESALE: "Wholesale / distributor",
    BusinessType.RESTAURANT: "Restaurant / cafe / cloud kitchen",
    BusinessType.MOBILE_ELECTRONICS: "Mobiles & electronics (new / used)",
    BusinessType.PHARMACY: "Pharmacy / medical store",
    BusinessType.APPAREL: "Garments / footwear",
    BusinessType.HARDWARE: "Hardware / building material",
    BusinessType.JEWELLERY: "Jewellery",
    BusinessType.BAKERY: "Bakery / sweets",
    BusinessType.SERVICES: "Services (repairs, salon, consultant)",
    BusinessType.OTHER: "Other",
}

BUSINESS_TYPE_CHOICES = [(t.value, BUSINESS_TYPE_LABELS[t]) for t in BusinessType]


def preset_features(business_type: BusinessType) -> dict[str, bool]:
    enabled = BUSINESS_TYPE_PRESETS.get(business_type, set())
    return {key: key in enabled for key in FEATURES}


def apply_business_type_preset(tenant, business_type: BusinessType) -> None:
    tenant.business_type = business_type
    tenant.features = preset_features(business_type)
    tenant.default_price_includes_tax = business_type in PRICES_INCLUDE_TAX_BY_DEFAULT


def current_tenant_has(key: str) -> bool:
    """Template/route helper: does the signed-in user's own client have
    this flag on? Always False for admin-hierarchy logins (no tenant)."""
    if not current_user.is_authenticated or current_user.tenant_id is None:
        return False
    tenant = current_user.tenant
    return bool(tenant and tenant.has_feature(key))


def current_tenant_reads(key: str) -> bool:
    """Is this module read-only for the signed-in user's client?"""
    if not current_user.is_authenticated or current_user.tenant_id is None:
        return False
    from app.utils.entitlements import READ_ONLY, module_mode

    tenant = current_user.tenant
    return bool(tenant and module_mode(tenant, key) == READ_ONLY)


def feature_required(key: str):
    """Route guard: 404 when the signed-in client has this flag off - a
    switched-off module is invisible, not merely forbidden.

    A module the client's package took away while it held records is
    read-only: its pages still open (view, print, export) but nothing can
    be saved there - any other request is refused (403)."""
    if key not in FEATURES:
        raise ValueError(f"Unknown feature flag: {key}")

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not current_tenant_has(key):
                if not current_tenant_reads(key):
                    abort(404)
                if request.method not in ("GET", "HEAD"):
                    abort(403)
                g.read_only_module = FEATURES[key].label
            return view(*args, **kwargs)

        return wrapped

    return decorator
