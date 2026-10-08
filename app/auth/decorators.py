from functools import wraps

from flask import abort
from flask_login import current_user, login_required

from app.models.user import UserRole


def roles_required(*roles: UserRole):
    """Restricts a route to specific roles. Always combine with
    @login_required (or put it first in the stack) - this decorator only
    checks role membership once a user is signed in.
    """

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def tenant_user_required(view):
    """Blocks any admin-hierarchy login (Ultra Admin, Auditor, Sub-Auditor -
    tenant_id is None) from client-only screens like the POS/invoicing
    forms - the firm views client data through the admin console, or
    through "act as", not by using the client's own screens directly."""

    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.tenant_id is None:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def admin_or_auditor_required(view):
    """Restricts a route to the shared admin console (Ultra Admin, Auditor,
    Sub-Auditor) - the three roles that manage clients rather than being
    one. Per-tenant/per-auditor ownership within that console is checked
    separately, per route, via app.utils.auditor_scope."""

    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin_hierarchy:
            abort(403)
        return view(*args, **kwargs)

    return wrapped
