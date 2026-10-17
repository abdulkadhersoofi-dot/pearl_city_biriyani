"""Auditor powers in the console (packages and powers, stage 4), and the
Ultra Admin's switches for one branch login (stage 5).

The Ultra Admin sets each Auditor's and Sub-Auditor's powers, the packages
they may give clients, and their client / Sub-Auditor limits. An Auditor
sets their own Sub-Auditors' powers - only powers they hold themselves,
and only from their own allowed packages. A Sub-Auditor never holds a
power their Auditor doesn't (app/utils/entitlements.auditor_can).
"""

from decimal import Decimal, InvalidOperation

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user

from app.auth.decorators import admin_or_auditor_required, roles_required
from app.extensions import db
from app.models.package import Package
from app.models.user import User, UserRole
from app.tenants import tenants_bp
from app.utils.entitlements import (
    AUDITOR_POWERS, ROLE_DEFAULT_POWERS, allowed_packages, auditor_can, client_room, grantable_powers,
    set_auditor_powers, set_login_switches, sub_auditor_room,
)
from app.utils.permissions import PERMISSION_COLUMNS


def may_set_powers(viewer, auditor) -> bool:
    """The Ultra Admin for anyone; an Auditor for their own Sub-Auditors."""
    if viewer.is_super_admin:
        return True
    return bool(viewer.is_auditor and auditor.is_sub_auditor and auditor.parent_auditor_id == viewer.id)


def auditor_powers_context(viewer, auditor):
    """What the auditor page's "Powers" section shows (None if `viewer`
    may not change them)."""
    if not may_set_powers(viewer, auditor):
        return None
    role = "auditor" if auditor.is_auditor else "sub_auditor"
    grantable = set(grantable_powers(viewer))
    if auditor.is_sub_auditor:
        grantable.discard("create_sub_auditors")
    grant = auditor.power_grant
    rows = [{"key": key, "label": label, "on": auditor_can(auditor, key), "default": ROLE_DEFAULT_POWERS[role][key],
             "set": grant is not None and key in (grant.powers or {}), "grantable": key in grantable}
            for key, label in AUDITOR_POWERS.items()]
    packages = (Package.query.filter_by(is_active=True).order_by(Package.sort_order, Package.name).all()
                if viewer.is_super_admin else allowed_packages(viewer))
    return {
        "rows": rows, "packages": packages,
        "allowed_ids": set(grant.allowed_package_ids or []) if grant is not None else set(),
        "max_clients": grant.max_clients if grant is not None else None,
        "max_sub_auditors": grant.max_sub_auditors if grant is not None else None,
        "clients_now": client_room(auditor)[0],
        "subs_now": sub_auditor_room(auditor)[0] if auditor.is_auditor else None,
        "can_limit_subs": viewer.is_super_admin and auditor.is_auditor,
    }


def _count(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ValueError
    return int(raw)


@tenants_bp.route("/auditors/<int:auditor_id>/powers", methods=["POST"])
@admin_or_auditor_required
def save_auditor_powers(auditor_id):
    auditor = User.query.filter(User.id == auditor_id,
                                User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])).first_or_404()
    if not may_set_powers(current_user, auditor):
        abort(403)
    grantable = set(grantable_powers(current_user))
    if auditor.is_sub_auditor:
        grantable.discard("create_sub_auditors")
    ticked = set(request.form.getlist("power"))
    # Every power stored explicitly; one the editor can't give is off.
    powers = {key: (key in ticked and key in grantable) for key in AUDITOR_POWERS}
    offered = {p.id for p in (Package.query.filter_by(is_active=True).all() if current_user.is_super_admin
                              else allowed_packages(current_user))}
    package_ids = sorted(int(i) for i in request.form.getlist("package_id") if i.isdigit() and int(i) in offered)
    grant = auditor.power_grant
    try:
        max_clients = _count(request.form.get("max_clients"))
        max_subs = (_count(request.form.get("max_sub_auditors")) if current_user.is_super_admin and auditor.is_auditor
                    else (grant.max_sub_auditors if grant is not None else None))
    except ValueError:
        flash("Limits are whole numbers - or leave them blank for no limit.", "error")
        return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id, _anchor="sec-powers"))
    set_auditor_powers(auditor, powers, current_user, allowed_package_ids=package_ids, max_clients=max_clients,
                       max_sub_auditors=max_subs)
    db.session.commit()
    flash(f"Powers saved for {auditor.name}.", "success")
    return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id, _anchor="sec-powers"))


# --- the Ultra Admin's switches for one branch login (stage 5) -------------------------------------

@tenants_bp.route("/clients/<int:tenant_id>/logins/<int:user_id>/switches", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def save_login_switches(tenant_id, user_id):
    """Modules hidden at this counter, counter permissions forced on or
    off, and a discount limit - above whatever the Client Admin set."""
    login = User.query.filter_by(id=user_id, tenant_id=tenant_id, role=UserRole.STAFF).first_or_404()
    hidden = request.form.getlist("hide")
    permissions = {}
    for key in PERMISSION_COLUMNS:
        value = request.form.get(f"perm_{key}")
        if value in ("on", "off"):
            permissions[key] = value == "on"
    raw = (request.form.get("max_discount") or "").strip()
    try:
        cap = Decimal(raw) if raw else None
        if cap is not None and not Decimal("0") <= cap <= Decimal("100"):
            raise InvalidOperation
    except InvalidOperation:
        flash("The discount limit is a percentage from 0 to 100 - or leave it blank.", "error")
        return redirect(url_for("tenants.detail", tenant_id=tenant_id, _anchor=f"login-{login.id}"))
    if request.form.get("action") == "clear":
        hidden, permissions, cap = [], {}, None
    set_login_switches(login, current_user, hidden_modules=hidden, permissions=permissions, max_discount_percent=cap,
                       role=request.form.get("role") or None)
    db.session.commit()
    flash(f"{'Handed back to the client' if request.form.get('action') == 'clear' else 'Switches saved'} "
          f"for {login.name}.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant_id, _anchor=f"login-{login.id}"))
