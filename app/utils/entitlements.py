"""Who may use what (packages and powers, stage 1).

Five layers decide whether a module or setting is usable, each able only
to narrow the one before it:

  1. the client's package (Ultra Admin) - each module Off, Included or
     Client's choice; each setting changeable by the client or ARFA only;
  2. the Ultra Admin's exceptions for that one client;
  3. the client's own switches - only for Client's-choice modules;
  4. a branch's narrowing - modules the Client Admin (or the Ultra
     Admin, per login) hid at one counter (stage 5);
  5. a login's own powers - its role and counter permissions, capped by
     the package (app/utils/permissions.py, stage 5).

`module_mode` is the one answer every check goes through:
Tenant.has_feature asks it, so menus, search, route guards and saves all
follow it. A client that was never given a package behaves exactly as
before packages existed - every module their own choice, every setting
theirs - and every client that existed when this shipped was put on the
"Current setup" package, which says the same.

Auditor powers follow the same idea: `auditor_can` answers from the
auditor's own AuditorPowers row, else from what their role could do
before powers existed.
"""

from app.extensions import db
from app.utils.audit import record_audit

# --- modules --------------------------------------------------------------------------------------

OFF = "off"            # not in the package: hidden, its pages 404
INCLUDED = "inc"       # always on; the client can't switch it off
CHOICE = "choice"      # paid for; the client switches it on or off
MODULE_STATES = {OFF: "Off", INCLUDED: "Included", CHOICE: "Client's choice"}

# What a module is for this client right now.
ON = "on"
READ_ONLY = "read_only"   # taken away while it held data: old records readable, nothing new

# --- settings ---------------------------------------------------------------------------------------

CLIENT = "client"      # the client changes it in Settings
ARFA = "arfa"          # only the Ultra Admin changes it, from the console
SETTING_STATES = {CLIENT: "Client can change", ARFA: "Only ARFA"}

SETTINGS = {
    "business_type": "Type of business, and switching modules to its set",
    "aato": "Turnover (AATO), which sets the HSN digits on invoices",
    "pricing": "Whether new items' prices include GST",
    "branding": "Logo, colours and font",
    "counter": "Payment modes and UPI ID at the counter",
    "receipt": "Receipt print size",
    "margin_declaration": "Margin-scheme declaration wording",
    "security": "Idle sign-out and counter lock",
    "gst_portal": "e-Invoice and e-Way Bill logins",
}

# --- limits -----------------------------------------------------------------------------------------

LIMITS = {
    "branches": "Branches",
    "logins": "Logins (owner, branches and cashier PINs)",
    "gstins": "GSTINs",
}

# --- sign-in minimums (stage 5) ---------------------------------------------------------------------
# The client may make each stricter than this, never looser.

SECURITY_MINIMUMS = {
    "idle_minutes": "Idle sign-out - at most this many minutes",
    "lock_minutes": "Counter lock with cashier PINs - at most this many minutes",
    "owner_two_step": "Two-step sign-in for the owner's login",
}
IDLE_MINUTE_CHOICES = (15, 30, 60, 120, 240, 480)   # as Settings > Sign-in & security offers
LOCK_MINUTE_CHOICES = (2, 5, 10, 15, 30)


def security_from_form(form, prefix: str = "sec_") -> dict:
    """Sign-in minimums from a posted form (package editor): blank = none."""
    out = {}
    for key, choices in (("idle_minutes", IDLE_MINUTE_CHOICES), ("lock_minutes", LOCK_MINUTE_CHOICES)):
        raw = form.get(f"{prefix}{key}", "")
        if raw.isdigit() and int(raw) in choices:
            out[key] = int(raw)
    if form.get(f"{prefix}owner_two_step") == "y":
        out["owner_two_step"] = True
    return out

# --- auditor powers ---------------------------------------------------------------------------------

AUDITOR_POWERS = {
    "onboard_clients": "Onboard new clients",
    "assign_packages": "Choose a package for their clients",
    "switch_client_modules": "Switch a client's Client's-choice modules",
    "pause_clients": "Pause and resume clients",
    "edit_client_details": "Add a client's business locations and logo",
    "act_as": "Act as a client or branch login",
    "reset_logins": "Reset, block and unblock a client's logins (password, two-step)",
    "month_end": "Month-end pack, Tally export and filing tracker",
    "create_sub_auditors": "Create Sub-Auditors and hand them clients",
}

