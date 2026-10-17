"""Settings of the business-type modules (Tenant.module_settings), with
their defaults - one place to read them from, so a client that never
opened a module's settings page still gets sensible behaviour."""

import copy
from decimal import Decimal

from app.utils import gst_config

ORDER_TYPES = {
    "dine_in": "Dine-in",
    "takeaway": "Takeaway",
    "delivery": "Delivery",
    "swiggy": "Swiggy",
    "zomato": "Zomato",
}

DEFAULTS = {
    "restaurant": {
        # Which order types the counter offers, and the price list each
        # bills from (None = the items' normal prices).
        "order_types": list(ORDER_TYPES),
        "price_lists": {key: None for key in ORDER_TYPES},
        # Packing: per item or per order, on these order types.
        "packing_charge": "0",
        "packing_per": "order",  # order / item
        "packing_on": ["takeaway", "delivery", "swiggy", "zomato"],
        # Service charge: a % of the food, only if the cashier ticks it
        # (gst_config.SERVICE_CHARGE_DEFAULT_ON) - dine-in only.
        "service_charge_percent": "0",
        "charge_gst_rate": str(gst_config.RESTAURANT_SERVICE_GST_RATE),
    },
    "weighing": {
        # Weighing-scale labels: EAN-13 = 2-digit prefix + item code +
        # value + check digit. Each prefix says what the value is.
        "prefixes": {"21": "weight", "22": "price"},
        "code_digits": 5,
    },
    "loyalty": {
        "points_per_100": "1",  # points earned per Rs. 100 spent
        "point_value": "1",  # rupees one point is worth when spent
        "min_redeem": 50,  # points needed before any can be spent
    },
    # Phase 10 - sign-in and counter settings a Client Admin picks.
    "security": {
        # Sign this client's logins out after this many idle minutes;
        # 0 = never (how it always was).
        "idle_minutes": 0,
    },
    # Second-hand goods (margin scheme) - app/margin.
    "margin": {
        # How the part of a margin-scheme sale that isn't margin (sale
        # price - margin) is reported: "exempt" - GSTR-1 Table 8 and
        # GSTR-3B 3.1(c), so the returns add up to the turnover; "none" -
        # only in the HSN summary's 0% row (the value of supply is the
        # margin alone, Rule 32(5)).
        "remainder_as": "exempt",
        # Ask for the seller's ID proof when buying from a person.
        "id_proof_required": True,
    },
    "cashier": {
        # Lock a counter with cashier PINs after this many idle minutes
        # (0 = only when a cashier taps Lock / Switch).
        "lock_minutes": 0,
    },
}


def module_settings(tenant, module: str) -> dict:
    merged = copy.deepcopy(DEFAULTS.get(module, {}))
    saved = (tenant.module_settings or {}).get(module) or {}
    for key, value in saved.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def save_module_settings(tenant, module: str, values: dict) -> None:
    current = dict(tenant.module_settings or {})
    current[module] = values
    tenant.module_settings = current  # reassigned: a JSON column doesn't track in-place changes


def dec(value, default="0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except Exception:
        return Decimal(default)
