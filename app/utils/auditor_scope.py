"""Which tenants an Auditor-hierarchy user may see/act on - the admin-side
counterpart to app.utils.tenant_scope (which scopes a signed-in tenant
user to their own tenant_id).

The Ultra Admin (Super Admin) is unrestricted - every route in
app.tenants.routes that uses this module treats `auditor_tenant_ids`
returning None as "no filter, see everything".

An Auditor sees their own directly-allocated tenants (Tenant.auditor_id)
plus every tenant allocated to any of their Sub-Auditors - the Auditor's
"office" as a whole. A Sub-Auditor sees only the tenants allocated
directly to them; they never inherit their parent Auditor's full pool.
"""

from flask import abort

from app.models.user import User, UserRole


def auditor_tenant_ids(user) -> set[int] | None:
    """None means unrestricted (Ultra Admin). Otherwise a (possibly empty)
    set of tenant ids this user may see."""
    if user.is_super_admin:
        return None
    if user.is_sub_auditor:
        return {t.id for t in user.allocated_tenants}
    if user.is_auditor:
        ids = {t.id for t in user.allocated_tenants}
        sub_auditor_ids = [u.id for u in user.sub_auditors]
        if sub_auditor_ids:
            from app.models.tenant import Tenant

            ids |= {t.id for t in Tenant.query.filter(Tenant.auditor_id.in_(sub_auditor_ids)).all()}
        return ids
    abort(403)


def assert_auditor_owns_tenant(user, tenant) -> None:
    """403s if `user` (an Auditor/Sub-Auditor/Ultra Admin) is not allowed
    to see/act on `tenant`. Ultra Admin always passes."""
    ids = auditor_tenant_ids(user)
    if ids is not None and tenant.id not in ids:
        abort(403)


def assert_auditor_owns_auditor(user, other) -> None:
    """403s unless `user` (Ultra Admin, or an Auditor managing their own
    Sub-Auditors) may view/manage the auditor-hierarchy account `other`."""
    if user.is_super_admin:
        return
    if user.is_auditor and other.is_sub_auditor and other.parent_auditor_id == user.id:
        return
    if user.id == other.id:
        return
    abort(403)


def visible_auditors(user):
    """Every Auditor/Sub-Auditor this user may see in a directory listing -
    everyone for the Ultra Admin, or just their own Sub-Auditors for an
    Auditor."""
    if user.is_super_admin:
        return User.query.filter(User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])).order_by(User.name).all()
    if user.is_auditor:
        return list(user.sub_auditors)
    return []