# What each role could do before powers existed - every power an existing
# auditor keeps until the Ultra Admin changes it. (Choosing packages is
# new, so nobody has it until it's given.)
ROLE_DEFAULT_POWERS = {
    "auditor": {
        "onboard_clients": True, "assign_packages": False, "switch_client_modules": True, "pause_clients": True,
        "edit_client_details": True, "act_as": True, "reset_logins": True, "month_end": True,
        "create_sub_auditors": True,
    },
    "sub_auditor": {
        "onboard_clients": True, "assign_packages": False, "switch_client_modules": True, "pause_clients": True,
        "edit_client_details": True, "act_as": True, "reset_logins": True, "month_end": True,
        "create_sub_auditors": False,
    },
}


# --- reading ----------------------------------------------------------------------------------------

def grant_of(tenant):
    """The client's TenantPackage row, or None if they were never given one."""
    return getattr(tenant, "package_grant", None) if tenant is not None else None


def package_module_state(package, key: str) -> str:
    state = (package.modules or {}).get(key)
    return state if state in MODULE_STATES else (package.unlisted_modules or OFF)


def module_state(tenant, key: str) -> str:
    """Off / Included / Client's choice for this client: their exception
    if the Ultra Admin made one, else their package. Never given a package:
    Client's choice, as before packages existed."""
    grant = grant_of(tenant)
    if grant is None:
        return CHOICE
    override = (grant.module_overrides or {}).get(key)
    if override in MODULE_STATES:
        return override
    return package_module_state(grant.package, key)


def module_mode(tenant, key: str) -> str:
    """Whether the module is on, off or read-only for this client now."""
    state = module_state(tenant, key)
    if state == INCLUDED:
        return ON
    if state == CHOICE:
        return ON if (tenant.features or {}).get(key) else OFF
    grant = grant_of(tenant)
    if grant is not None and key in (grant.read_only_modules or []):
        return READ_ONLY
    return OFF


def client_may_switch(tenant, key: str) -> bool:
    """May the client themselves switch this module on and off?"""
    return module_state(tenant, key) == CHOICE


def setting_state(tenant, key: str) -> str:
    if key not in SETTINGS:
        raise ValueError(f"Unknown setting: {key}")
    grant = grant_of(tenant)
    if grant is None:
        return CLIENT
    override = (grant.setting_overrides or {}).get(key)
    if override in SETTING_STATES:
        return override
    state = (grant.package.settings or {}).get(key)
    return state if state in SETTING_STATES else CLIENT


def setting_locked(tenant, key: str) -> bool:
    """True when only ARFA may change this setting for this client."""
    return setting_state(tenant, key) == ARFA


def limit_of(tenant, key: str):
    """The client's limit (an int), or None for no limit."""
    if key not in LIMITS:
        raise ValueError(f"Unknown limit: {key}")
    grant = grant_of(tenant)
    if grant is None:
        return None
    overrides = grant.limit_overrides or {}
    value = overrides[key] if key in overrides else (grant.package.limits or {}).get(key)
    return int(value) if value is not None else None


def own_branding(tenant) -> bool:
    """May the client use their own logo, colours and sign-in link?"""
    grant = grant_of(tenant)
    if grant is None:
        return True
    if grant.own_branding_override is not None:
        return grant.own_branding_override
    return bool(grant.package.own_branding)


def auditor_can(user, power: str) -> bool:
    """May this console login use this power? The Ultra Admin always may;
    client and branch logins never do."""
    if power not in AUDITOR_POWERS:
        raise ValueError(f"Unknown power: {power}")
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_super_admin:
        return True
    role = "auditor" if user.is_auditor else "sub_auditor" if user.is_sub_auditor else None
    if role is None:
        return False
    grant = getattr(user, "power_grant", None)
    if grant is not None and power in (grant.powers or {}):
        allowed = bool(grant.powers[power])
    else:
        allowed = ROLE_DEFAULT_POWERS[role][power]
    # A Sub-Auditor never holds a power their Auditor doesn't.
    parent = getattr(user, "parent_auditor", None) if role == "sub_auditor" else None
    if allowed and parent is not None:
        allowed = auditor_can(parent, power)
    return allowed


def power_required(power: str):
    """Route guard for the console: 403 unless the signed-in login holds
    this power (the Ultra Admin always does)."""
    from functools import wraps

    from flask import abort
    from flask_login import current_user

    if power not in AUDITOR_POWERS:
        raise ValueError(f"Unknown power: {power}")

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not auditor_can(current_user, power):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def grantable_powers(actor) -> list[str]:
    """Powers `actor` may hand out: the Ultra Admin any; an Auditor only
    what they hold themselves, and never creating Sub-Auditors (a
    Sub-Auditor can't have their own)."""
    if actor.is_super_admin:
        return list(AUDITOR_POWERS)
    if actor.is_auditor:
        return [p for p in AUDITOR_POWERS if p != "create_sub_auditors" and auditor_can(actor, p)]
    return []


