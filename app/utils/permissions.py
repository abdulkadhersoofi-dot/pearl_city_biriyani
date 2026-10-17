"""Per-login permissions for branch/counter (Staff) logins.

A Client Admin can always do everything in their own business; these
switches only ever narrow what a Staff login may do at the counter. Each
defaults to exactly what a Staff login could already do before this
existed (refund from the Day-book and see the Day-book/Z-report: yes;
anything else: no), so adding them changed nothing for anyone.
"""

from decimal import Decimal
from functools import wraps

from flask import abort
from flask_login import current_user, login_required

# permission key -> User column
PERMISSION_COLUMNS = {
    "discount": "can_give_discount",
    "refund": "can_refund",
    "void": "can_void",
    "view_reports": "can_view_reports",
    "edit_price": "can_edit_price_at_pos",
    "manage_stock": "can_manage_stock",
}

PERMISSION_LABELS = {
    "discount": "Give discounts at billing (up to the limit below)",
    "refund": "Refund bills",
    "void": "Void bills",
    "view_reports": "See the Day-book and Z-report",
    "edit_price": "Change an item's price at billing",
    "manage_stock": "Adjust and transfer stock",
}


# Roles a Client Admin gives a branch login (packages and powers, stage 5):
# each fills the six permissions, which stay editable one by one. A role
# can only hand out what the client's package allows (see
# entitlements.counter_permissions_allowed). "Cashier" is exactly what a
# new branch login could do before roles existed.
BRANCH_ROLES = {
    "cashier": {
        "label": "Cashier", "help": "Bills, refunds, and sees the Day-book.",
        "permissions": {"discount": False, "refund": True, "void": False, "view_reports": True,
                        "edit_price": False, "manage_stock": False},
        "max_discount": Decimal("0"),
    },
    "senior_cashier": {
        "label": "Senior cashier", "help": "A cashier who may also give discounts, change prices and void bills.",
        "permissions": {"discount": True, "refund": True, "void": True, "view_reports": True,
                        "edit_price": True, "manage_stock": False},
        "max_discount": Decimal("10"),
    },
    "manager": {
        "label": "Manager", "help": "Everything at this counter, stock included.",
        "permissions": {"discount": True, "refund": True, "void": True, "view_reports": True,
                        "edit_price": True, "manage_stock": True},
        "max_discount": Decimal("25"),
    },
    "stock_keeper": {
        "label": "Stock keeper", "help": "Bills, and adjusts and transfers stock - no refunds or discounts.",
        "permissions": {"discount": False, "refund": False, "void": False, "view_reports": True,
                        "edit_price": False, "manage_stock": True},
        "max_discount": Decimal("0"),
    },
}


def role_of(permissions: dict, max_discount) -> str | None:
    """The role these six permissions (and discount limit) make up, if any."""
    for key, role in BRANCH_ROLES.items():
        if all(bool(permissions.get(p)) == v for p, v in role["permissions"].items()) and (
                not role["permissions"]["discount"] or Decimal(max_discount or 0) == role["max_discount"]):
            return key
    return None


def user_can(user, permission: str) -> bool:
    if permission not in PERMISSION_COLUMNS:
        raise ValueError(f"Unknown permission: {permission}")
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_client_admin:
        return True
    if user.is_staff:
        from app.utils.entitlements import branch_permission

        return branch_permission(user, permission)
    return False


def max_discount_percent(user) -> Decimal:
    if user.is_client_admin:
        return Decimal("100")
    if user.is_staff:
        from app.utils.entitlements import branch_max_discount

        return branch_max_discount(user)
    return Decimal("0")


def current_user_can(permission: str) -> bool:
    return user_can(current_user, permission)


def permission_required(permission: str):
    """Route guard: 403 unless the signed-in login holds this permission."""
    if permission not in PERMISSION_COLUMNS:
        raise ValueError(f"Unknown permission: {permission}")

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not user_can(current_user, permission):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator
