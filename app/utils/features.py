"""Per-client feature flags - the one switchboard deciding which modules a
client sees, so a kirana store, a pharmacy and a restaurant can share one
app without each wading through the others' screens.

A flag only does anything once its module is built (`available=True`);
flags for later phases are registered here already so business-type
presets set them from day one, and they light up for those clients the
moment the module ships - but Settings only offers switches that do
something today.
"""

from dataclasses import dataclass
from functools import wraps

from flask import abort
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
        Feature("expenses", "Expenses", "Record rent, salary, electricity and other expenses."),
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
        Feature("margin_scheme", "Margin scheme", "GST on margin for second-hand goods."),
        Feature("weighing", "Weighing scale items", "Price per kg, weight entry at billing."),
        Feature("restaurant_mode", "Restaurant tables & KOT", "Tables, kitchen order tickets, order types."),
        Feature("job_cards", "Job cards", "Repair and service jobs from receipt to delivery."),
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
        Feature("einvoice", "e-Invoice", "Generate IRN and QR for B2B invoices."),
        Feature("eway_bill", "e-Way Bill", "e-Way bills for goods movement."),
        Feature("loyalty", "Loyalty points", "Points on purchases, redeemable at billing."),
    ]
}

AVAILABLE_FEATURES = [f for f in FEATURES.values() if f.available]

# What a new client of each type starts with. Only applied when a client
# is onboarded (or when the Client Admin explicitly asks Settings to reset
# to these), never retroactively.
BUSINESS_TYPE_PRESETS: dict[BusinessType, set[str]] = {
    BusinessType.RETAIL: {"inventory", "purchases", "expenses", "credit_sales", "categories", "favourites"},
    BusinessType.WHOLESALE: {"inventory", "purchases", "expenses", "credit_sales", "multi_price_list", "eway_bill"},
    BusinessType.RESTAURANT: {"categories", "restaurant_mode", "expenses"},
    BusinessType.MOBILE_ELECTRONICS: {
        "inventory", "purchases", "expenses", "credit_sales", "serial_imei", "margin_scheme", "job_cards",
    },
    BusinessType.PHARMACY: {"inventory", "purchases", "expenses", "credit_sales", "batch_expiry", "favourites"},
    BusinessType.APPAREL: {"inventory", "purchases", "expenses", "variants", "categories", "loyalty"},
    BusinessType.HARDWARE: {"inventory", "purchases", "expenses", "credit_sales", "multi_price_list", "categories"},
    BusinessType.JEWELLERY: {"inventory", "purchases", "expenses", "credit_sales", "weighing"},
    BusinessType.BAKERY: {"inventory", "purchases", "expenses", "weighing", "batch_expiry", "categories", "favourites"},
    BusinessType.SERVICES: {"expenses", "credit_sales", "job_cards"},
    BusinessType.OTHER: set(),
}

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


def feature_required(key: str):
    """Route guard: 404 when the signed-in client has this flag off - a
    switched-off module is invisible, not merely forbidden."""
    if key not in FEATURES:
        raise ValueError(f"Unknown feature flag: {key}")

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not current_tenant_has(key):
                abort(404)
            return view(*args, **kwargs)

        return wrapped

    return decorator