def allowed_packages(user) -> list:
    """Packages this login may give a client: every available one for the
    Ultra Admin; for an auditor, the ones the Ultra Admin (or their
    Auditor) allowed them - only with the "choose a package" power."""
    from app.models.package import Package

    query = Package.query.filter_by(is_active=True).order_by(Package.sort_order, Package.name)
    if user.is_super_admin:
        return query.all()
    if not auditor_can(user, "assign_packages"):
        return []
    grant = getattr(user, "power_grant", None)
    ids = set(grant.allowed_package_ids or []) if grant is not None else set()
    # A Sub-Auditor never keeps a package their Auditor no longer has.
    parent = getattr(user, "parent_auditor", None) if user.is_sub_auditor else None
    if parent is not None:
        ids &= {p.id for p in allowed_packages(parent)}
    return [p for p in query.all() if p.id in ids]


def client_room(user):
    """(clients now, limit or None) for an auditor's client limit."""
    from app.utils.auditor_scope import auditor_tenant_ids

    if user.is_super_admin:
        return 0, None
    grant = getattr(user, "power_grant", None)
    limit = grant.max_clients if grant is not None else None
    return len(auditor_tenant_ids(user) or ()), limit


def sub_auditor_room(user):
    """(Sub-Auditors now, limit or None) for an Auditor."""
    from app.models.user import User

    if user.is_super_admin:
        return 0, None
    grant = getattr(user, "power_grant", None)
    limit = grant.max_sub_auditors if grant is not None else None
    return User.query.filter_by(parent_auditor_id=user.id).count(), limit


def has_room(room) -> bool:
    count, limit = room
    return limit is None or count < limit


# --- changing (each change audit-logged with who made it) ------------------------------------------

def _ensure_grant(tenant, actor):
    from app.models.package import TenantPackage

    grant = grant_of(tenant)
    if grant is None:
        grant = TenantPackage(tenant_id=tenant.id, package=current_setup_package(), assigned_by_id=_id(actor))
        db.session.add(grant)
        tenant.package_grant = grant
    return grant


def _id(actor):
    return getattr(actor, "id", None)


def assign_package(tenant, package, actor, keep_exceptions: bool = False):
    """Put the client on `package`. Their exceptions are cleared unless
    asked to keep them."""
    from app.models.package import TenantPackage

    grant = grant_of(tenant)
    before = grant.package.name if grant is not None else None
    if grant is None:
        grant = TenantPackage(tenant_id=tenant.id, package=package)
        db.session.add(grant)
        tenant.package_grant = grant
    grant.package = package
    grant.assigned_by_id = _id(actor)
    if not keep_exceptions:
        grant.module_overrides = {}
        grant.setting_overrides = {}
        grant.limit_overrides = {}
        grant.own_branding_override = None
        grant.counter_permissions_override = None
        grant.security_overrides = {}
    refresh_read_only(tenant)
    record_audit(actor, "package_assigned", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
                 details={"from": before, "to": package.name, "kept_exceptions": keep_exceptions})
    return grant


def set_module_exception(tenant, key: str, state, actor):
    """One module for this client only: "off" / "inc" / "choice", or None
    to go back to what the package says."""
    from app.utils.features import FEATURES

    if key not in FEATURES:
        raise ValueError(f"Unknown module: {key}")
    if state is not None and state not in MODULE_STATES:
        raise ValueError(f"Unknown module state: {state}")
    grant = _ensure_grant(tenant, actor)
    overrides = dict(grant.module_overrides or {})
    before = overrides.get(key)
    if state is None or state == package_module_state(grant.package, key):
        overrides.pop(key, None)
    else:
        overrides[key] = state
    grant.module_overrides = overrides
    refresh_read_only(tenant)
    record_audit(actor, "module_exception_set", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
                 details={"module": key, "from": before, "to": overrides.get(key)})
    return grant


def set_setting_exception(tenant, key: str, state, actor):
    """One setting for this client only: "client" / "arfa", or None for the package's."""
    if key not in SETTINGS:
        raise ValueError(f"Unknown setting: {key}")
    if state is not None and state not in SETTING_STATES:
        raise ValueError(f"Unknown setting state: {state}")
    grant = _ensure_grant(tenant, actor)
    overrides = dict(grant.setting_overrides or {})
    before = overrides.get(key)
    if state is None:
        overrides.pop(key, None)
    else:
        overrides[key] = state
    grant.setting_overrides = overrides
    record_audit(actor, "setting_exception_set", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
                 details={"setting": key, "from": before, "to": overrides.get(key)})
    return grant


