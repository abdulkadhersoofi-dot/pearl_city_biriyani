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

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.package import Package, TenantPackage
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.settings.sections import MODULE_GROUPS
from app.tenants import tenants_bp
from app.utils.audit import record_audit
from app.utils.entitlements import (
    AS_PACKAGE, CHOICE, LIMITS, MODULE_STATES, OFF, SETTING_STATES, SETTINGS, assign_package, ensure_starting_packages,
    grant_of, module_state, own_branding, package_module_state, package_price_for, package_summary, preview_package,
    set_branding_exception, set_limit_exception, set_module_exception, set_setting_exception, setting_state,
)
from app.utils.features import AVAILABLE_FEATURES, FEATURES


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
    return errors


@tenants_bp.route("/packages/new", methods=["GET", "POST"])
@roles_required(UserRole.SUPER_ADMIN)
def package_new():
    source = Package.query.get(request.args.get("copy", type=int) or 0)
    package = Package(name="", modules={}, settings={}, limits={}, unlisted_modules=OFF, own_branding=True)
    if source is not None and request.method == "GET":
        package.name = f"{source.name} (copy)"
        package.description = source.description
        package.modules = {f.key: package_module_state(source, f.key) for f in AVAILABLE_FEATURES}
        package.settings = dict(source.settings or {})
        package.limits = dict(source.limits or {})
        package.own_branding = source.own_branding
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
                           is_new=True)


@tenants_bp.route("/packages/<int:package_id>", methods=["GET", "POST"])
@roles_required(UserRole.SUPER_ADMIN)
def package_edit(package_id):
    package = Package.query.get_or_404(package_id)
    if request.method == "POST":
        before = {"modules": dict(package.modules or {}), "settings": dict(package.settings or {}),
                  "limits": dict(package.limits or {}), "own_branding": package.own_branding,
                  "price_monthly": str(package.price_monthly), "price_yearly": str(package.price_yearly)}
        errors = _package_from_form(package)
        if errors:
            db.session.rollback()
            for error in errors:
                flash(error, "error")
            package = Package.query.get_or_404(package_id)
        else:
            record_audit(current_user, "package_edited", entity_type="package", entity_id=package.id,
                         details={"name": package.name, "before": before,
                                  "after": {"modules": package.modules, "settings": package.settings,
                                            "limits": package.limits, "own_branding": package.own_branding,
                                            "price_monthly": str(package.price_monthly),
                                            "price_yearly": str(package.price_yearly)}})
            db.session.commit()
            flash(f"Package {package.name} saved.", "success")
            return redirect(url_for("tenants.package_edit", package_id=package.id))
    return render_template("tenants/package_form.html", package=package, groups=module_rows(),
                           clients=_clients_using(package.id), settings=SETTINGS, limits=LIMITS,
                           states=MODULE_STATES, state_of=package_module_state, is_new=False)


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

def client_package_context(tenant):
    """What the client page's "Package & modules" section shows."""
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
    return {
        "grant": grant, "current_package": current,
        "package_choices": Package.query.filter_by(is_active=True).order_by(Package.sort_order, Package.name).all(),
        "module_rows": rows, "setting_rows": setting_rows, "limit_rows": limit_rows,
        "branding_exception": grant.own_branding_override if grant is not None else None,
        "branding_now": own_branding(tenant), "branding_package": current.own_branding if current else True,
        "exception_count": (len(grant.module_overrides or {}) + len(grant.setting_overrides or {})
                            + len(grant.limit_overrides or {}) + (grant.own_branding_override is not None))
        if grant is not None else 0,
        "module_states": MODULE_STATES, "setting_states": SETTING_STATES,
    }


@tenants_bp.route("/clients/<int:tenant_id>/package/preview")
@roles_required(UserRole.SUPER_ADMIN)
def package_preview(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    package = Package.query.get_or_404(request.args.get("package_id", type=int) or 0)
    keep = request.args.get("keep_exceptions") == "y"
    return render_template("tenants/_package_preview.html", tenant=tenant, package=package,
                           changes=preview_package(tenant, package, keep), summary=package_summary(package),
                           price=package_price_for(package, tenant.billing_cycle))


@tenants_bp.route("/clients/<int:tenant_id>/package", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def assign_client_package(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    package = Package.query.get(request.form.get("package_id", type=int) or 0)
    if package is None or not package.is_active:
        flash("Pick a package that's available.", "error")
        return redirect(url_for("tenants.detail", tenant_id=tenant.id, _anchor="sec-package"))
    keep = request.form.get("keep_exceptions") == "y"
    changes = preview_package(tenant, package, keep)
    assign_package(tenant, package, current_user, keep_exceptions=keep)
    message = f"{tenant.legal_name} is now on {package.name}"
    if changes:
        on = sum(1 for c in changes if c["after"])
        message += f" - {on} module{'s' if on != 1 else ''} on, {len(changes) - on} off"
    price = package_price_for(package, tenant.billing_cycle)
    if request.form.get("use_price") == "y" and price is not None:
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
    db.session.commit()
    flash(f"{changed} change{'s' if changed != 1 else ''} saved for {tenant.legal_name}." if changed
          else "Nothing changed.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id, _anchor="sec-package"))
