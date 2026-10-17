"""Packages in the console (packages and powers, stage 2) - Ultra Admin only.

  /admin/packages                      every package, with how many clients use it
  /admin/packages/new[?copy=<id>]      a new package (or a copy of one)
  /admin/packages/<id>                 edit one; retire / bring back
  /admin/clients/<id>/package          give a client a package (with a preview first)
  /admin/clients/<id>/exceptions       change single modules / settings / limits for one client

Every change goes through app/utils/entitlements.py, which writes the
audit log. A package in use can't be deleted, only retired: its clients
keep it, nobody new gets it.
"""

from decimal import Decimal, InvalidOperation

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import admin_or_auditor_required, roles_required
from app.extensions import db
from app.models.package import ModuleRequest, Package, TenantPackage
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.settings.sections import MODULE_GROUPS
from app.tenants import tenants_bp
from app.utils.audit import record_audit
from app.utils.auditor_scope import assert_auditor_owns_tenant
from app.utils.entitlements import (
    AS_PACKAGE, CHOICE, LIMITS, MODULE_STATES, OFF, SETTING_STATES, SETTINGS, allowed_packages, assign_package,
    auditor_can, ensure_starting_packages,
    grant_of, module_state, own_branding, package_module_state, package_price_for, package_summary, preview_package,
    refresh_read_only, resolve_module_request, set_branding_exception, set_limit_exception, set_module_exception, set_setting_exception,
    setting_state,
)
from app.utils.entitlements import (
    IDLE_MINUTE_CHOICES, LOCK_MINUTE_CHOICES, SECURITY_MINIMUMS, security_from_form, set_counter_permissions_exception,
    set_security_exception,
)
from app.utils.features import AVAILABLE_FEATURES, FEATURES
from app.utils.permissions import PERMISSION_COLUMNS, PERMISSION_LABELS


def module_rows():
    """[(group label, [Feature, ...])] in the Settings > Modules order;
    any module not in a group goes last."""
    seen = set()
    out = []
    for label, items in MODULE_GROUPS:
        features = [FEATURES[key] for key, *_ in items if key in FEATURES and FEATURES[key].available]
        seen.update(f.key for f in features)
        out.append((label, features))
    rest = [f for f in AVAILABLE_FEATURES if f.key not in seen]
    if rest:
        out.append(("Other", rest))
    return out