AS_PACKAGE = "package"   # "no exception - use what the package says"


def set_limit_exception(tenant, key: str, value, actor):
    """One limit for this client only: a number, None for no limit, or
    AS_PACKAGE to go back to the package's."""
    if key not in LIMITS:
        raise ValueError(f"Unknown limit: {key}")
    if value is not None and value != AS_PACKAGE and (not isinstance(value, int) or value < 0):
        raise ValueError("A limit is a whole number of 0 or more.")
    grant = _ensure_grant(tenant, actor)
    overrides = dict(grant.limit_overrides or {})
    before = overrides.get(key, AS_PACKAGE)
    if value == AS_PACKAGE:
        overrides.pop(key, None)
    else:
        overrides[key] = value
    grant.limit_overrides = overrides
    record_audit(actor, "limit_exception_set", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
                 details={"limit": key, "from": before, "to": overrides.get(key, AS_PACKAGE)})
    return grant


def set_branding_exception(tenant, allowed, actor):
    """Own branding for this client only: True, False, or None for the package's."""
    grant = _ensure_grant(tenant, actor)
    before = grant.own_branding_override
    grant.own_branding_override = allowed
    record_audit(actor, "branding_exception_set", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
                 details={"from": before, "to": allowed})
    return grant


def set_auditor_powers(user, powers: dict, actor, allowed_package_ids=None, max_clients=None,
                       max_sub_auditors=None):
    """Set an auditor's powers (only known keys are kept)."""
    from app.models.package import AuditorPowers

    unknown = set(powers) - set(AUDITOR_POWERS)
    if unknown:
        raise ValueError(f"Unknown powers: {sorted(unknown)}")
    grant = getattr(user, "power_grant", None)
    before = dict(grant.powers or {}) if grant is not None else {}
    if grant is None:
        grant = AuditorPowers(user_id=user.id)
        db.session.add(grant)
        user.power_grant = grant
    grant.powers = {k: bool(v) for k, v in powers.items()}
    grant.allowed_package_ids = list(allowed_package_ids or [])
    grant.max_clients = max_clients
    grant.max_sub_auditors = max_sub_auditors
    grant.set_by_id = _id(actor)
    record_audit(actor, "auditor_powers_set", entity_type="user", entity_id=user.id,
                 details={"before": before, "after": grant.powers})
    return grant


# --- read-only, locks, requests (what the client sees; stage 3) ------------------------------------

def refresh_read_only(tenant):
    """Re-work out which modules are read-only for this client: every
    module their package (or an exception) switched Off while they still
    hold records in it. Called after any change to their package."""
    from app.utils.features import AVAILABLE_FEATURES
    from app.utils.module_data import has_records

    grant = grant_of(tenant)
    if grant is None:
        return []
    keys = sorted(f.key for f in AVAILABLE_FEATURES
                  if module_state(tenant, f.key) == OFF and has_records(tenant, f.key))
    if keys != list(grant.read_only_modules or []):
        grant.read_only_modules = keys
    return keys


def ultra_admin_acting() -> bool:
    """Is the Ultra Admin signed in as this client ("Act as")? Then the
    package's locks stand aside, so the firm can change a locked setting
    for the client on their own screens."""
    from flask import has_request_context, session

    if not has_request_context() or not session.get("impersonator_id"):
        return False
    from app.models.user import User

    actor = db.session.get(User, session["impersonator_id"])
    return bool(actor and actor.is_super_admin)


def acting_console_user():
    """The console login acting as this client ("Act as"), if any."""
    from flask import has_request_context, session

    if not has_request_context() or not session.get("impersonator_id"):
        return None
    from app.models.user import User

    return db.session.get(User, session["impersonator_id"])


def locked_for_client(tenant, key: str) -> bool:
    """Is this setting locked for whoever is signed in as the client now?
    Their look is also locked when their package uses ARFA's look."""
    if ultra_admin_acting():
        return False
    return setting_locked(tenant, key) or (key == "branding" and not own_branding(tenant))


def request_module(tenant, key: str, user):
    """The client asks ARFA to add a module. Returns the open request
    (an existing one if they already asked)."""
    from app.models.package import ModuleRequest
    from app.utils.features import FEATURES

    if key not in FEATURES:
        raise ValueError(f"Unknown module: {key}")
    open_request = ModuleRequest.query.filter_by(tenant_id=tenant.id, feature_key=key,
                                                 status=ModuleRequest.PENDING).first()
    if open_request is not None:
        return open_request
    req = ModuleRequest(tenant_id=tenant.id, feature_key=key, requested_by_id=_id(user))
    db.session.add(req)
    record_audit(user, "module_requested", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
                 details={"module": key})
    return req


