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


def user_can(user, permission: str) -> bool:
    if permission not in PERMISSION_COLUMNS:
        raise ValueError(f"Unknown permission: {permission}")
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_client_admin:
        return True
    if user.is_staff:
        return bool(getattr(user, PERMISSION_COLUMNS[permission]))
    return False


def max_discount_percent(user) -> Decimal:
    if user.is_client_admin:
        return Decimal("100")
    if user.is_staff and user.can_give_discount:
        return Decimal(user.max_discount_percent or 0)
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