def _money(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError("Enter prices in rupees, like 499 or 4999.00.")
    if value < 0:
        raise ValueError("A price can't be below zero.")
    return value


def _count(raw, label):
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ValueError(f"{label}: enter a whole number, or leave it blank for no limit.")
    return int(raw)


def _login_bits() -> dict:
    """What the package editor and a client's exceptions need for branch
    logins and sign-in (stage 5)."""
    return {"permission_labels": PERMISSION_LABELS, "security_minimums": SECURITY_MINIMUMS,
            "idle_choices": IDLE_MINUTE_CHOICES, "lock_choices": LOCK_MINUTE_CHOICES}


def _clients_using(package_id):
    return TenantPackage.query.filter_by(package_id=package_id).count()


# --- the Packages screen -----------------------------------------------------------------------------

@tenants_bp.route("/packages")
@roles_required(UserRole.SUPER_ADMIN)
def packages():
    ensure_starting_packages()
    db.session.commit()
    rows = Package.query.order_by(Package.is_active.desc(), Package.sort_order, Package.name).all()
    counts = dict(db.session.query(TenantPackage.package_id, db.func.count(TenantPackage.id))
                  .group_by(TenantPackage.package_id).all())
    return render_template("tenants/packages.html", packages=rows, counts=counts,
                           summaries={p.id: package_summary(p) for p in rows}, limits=LIMITS)


def _package_from_form(package):
    """Fill `package` from the editor's post; returns a list of problems."""
    errors = []
    name = (request.form.get("name") or "").strip()
    if not name:
        errors.append("Give the package a name.")
    elif Package.query.filter(Package.name == name, Package.id != (package.id or 0)).first():
        errors.append(f"There is already a package called {name}.")
    package.name = name or package.name
    package.description = (request.form.get("description") or "").strip() or None
    try:
        package.price_monthly = _money(request.form.get("price_monthly"))
        package.price_yearly = _money(request.form.get("price_yearly"))
    except ValueError as exc:
        errors.append(str(exc))
    limits = {}
    for key, label in LIMITS.items():
        try:
            value = _count(request.form.get(f"limit_{key}"), label)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if value is not None:
            limits[key] = value
    package.limits = limits
    package.own_branding = request.form.get("own_branding") == "y"
    unlisted = request.form.get("unlisted_modules")
    package.unlisted_modules = unlisted if unlisted in MODULE_STATES else OFF
    modules = {}
    for feature in AVAILABLE_FEATURES:
        state = request.form.get(f"mod_{feature.key}")
        if state not in MODULE_STATES:
            errors.append(f"Choose Off, Included or Client's choice for {feature.label}.")
            continue
        modules[feature.key] = state
    package.modules = modules
    settings = {}
    for key in SETTINGS:
        state = request.form.get(f"set_{key}")
        settings[key] = state if state in SETTING_STATES else "client"
    package.settings = settings
    # Branch logins and sign-in (stage 5). Every permission ticked = all, as
    # before packages (stored as "no narrowing").
    if request.form.get("counter_form"):
        ticked = [k for k in PERMISSION_COLUMNS if k in set(request.form.getlist("counter_permission"))]
        package.counter_permissions = None if len(ticked) == len(PERMISSION_COLUMNS) else ticked
        package.security = security_from_form(request.form)
    return errors


@tenants_bp.route("/packages/new", methods=["GET", "POST"])
@roles_required(UserRole.SUPER_ADMIN)
def package_new():
    source = Package.query.get(request.args.get("copy", type=int) or 0)
    package = Package(name="", modules={}, settings={}, limits={}, unlisted_modules=OFF, own_branding=True,
                      security={})
    if source is not None and request.method == "GET":
        package.name = f"{source.name} (copy)"
        package.description = source.description
        package.modules = {f.key: package_module_state(source, f.key) for f in AVAILABLE_FEATURES}
        package.settings = dict(source.settings or {})
        package.limits = dict(source.limits or {})
        package.own_branding = source.own_branding
        package.counter_permissions = list(source.counter_permissions) if source.counter_permissions is not None else None
        package.security = dict(source.security or {})
        package.price_monthly, package.price_yearly = source.price_monthly, source.price_yearly
    if request.method == "POST":
        errors = _package_from_form(package)
        if errors:
            for error in errors:
                flash(error, "error")
        else:
            package.sort_order = (db.session.query(db.func.max(Package.sort_order)).scalar() or 0) + 10
            db.session.add(package)
            db.session.flush()
            record_audit(current_user, "package_created", entity_type="package", entity_id=package.id,
                         details={"name": package.name, "copied_from": source.name if source else None})
            db.session.commit()
            flash(f"Package {package.name} created.", "success")
            return redirect(url_for("tenants.package_edit", package_id=package.id))
    return render_template("tenants/package_form.html", package=package, groups=module_rows(), clients=0,
                           settings=SETTINGS, limits=LIMITS, states=MODULE_STATES, state_of=package_module_state,
                           is_new=True, **_login_bits())


@tenants_bp.route("/packages/<int:package_id>", methods=["GET", "POST"])
@roles_required(UserRole.SUPER_ADMIN)
def package_edit(package_id):
    package = Package.query.get_or_404(package_id)
    if request.method == "POST":
        before = {"modules": dict(package.modules or {}), "settings": dict(package.settings or {}),
                  "limits": dict(package.limits or {}), "own_branding": package.own_branding,
                  "counter_permissions": package.counter_permissions, "security": dict(package.security or {}),
                  "price_monthly": str(package.price_monthly), "price_yearly": str(package.price_yearly)}
        errors = _package_from_form(package)
        if errors:
            db.session.rollback()
            for error in errors:
                flash(error, "error")
            package = Package.query.get_or_404(package_id)
        else:
            # Clients on it may have just lost (or got back) a module with records.
            for grant in TenantPackage.query.filter_by(package_id=package.id).all():
                refresh_read_only(grant.tenant)
            record_audit(current_user, "package_edited", entity_type="package", entity_id=package.id,
                         details={"name": package.name, "before": before,
                                  "after": {"modules": package.modules, "settings": package.settings,
                                            "limits": package.limits, "own_branding": package.own_branding,
                                            "counter_permissions": package.counter_permissions,
                                            "security": package.security,
                                            "price_monthly": str(package.price_monthly),
                                            "price_yearly": str(package.price_yearly)}})
            db.session.commit()
            flash(f"Package {package.name} saved.", "success")
            return redirect(url_for("tenants.package_edit", package_id=package.id))
    return render_template("tenants/package_form.html", package=package, groups=module_rows(),
                           clients=_clients_using(package.id), settings=SETTINGS, limits=LIMITS,
                           states=MODULE_STATES, state_of=package_module_state, is_new=False, **_login_bits())


@tenants_bp.route("/packages/<int:package_id>/active", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def package_active(package_id):
    package = Package.query.get_or_404(package_id)
    package.is_active = request.form.get("active") == "1"
    record_audit(current_user, "package_retired" if not package.is_active else "package_restored",
                 entity_type="package", entity_id=package.id, details={"name": package.name})
    db.session.commit()
    flash(f"{package.name} is {'available again' if package.is_active else 'retired - its clients keep it, nobody new gets it'}.",
          "success")
    return redirect(url_for("tenants.package_edit", package_id=package.id))


# --- a client's package and exceptions ---------------------------------------------------------------

def client_package_context(tenant, viewer=None):
    """What the client page's "Package & modules" section shows. The
    Ultra Admin sees all of it; an auditor only the parts their powers
    allow (choosing from their allowed packages, switching the client's
    own-choice modules) - None if neither."""
    from flask_login import current_user

    viewer = viewer or current_user
    is_ultra = bool(viewer.is_super_admin)
    can_assign = is_ultra or auditor_can(viewer, "assign_packages")
    can_switch = is_ultra or auditor_can(viewer, "switch_client_modules")
    if not (can_assign or can_switch):
        return None
    ensure_starting_packages()
    grant = grant_of(tenant)
    current = grant.package if grant is not None else None
    rows = []
    for label, features in module_rows():
        items = []
        for f in features:
            exception = (grant.module_overrides or {}).get(f.key) if grant is not None else None
            items.append({"feature": f, "package_state": package_module_state(current, f.key) if current else CHOICE,
                          "exception": exception, "state": module_state(tenant, f.key), "on": tenant.has_feature(f.key)})
        rows.append((label, items))
    setting_rows = [{"key": key, "label": label,
                     "package_state": (current.settings or {}).get(key, "client") if current else "client",
                     "exception": (grant.setting_overrides or {}).get(key) if grant is not None else None,
                     "state": setting_state(tenant, key)} for key, label in SETTINGS.items()]
    limit_rows = [{"key": key, "label": label,
                   "package_value": (current.limits or {}).get(key) if current else None,
                   "has_exception": grant is not None and key in (grant.limit_overrides or {}),
                   "exception": (grant.limit_overrides or {}).get(key) if grant is not None else None}
                  for key, label in LIMITS.items()]
    security_rows = [{"key": key, "label": label,
                      "package_value": (current.security or {}).get(key) if current else None,
                      "has_exception": grant is not None and key in (grant.security_overrides or {}),
                      "exception": (grant.security_overrides or {}).get(key) if grant is not None else None,
                      "choices": {"idle_minutes": IDLE_MINUTE_CHOICES, "lock_minutes": LOCK_MINUTE_CHOICES}.get(key)}
                     for key, label in SECURITY_MINIMUMS.items()]
    return {
        "grant": grant, "current_package": current,
        "package_choices": allowed_packages(viewer),
        "security_rows": security_rows, "permission_labels": PERMISSION_LABELS,
        "counter_exception": grant.counter_permissions_override if grant is not None else None,
        "counter_package": current.counter_permissions if current else None,
        "is_ultra": is_ultra, "can_assign": can_assign, "can_switch": can_switch,
        "module_rows": rows, "setting_rows": setting_rows, "limit_rows": limit_rows,
        "branding_exception": grant.own_branding_override if grant is not None else None,
        "branding_now": own_branding(tenant), "branding_package": current.own_branding if current else True,
        "exception_count": (len(grant.module_overrides or {}) + len(grant.setting_overrides or {})
                            + len(grant.limit_overrides or {}) + (grant.own_branding_override is not None)
                            + len(grant.security_overrides or {}) + (grant.counter_permissions_override is not None))
        if grant is not None else 0,
        "module_states": MODULE_STATES, "setting_states": SETTING_STATES,
        "requests": [{"req": r, "label": FEATURES[r.feature_key].label if r.feature_key in FEATURES else r.feature_key}
                     for r in ModuleRequest.query.filter_by(tenant_id=tenant.id, status=ModuleRequest.PENDING)
                     .order_by(ModuleRequest.created_at)],
    }


def _client_for_packages(tenant_id, power):
    """The client, if the signed-in console login may do `power` for it."""
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    if not auditor_can(current_user, power):
        abort(403)
    return tenant


def _allowed_package(package_id):
    return next((p for p in allowed_packages(current_user) if p.id == package_id), None)


@tenants_bp.route("/clients/<int:tenant_id>/package/preview")
@admin_or_auditor_required
def package_preview(tenant_id):
    tenant = _client_for_packages(tenant_id, "assign_packages")
    package = _allowed_package(request.args.get("package_id", type=int)) or abort(404)
    keep = request.args.get("keep_exceptions") == "y"
    return render_template("tenants/_package_preview.html", tenant=tenant, package=package,
                           changes=preview_package(tenant, package, keep), summary=package_summary(package),
                           price=package_price_for(package, tenant.billing_cycle))


@tenants_bp.route("/clients/<int:tenant_id>/package", methods=["POST"])
@admin_or_auditor_required
def assign_client_package(tenant_id):
    tenant = _client_for_packages(tenant_id, "assign_packages")
    package = _allowed_package(request.form.get("package_id", type=int))
    if package is None:
        flash("Pick a package that's available.", "error")
        return redirect(url_for("tenants.detail", tenant_id=tenant.id, _anchor="sec-package"))
    # Only the Ultra Admin's exceptions exist, and only they may keep or drop them.
    keep = request.form.get("keep_exceptions") == "y" or not current_user.is_super_admin
    changes = preview_package(tenant, package, keep)
    assign_package(tenant, package, current_user, keep_exceptions=keep)
    message = f"{tenant.legal_name} is now on {package.name}"
    if changes:
        on = sum(1 for c in changes if c["after"])
        message += f" - {on} module{'s' if on != 1 else ''} on, {len(changes) - on} off"
    price = package_price_for(package, tenant.billing_cycle)
    if request.form.get("use_price") == "y" and price is not None and current_user.is_super_admin:
        tenant.subscription_fee = price
        record_audit(current_user, "subscription_fee_set", tenant_id=tenant.id, entity_type="tenant",
                     entity_id=tenant.id, details={"fee": str(price), "from_package": package.name})
        message += f"; fee set to Rs. {price}"
    db.session.commit()
    flash(message + ".", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id, _anchor="sec-package"))


@tenants_bp.route("/clients/<int:tenant_id>/exceptions", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def save_client_exceptions(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    grant = grant_of(tenant)
    changed = 0
    for feature in AVAILABLE_FEATURES:
        wanted = request.form.get(f"mod_{feature.key}")
        if wanted is None:
            continue
        current = (grant.module_overrides or {}).get(feature.key) if grant is not None else None
        target = None if wanted == AS_PACKAGE else wanted
        if target is not None and target not in MODULE_STATES:
            abort(400)
        if target != current:
            grant = set_module_exception(tenant, feature.key, target, current_user)
            changed += 1
    for key in SETTINGS:
        wanted = request.form.get(f"set_{key}")
        if wanted is None:
            continue
        current = (grant.setting_overrides or {}).get(key) if grant is not None else None
        target = None if wanted == AS_PACKAGE else wanted
        if target is not None and target not in SETTING_STATES:
            abort(400)
        if target != current:
            grant = set_setting_exception(tenant, key, target, current_user)
            changed += 1
    for key, label in LIMITS.items():
        mode = request.form.get(f"limitmode_{key}")
        if mode is None:
            continue
        overrides = (grant.limit_overrides or {}) if grant is not None else {}
        current = overrides[key] if key in overrides else AS_PACKAGE
        if mode == "none":
            target = None
        elif mode == "number":
            try:
                target = _count(request.form.get(f"limit_{key}"), label)
            except ValueError as exc:
                flash(str(exc), "error")
                continue
            if target is None:
                flash(f"{label}: enter a number for this client's own limit.", "error")
                continue
        else:
            target = AS_PACKAGE
        if target != current:
            grant = set_limit_exception(tenant, key, target, current_user)
            changed += 1
    branding = request.form.get("own_branding")
    if branding is not None:
        target = {"yes": True, "no": False}.get(branding)
        current = grant.own_branding_override if grant is not None else None
        if target != current:
            set_branding_exception(tenant, target, current_user)
            changed += 1
    # Counter permissions and sign-in minimums (stage 5).
    counter_mode = request.form.get("counter_mode")
    if counter_mode is not None:
        target = (None if counter_mode != "own"
                  else [k for k in PERMISSION_COLUMNS if k in set(request.form.getlist("counter_permission"))])
        current = grant.counter_permissions_override if grant is not None else None
        if target != current:
            grant = set_counter_permissions_exception(tenant, target, current_user)
            changed += 1
    for key in SECURITY_MINIMUMS:
        wanted = request.form.get(f"secmode_{key}")
        if wanted is None:
            continue
        if wanted == AS_PACKAGE:
            target = AS_PACKAGE
        elif key == "owner_two_step":
            target = {"yes": True, "no": False}.get(wanted, AS_PACKAGE)
        elif wanted == "none":
            target = None
        elif wanted.isdigit():
            target = int(wanted)
        else:
            abort(400)
        overrides = (grant.security_overrides or {}) if grant is not None else {}
        current = overrides[key] if key in overrides else AS_PACKAGE
        if target != current:
            grant = set_security_exception(tenant, key, target, current_user)
            changed += 1
    db.session.commit()
    flash(f"{changed} change{'s' if changed != 1 else ''} saved for {tenant.legal_name}." if changed
          else "Nothing changed.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id, _anchor="sec-package"))


@tenants_bp.route("/clients/<int:tenant_id>/requests/<int:request_id>", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def resolve_request(tenant_id, request_id):
    """Add a module a client asked for (as their choice, or included), or decline."""
    req = ModuleRequest.query.filter_by(id=request_id, tenant_id=tenant_id).first_or_404()
    if req.status != ModuleRequest.PENDING:
        flash("That request was already dealt with.", "info")
        return redirect(url_for("tenants.detail", tenant_id=tenant_id, _anchor="sec-package"))
    action = request.form.get("action")
    if action not in (CHOICE, "inc", "decline"):
        abort(400)
    resolve_module_request(req, action, current_user)
    db.session.commit()
    label = FEATURES[req.feature_key].label if req.feature_key in FEATURES else req.feature_key
    flash(f"Declined {label} for {req.tenant.legal_name}." if action == "decline"
          else f"{label} added for {req.tenant.legal_name}.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant_id, _anchor="sec-package"))


@tenants_bp.route("/clients/<int:tenant_id>/choices", methods=["POST"])
@admin_or_auditor_required
def save_client_choices(tenant_id):
    """Switch a client's own-choice modules on or off for them (the Ultra
    Admin, or an auditor with that power). Never touches a module the
    package includes or leaves out."""
    tenant = _client_for_packages(tenant_id, "switch_client_modules")
    ticked = set(request.form.getlist("module"))
    before = {f.key: tenant.has_feature(f.key) for f in AVAILABLE_FEATURES}
    for feature in AVAILABLE_FEATURES:
        if module_state(tenant, feature.key) == CHOICE:
            tenant.set_feature(feature.key, feature.key in ticked)
    changed = sorted(k for k, on in before.items() if tenant.has_feature(k) != on)
    if changed:
        record_audit(current_user, "client_modules_switched", tenant_id=tenant.id, entity_type="tenant",
                     entity_id=tenant.id, details={"changed": changed,
                                                   "on": [k for k in changed if tenant.has_feature(k)]})
    db.session.commit()
    flash(f"{len(changed)} module{'s' if len(changed) != 1 else ''} switched for {tenant.legal_name}." if changed
          else "Nothing changed.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id, _anchor="sec-package"))


# --- the "Changes" list (stage 6) --------------------------------------------------------------------

CHANGES_PER_PAGE = 50


@tenants_bp.route("/clients/<int:tenant_id>/changes")
@admin_or_auditor_required
def client_changes_view(tenant_id):
    """Everything changed for this client's package, modules, settings and
    logins - when, and by whom. Anyone who can open the client can read it."""
    from app.utils.changes import KINDS, client_changes

    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    kind = request.args.get("kind")
    if kind not in KINDS or kind == "powers":
        kind = None
    page = max(request.args.get("page", type=int) or 1, 1)
    rows, total = client_changes(tenant, kind=kind, limit=CHANGES_PER_PAGE, offset=(page - 1) * CHANGES_PER_PAGE)
    return render_template("tenants/changes.html", tenant=tenant, rows=rows, total=total, kind=kind,
                           kinds={k: v for k, v in KINDS.items() if k != "powers"}, page=page,
                           pages=max(1, -(-total // CHANGES_PER_PAGE)))