def resolve_module_request(req, action: str, actor):
    """"choice" or "inc" adds the module for this client (as an exception
    to their package); "decline" closes the request."""
    from datetime import datetime, timezone

    from app.models.package import ModuleRequest

    if action not in (CHOICE, INCLUDED, "decline"):
        raise ValueError(f"Unknown action: {action}")
    if action != "decline":
        set_module_exception(req.tenant, req.feature_key, action, actor)
        if action == CHOICE:
            # They asked for it, so it starts switched on.
            req.tenant.set_feature(req.feature_key, True)
    req.status = ModuleRequest.DECLINED if action == "decline" else ModuleRequest.ADDED
    req.resolved_by_id = _id(actor)
    req.resolved_at = datetime.now(timezone.utc)
    record_audit(actor, "module_request_resolved", tenant_id=req.tenant_id, entity_type="tenant",
                 entity_id=req.tenant_id, details={"module": req.feature_key, "action": action})
    return req


# --- previews and summaries (the console) ------------------------------------------------------------

def preview_package(tenant, package, keep_exceptions: bool = False) -> list[dict]:
    """What changes for this client if they move to `package`: each module
    whose on/off would differ, given the client's own switches. Their
    records are never touched either way."""
    from app.utils.features import AVAILABLE_FEATURES

    grant = grant_of(tenant)
    overrides = dict(grant.module_overrides or {}) if (grant is not None and keep_exceptions) else {}
    changes = []
    for feature in AVAILABLE_FEATURES:
        key = feature.key
        state = overrides.get(key) if overrides.get(key) in MODULE_STATES else package_module_state(package, key)
        after = state == INCLUDED or (state == CHOICE and bool((tenant.features or {}).get(key)))
        before = tenant.has_feature(key)
        if before != after:
            changes.append({"key": key, "label": feature.label, "before": before, "after": after, "state": state})
    return changes


def package_summary(package) -> dict:
    """How many modules are Included / Client's choice / Off in a package."""
    from app.utils.features import AVAILABLE_FEATURES

    counts = {INCLUDED: 0, CHOICE: 0, OFF: 0}
    for feature in AVAILABLE_FEATURES:
        counts[package_module_state(package, feature.key)] += 1
    return counts


def package_price_for(package, billing_cycle):
    """The package's price for a client's billing cycle (monthly unless
    they're on a yearly one), or None if it has none."""
    cycle = getattr(billing_cycle, "value", billing_cycle)
    return package.price_yearly if cycle == "yearly" else package.price_monthly


# Which starting package a new client of each type is offered first.
BUSINESS_TYPE_PACKAGE = {
    "retail": "retail", "wholesale": "wholesale", "restaurant": "restaurant", "mobile_electronics": "electronics",
    "pharmacy": "pharmacy", "apparel": "retail", "hardware": "wholesale", "jewellery": "retail", "bakery": "retail",
    "services": "billing", "other": "current",
}


# --- the packages ARFA ships -------------------------------------------------------------------------

CURRENT_SETUP = "current"

_BILLING_INC = ["credit_sales", "favourites"]
_RETAIL_INC = _BILLING_INC + ["inventory", "purchases", "expenses", "business_reports", "daily_summary"]
_COUNTER_CHOICE = ["categories", "day_close", "cashier_pin", "offline_pos"]
_ARFA_SETS_BUSINESS = {"business_type": ARFA, "aato": ARFA}


def _modules(included=(), choice=()):
    out = {key: INCLUDED for key in included}
    out.update({key: CHOICE for key in choice if key not in out})
    return out


