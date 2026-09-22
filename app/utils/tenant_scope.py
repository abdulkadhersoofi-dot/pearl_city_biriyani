from flask import abort
from flask_login import current_user


def tenant_query(model):
    """Every business-data query in the app goes through this - filters to
    the signed-in user's own tenant. Client Admins and Staff never see
    another client's rows this way, even if they guess an id in the URL.
    Super Admin routes must not use this helper; they pass a tenant_id
    explicitly and are audit-logged instead.
    """
    if current_user.tenant_id is None:
        abort(403)
    return model.query.filter_by(tenant_id=current_user.tenant_id)


def assert_owns(instance) -> None:
    """Defense in depth for single-record fetches (get_or_404 then this)."""
    if getattr(instance, "tenant_id", None) != current_user.tenant_id:
        abort(403)
