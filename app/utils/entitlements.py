"""Who may use what (packages and powers, stage 1).

Five layers decide whether a module or setting is usable, each able only
to narrow the one before it:

  1. the client's package (Ultra Admin) - each module Off, Included or
     Client's choice; each setting changeable by the client or ARFA only;
  2. the Ultra Admin's exceptions for that one client;
  3. the client's own switches - only for Client's-choice modules;
  4. a branch's narrowing (stage 5);
  5. a login's own powers (app/utils/permissions.py, stage 5).

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
    "logins": "Branch logins",
    "gstins": "GSTINs",
}

# --- auditor powers ---------------------------------------------------------------------------------

AUDITOR_POWERS = {
    "onboard_clients": "Onboard new clients",
    "assign_packages": "Choose a package for their clients",
    "switch_client_modules": "Switch a client's Client's-choice modules",
    "pause_clients": "Pause and resume clients",
    "act_as": "Act as a client or branch login",
    "reset_logins": "Reset passwords and two-step sign-in",
    "month_end": "Month-end pack, Tally export and filing tracker",
    "create_sub_auditors": "Create Sub-Auditors and hand them clients",
}

# What each role could do before powers existed - every power an existing
# auditor keeps until the Ultra Admin changes it. (Choosing packages is
# new, so nobody has it until it's given.)
ROLE_DEFAULT_POWERS = {
    "auditor": {
        "onboard_clients": True, "assign_packages": False, "switch_client_modules": True, "pause_clients": True,
        "act_as": True, "reset_logins": True, "month_end": True, "create_sub_auditors": True,
    },
    "sub_auditor": {
        "onboard_clients": True, "assign_packages": False, "switch_client_modules": True, "pause_clients": True,
        "act_as": True, "reset_logins": True, "month_end": True, "create_sub_auditors": False,
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
        return bool(grant.powers[power])
    return ROLE_DEFAULT_POWERS[role][power]


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