# Built from the business-type presets (app/utils/features.py), so a new
# client on one of these starts much as they would today. Prices are left
# for the Ultra Admin to set.
STARTING_PACKAGES = [
    {"key": CURRENT_SETUP, "name": "Current setup", "sort_order": 0, "unlisted_modules": CHOICE, "modules": {},
     "settings": {}, "limits": {}, "own_branding": True,
     "description": "Everything as it was before packages: every module the client's own choice, every setting "
                    "theirs, no limits. Every client that existed when packages arrived starts here."},
    {"key": "billing", "name": "Billing", "sort_order": 10,
     "modules": _modules(_BILLING_INC, ["categories", "day_close", "cashier_pin"]),
     "settings": {**_ARFA_SETS_BUSINESS, "branding": ARFA}, "limits": {"branches": 1, "logins": 2, "gstins": 1},
     "own_branding": False, "description": "The counter and invoices only."},
    {"key": "retail", "name": "Retail", "sort_order": 20,
     "modules": _modules(_RETAIL_INC, _COUNTER_CHOICE + ["loyalty", "multi_price_list", "variants"]),
     "settings": dict(_ARFA_SETS_BUSINESS), "limits": {"branches": 3, "logins": 6, "gstins": 1},
     "own_branding": True, "description": "Billing plus stock, buying, expenses and the dashboard."},
    {"key": "restaurant", "name": "Restaurant", "sort_order": 30,
     "modules": _modules(_BILLING_INC + ["restaurant_mode", "categories", "business_reports", "daily_summary"],
                         ["expenses", "inventory", "purchases", "day_close", "cashier_pin", "offline_pos"]),
     "settings": dict(_ARFA_SETS_BUSINESS), "limits": {"branches": 3, "logins": 8, "gstins": 1},
     "own_branding": True, "description": "Billing plus restaurant tables, kitchen tickets and the dashboard."},
    {"key": "pharmacy", "name": "Pharmacy", "sort_order": 40,
     "modules": _modules(_RETAIL_INC + ["batch_expiry", "pharmacy"], _COUNTER_CHOICE + ["receivables"]),
     "settings": dict(_ARFA_SETS_BUSINESS), "limits": {"branches": 3, "logins": 6, "gstins": 1},
     "own_branding": True, "description": "Retail plus batch and expiry, and the scheduled-drug register."},
    {"key": "electronics", "name": "Mobiles & Electronics", "sort_order": 50,
     "modules": _modules(_RETAIL_INC + ["serial_imei", "margin_scheme", "job_cards", "electronics"],
                         _COUNTER_CHOICE + ["receivables", "loyalty"]),
     "settings": dict(_ARFA_SETS_BUSINESS), "limits": {"branches": 3, "logins": 6, "gstins": 1},
     "own_branding": True, "description": "Retail plus serial / IMEI numbers, the margin scheme and repairs."},
    {"key": "wholesale", "name": "Wholesale", "sort_order": 60,
     "modules": _modules(_RETAIL_INC + ["receivables", "payables", "sales_documents", "multi_price_list", "eway_bill",
                                        "wholesale"],
                         _COUNTER_CHOICE + ["einvoice", "cash_bank_book", "recurring_invoices"]),
     "settings": dict(_ARFA_SETS_BUSINESS), "limits": {"branches": 5, "logins": 12, "gstins": 3},
     "own_branding": True, "description": "Retail plus customer and supplier accounts, quotes and orders, price "
                                          "lists, e-Way bills and salesmen."},
    {"key": "everything", "name": "Everything", "sort_order": 70, "unlisted_modules": CHOICE, "modules": {},
     "settings": dict(_ARFA_SETS_BUSINESS), "limits": {}, "own_branding": True,
     "description": "Every module as the client's own choice, no limits."},
]


def ensure_starting_packages():
    """Create any of ARFA's starting packages that don't exist yet (by key).
    Never changes one that does - the Ultra Admin may have edited it."""
    from app.models.package import Package

    existing = {p.key for p in Package.query.filter(Package.key.isnot(None)).all()}
    for spec in STARTING_PACKAGES:
        if spec["key"] in existing:
            continue
        db.session.add(Package(**{"unlisted_modules": OFF, **spec}))
    db.session.flush()


def current_setup_package():
    from app.models.package import Package

    package = Package.query.filter_by(key=CURRENT_SETUP).first()
    if package is None:
        ensure_starting_packages()
        package = Package.query.filter_by(key=CURRENT_SETUP).first()
    return package


# --- branches, logins, limits, style and sign-in minimums (stage 5) -------------------------------------

# Modules one branch can be left without. Each opens its own pages or adds
# its own panel at the counter, and a counter without it still bills
# correctly - the business keeps the module, its stock rules and GST.
BRANCH_MODULES = ("restaurant_mode", "job_cards", "loyalty", "inventory", "wholesale")


def login_grant_of(user):
    return getattr(user, "login_grant", None) if user is not None else None


def hidden_by_arfa(user, key: str) -> bool:
    grant = login_grant_of(user)
    return bool(grant is not None and key in (grant.hidden_modules or []))


def login_hides(user, key: str) -> bool:
    """Is this module hidden for this one branch login (by the Client Admin
    or by the Ultra Admin)? Only branch logins are ever narrowed."""
    if not getattr(user, "is_staff", False) or key not in BRANCH_MODULES:
        return False
    return key in (user.hidden_modules or []) or hidden_by_arfa(user, key)


def counter_permissions_allowed(tenant) -> set:
    """Counter permissions this client may hand their branch logins - all of
    them unless their package (or an exception) narrows it."""
    from app.utils.permissions import PERMISSION_COLUMNS

    grant = grant_of(tenant)
    if grant is None:
        return set(PERMISSION_COLUMNS)
    chosen = grant.counter_permissions_override
    if chosen is None:
        chosen = grant.package.counter_permissions
    return set(PERMISSION_COLUMNS) if chosen is None else set(chosen) & set(PERMISSION_COLUMNS)


def arfa_permission(user, permission: str):
    """True / False when the Ultra Admin set this permission for this login,
    else None (the Client Admin decides)."""
    grant = login_grant_of(user)
    if grant is None:
        return None
    value = (grant.permissions or {}).get(permission)
    return None if value is None else bool(value)


def branch_permission(user, permission: str) -> bool:
    """May this branch login do this? The Ultra Admin's switch if set, else
    what the Client Admin gave it - but only if the package allows it."""
    from app.utils.permissions import PERMISSION_COLUMNS

    forced = arfa_permission(user, permission)
    if forced is not None:
        return forced
    return (bool(getattr(user, PERMISSION_COLUMNS[permission]))
            and permission in counter_permissions_allowed(user.tenant))


def branch_max_discount(user):
    from decimal import Decimal

    if not branch_permission(user, "discount"):
        return Decimal("0")
    own = Decimal(user.max_discount_percent or 0)
    grant = login_grant_of(user)
    if grant is not None and grant.max_discount_percent is not None:
        cap = Decimal(grant.max_discount_percent)
        return cap if arfa_permission(user, "discount") else min(own, cap)
    return own


def set_login_switches(user, actor, hidden_modules=(), permissions=None, max_discount_percent=None, role=None):
    """The Ultra Admin's switches for one branch login. Empty everything to
    hand the login back to the Client Admin."""
    from app.models.package import LoginGrant
    from app.utils.permissions import BRANCH_ROLES, PERMISSION_COLUMNS

    if not user.is_staff:
        raise ValueError("Only branch logins have their own switches.")
    hidden = sorted(k for k in hidden_modules if k in BRANCH_MODULES)
    perms = {k: bool(v) for k, v in (permissions or {}).items() if k in PERMISSION_COLUMNS and v is not None}
    grant = login_grant_of(user)
    before = ({"hidden": grant.hidden_modules, "permissions": grant.permissions,
               "max_discount": str(grant.max_discount_percent) if grant.max_discount_percent is not None else None}
              if grant is not None else None)
    if grant is None:
        grant = LoginGrant(tenant_id=user.tenant_id, user_id=user.id)
        db.session.add(grant)
        user.login_grant = grant
    grant.hidden_modules = hidden
    grant.permissions = perms
    grant.max_discount_percent = max_discount_percent
    grant.role = role if role in BRANCH_ROLES else None
    grant.set_by_id = _id(actor)
    record_audit(actor, "login_switches_set", tenant_id=user.tenant_id, entity_type="user", entity_id=user.id,
                 details={"before": before, "after": {"hidden": hidden, "permissions": perms,
                                                      "max_discount": str(max_discount_percent)
                                                      if max_discount_percent is not None else None}})
    return grant


def set_counter_permissions_exception(tenant, permissions, actor):
    """The counter permissions this client may give, for this client only -
    a list of keys, or None to go back to the package's."""
    from app.utils.permissions import PERMISSION_COLUMNS

    grant = _ensure_grant(tenant, actor)
    before = grant.counter_permissions_override
    grant.counter_permissions_override = (None if permissions is None
                                          else [k for k in PERMISSION_COLUMNS if k in set(permissions)])
    record_audit(actor, "counter_permissions_exception_set", tenant_id=tenant.id, entity_type="tenant",
                 entity_id=tenant.id, details={"from": before, "to": grant.counter_permissions_override})
    return grant


# Limits: how many there are now. A login is anyone who signs in - the
# owner's login, each branch login, and each cashier with a PIN.

def limit_usage(tenant, key: str) -> int:
    from app.models.platform import Cashier
    from app.models.user import User, UserRole

    if key not in LIMITS:
        raise ValueError(f"Unknown limit: {key}")
    if key == "branches":
        return User.query.filter_by(tenant_id=tenant.id, role=UserRole.STAFF, is_active=True).count()
    if key == "logins":
        return (User.query.filter_by(tenant_id=tenant.id, is_active=True).count()
                + Cashier.query.filter_by(tenant_id=tenant.id, is_active=True).count())
    return tenant.gstins.filter_by(is_active=True).count()


def limit_meter(tenant, key: str) -> dict:
    """For a "3 of 5 used" meter where branches, logins and GSTINs are added."""
    used, limit = limit_usage(tenant, key), limit_of(tenant, key)
    return {"key": key, "label": LIMITS[key], "used": used, "limit": limit,
            "full": limit is not None and used >= limit, "over": limit is not None and used > limit,
            "percent": min(100, round(used * 100 / limit)) if limit else 0}


def may_add(tenant, key: str, actor=None, adding: int = 1) -> bool:
    """Room for `adding` more under this limit? The Ultra Admin (directly or
    acting as the client) may always go past it."""
    if getattr(actor, "is_super_admin", False) or ultra_admin_acting():
        return True
    limit = limit_of(tenant, key)
    return limit is None or limit_usage(tenant, key) + adding <= limit


def limit_message(tenant, key: str) -> str:
    return (f"Your package allows {limit_of(tenant, key)} {LIMITS[key].split(' (')[0].lower()} - all in use. "
            "Ask ARFA to raise it.")


# Sign-in minimums.

def security_minimum(tenant, key: str):
    if key not in SECURITY_MINIMUMS:
        raise ValueError(f"Unknown sign-in minimum: {key}")
    grant = grant_of(tenant)
    if grant is None:
        return None
    overrides = grant.security_overrides or {}
    value = overrides[key] if key in overrides else (grant.package.security or {}).get(key)
    if key == "owner_two_step":
        return bool(value)
    return int(value) if value else None


def strictest_minutes(chosen: int, most) -> int:
    """The client's choice, but never longer than ARFA's most (0 = never)."""
    if not most:
        return chosen
    return chosen if chosen and chosen <= most else most


def set_security_exception(tenant, key: str, value, actor):
    """One sign-in minimum for this client only: a number (minutes) or
    True / False, None for no minimum, AS_PACKAGE for the package's."""
    if key not in SECURITY_MINIMUMS:
        raise ValueError(f"Unknown sign-in minimum: {key}")
    grant = _ensure_grant(tenant, actor)
    overrides = dict(grant.security_overrides or {})
    before = overrides.get(key, AS_PACKAGE)
    if value == AS_PACKAGE:
        overrides.pop(key, None)
    else:
        overrides[key] = value
    grant.security_overrides = overrides
    record_audit(actor, "security_exception_set", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
                 details={"minimum": key, "from": before, "to": overrides.get(key, AS_PACKAGE)})
    return grant


# Branding.

def brand_logo_id(tenant):
    """The client's logo where their own branding shows (screens, printed
    documents), or None when their package uses ARFA's look."""
    return tenant.logo_image_id if tenant is not None and own_branding(tenant) else None


def branch_login_context(user) -> dict:
    """What a branch login's permissions page (Client Admin) and its
    switches in the console (Ultra Admin) show: each permission with what
    the Client Admin gave, whether the package allows it, what ARFA set and
    the result; and the modules at this counter."""
    from app.utils.features import FEATURES
    from app.utils.permissions import BRANCH_ROLES, PERMISSION_COLUMNS, PERMISSION_LABELS

    tenant = user.tenant
    allowed = counter_permissions_allowed(tenant)
    grant = login_grant_of(user)
    rows = [{"key": key, "label": PERMISSION_LABELS[key], "column": column,
             "own": bool(getattr(user, column)), "allowed": key in allowed,
             "arfa": arfa_permission(user, key), "on": branch_permission(user, key)}
            for key, column in PERMISSION_COLUMNS.items()]
    modules = [{"key": key, "label": FEATURES[key].label, "business_on": tenant.has_feature(key),
                "hidden_by_client": key in (user.hidden_modules or []), "hidden_by_arfa": hidden_by_arfa(user, key)}
               for key in BRANCH_MODULES]
    return {
        "rows": rows, "modules": modules, "roles": BRANCH_ROLES, "role": user.branch_role,
        "allowed": allowed, "grant": grant, "max_discount": branch_max_discount(user),
        "arfa_discount_cap": grant.max_discount_percent if grant is not None else None,
        "arfa_role": grant.role if grant is not None else None,
        "any_arfa": bool(grant is not None and (grant.hidden_modules or grant.permissions
                                                or grant.max_discount_percent is not None)),
    }
