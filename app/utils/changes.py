"""The "Changes" list (packages and powers, stage 6): what was changed for a
client - their package, modules, settings, limits, branding, sign-in
minimums, branch logins - or for an auditor (their powers), when, and by
whom. Read from the audit log, which every change already writes; nothing
extra is stored.
"""

from app.models.audit_log import AuditLog
from app.utils.entitlements import AS_PACKAGE, LIMITS, MODULE_STATES, SECURITY_MINIMUMS, SETTING_STATES, SETTINGS

# kind -> label, for the filter chips
KINDS = {
    "package": "Package",
    "modules": "Modules",
    "settings": "Settings",
    "logins": "Branches & logins",
    "powers": "Powers",
}

# audit action -> kind
CLIENT_ACTIONS = {
    "package_assigned": "package",
    "package_edited": "package",
    "branding_exception_set": "package",
    "limit_exception_set": "package",
    "module_exception_set": "modules",
    "module_requested": "modules",
    "module_request_resolved": "modules",
    "client_modules_switched": "modules",
    "tenant_features_changed": "modules",
    "setting_exception_set": "settings",
    "security_exception_set": "settings",
    "settings_saved": "settings",
    "security_settings_saved": "settings",
    "counter_permissions_exception_set": "logins",
    "login_switches_set": "logins",
    "branch_permissions_changed": "logins",
    "branch_created": "logins",
    "branch_activated": "logins",
    "branch_deactivated": "logins",
}

AUDITOR_ACTIONS = {"auditor_powers_set": "powers"}


def _module(key):
    from app.utils.features import FEATURES

    return FEATURES[key].label if key in FEATURES else key


def _state(value, labels):
    if value is None or value == AS_PACKAGE:
        return "as the package"
    return labels.get(value, str(value))


def _minutes(value):
    if value is None:
        return "no minimum"
    if value == AS_PACKAGE:
        return "as the package"
    if value is True:
        return "required"
    if value is False:
        return "not required"
    return f"at most {value} minutes"


def _permission(key):
    from app.utils.permissions import PERMISSION_LABELS

    return PERMISSION_LABELS.get(key, key)


def _user_name(user_id):
    if not user_id:
        return None
    from app.extensions import db
    from app.models.user import User

    user = db.session.get(User, user_id)
    return user.name if user else None


def describe(log) -> str:
    """One line in plain words for an audit row."""
    d = log.details or {}
    a = log.action
    if a == "package_assigned":
        text = f"Put on the {d.get('to')} package" + (f" (was {d['from']})" if d.get("from") else "")
        return text + (" - kept their own changes" if d.get("kept_exceptions") else "")
    if a == "package_edited":
        return f"The {d.get('name')} package itself was edited"
    if a == "branding_exception_set":
        return f"Own branding: {_state(d.get('from'), {True: 'allowed', False: 'not allowed'})} → " \
               f"{_state(d.get('to'), {True: 'allowed', False: 'not allowed'})}"
    if a == "limit_exception_set":
        label = LIMITS.get(d.get("limit"), d.get("limit"))
        def lim(v):
            return "as the package" if v == AS_PACKAGE else ("no limit" if v is None else str(v))
        return f"{label}: {lim(d.get('from'))} → {lim(d.get('to'))}"
    if a == "module_exception_set":
        return f"{_module(d.get('module'))}: {_state(d.get('from'), MODULE_STATES)} → {_state(d.get('to'), MODULE_STATES)}"
    if a == "module_requested":
        return f"Asked ARFA to add {_module(d.get('module'))}"
    if a == "module_request_resolved":
        outcome = {"choice": "added as their choice", "inc": "included", "decline": "declined"}.get(d.get("action"),
                                                                                                    d.get("action"))
        return f"Request for {_module(d.get('module'))} {outcome}"
    if a == "client_modules_switched":
        on = set(d.get("on") or [])
        parts = [f"{_module(k)} {'on' if k in on else 'off'}" if "on" in d else _module(k) for k in d.get("changed", [])]
        return "Switched for them: " + ", ".join(parts)
    if a == "tenant_features_changed":
        before, after = d.get("before") or {}, d.get("after") or {}
        on = sorted(k for k in after if after.get(k) and not before.get(k))
        off = sorted(k for k in before if before.get(k) and not after.get(k))
        parts = [f"{_module(k)} on" for k in on] + [f"{_module(k)} off" for k in off]
        return "Modules switched: " + (", ".join(parts) if parts else "no change")
    if a == "setting_exception_set":
        label = SETTINGS.get(d.get("setting"), d.get("setting"))
        return f"{label}: {_state(d.get('from'), SETTING_STATES)} → {_state(d.get('to'), SETTING_STATES)}"
    if a == "security_exception_set":
        label = SECURITY_MINIMUMS.get(d.get("minimum"), d.get("minimum"))
        return f"{label}: {_minutes(d.get('from'))} → {_minutes(d.get('to'))}"
    if a == "settings_saved":
        from app.settings.sections import SECTIONS

        names = {k: label for k, label, *_ in SECTIONS}
        return f"Settings saved: {names.get(d.get('section'), d.get('section') or 'all sections')}"
    if a == "security_settings_saved":
        bits = []
        if "idle_minutes" in d:
            bits.append(f"idle sign-out {d['idle_minutes'] or 'never'}{' min' if d['idle_minutes'] else ''}")
        if "lock_minutes" in d:
            bits.append(f"counter lock {d['lock_minutes'] or 'on Lock only'}{' min' if d['lock_minutes'] else ''}")
        return "Sign-in settings: " + ", ".join(bits)
    if a == "counter_permissions_exception_set":
        to = d.get("to")
        return "Counter permissions they may give: " + (
            "as the package" if to is None else (", ".join(_permission(k) for k in to) or "none"))
    if a == "login_switches_set":
        name = _user_name(log.entity_id) or "a branch login"
        after = d.get("after") or {}
        bits = [f"{_permission(k)} always {'on' if v else 'off'}" for k, v in (after.get("permissions") or {}).items()]
        if after.get("max_discount"):
            bits.append(f"discount up to {after['max_discount']}%")
        if after.get("hidden"):
            bits.append("hidden: " + ", ".join(_module(k) for k in after["hidden"]))
        return f"ARFA's switches for {name}: " + ("; ".join(bits) if bits else "handed back to the client")
    if a == "branch_permissions_changed":
        from app.utils.permissions import BRANCH_ROLES

        name = _user_name(log.entity_id) or "a branch login"
        role = d.get("role")
        text = f"Permissions for {name}" + (f": {BRANCH_ROLES[role]['label']}" if role in BRANCH_ROLES else
                                            (": own set" if "role" in d else ""))
        if d.get("hidden_modules"):
            hidden = d["hidden_modules"][1] or []
            text += " - hidden at this counter: " + (", ".join(_module(k) for k in hidden) if hidden else "none")
        return text
    if a in ("branch_created", "branch_activated", "branch_deactivated"):
        name = d.get("name") or _user_name(log.entity_id) or "A branch login"
        verb = {"branch_created": "added", "branch_activated": "switched back on",
                "branch_deactivated": "switched off"}[a]
        return f"Branch login {name} {verb}" + (f" as {d['role_label']}" if d.get("role_label") else "")
    if a == "auditor_powers_set":
        from app.utils.entitlements import AUDITOR_POWERS

        before, after = d.get("before") or {}, d.get("after") or {}
        changed = [f"{AUDITOR_POWERS.get(k, k)} {'on' if v else 'off'}" for k, v in after.items() if before.get(k) != v]
        return "Powers saved" + (": " + "; ".join(changed) if changed else " (no switch changed)")
    return a.replace("_", " ").capitalize()


def _rows(logs, kinds):
    out = []
    for log in logs:
        out.append({"log": log, "kind": kinds.get(log.action), "text": describe(log),
                    "who": log.actor.name if log.actor else "System",
                    "who_role": log.actor.role_label if log.actor else "", "when": log.created_at})
    return out


def client_changes(tenant, kind=None, limit=None, offset=0):
    """(rows, total) - newest first. Includes edits of the package the
    client is on now, made while they were on it."""
    actions = [a for a, k in CLIENT_ACTIONS.items() if kind is None or k == kind]
    query = AuditLog.query.filter(AuditLog.tenant_id == tenant.id, AuditLog.action.in_(actions))
    grant = getattr(tenant, "package_grant", None)
    if grant is not None and "package_edited" in actions:
        since = grant.created_at
        query = AuditLog.query.filter(
            (AuditLog.tenant_id == tenant.id) & AuditLog.action.in_(actions)
            | ((AuditLog.action == "package_edited") & (AuditLog.entity_id == grant.package_id)
               & (AuditLog.created_at >= since)))
    total = query.count()
    query = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(offset)
    if limit:
        query = query.limit(limit)
    return _rows(query.all(), CLIENT_ACTIONS), total


def auditor_changes(user, limit=None, offset=0):
    query = AuditLog.query.filter(AuditLog.action.in_(list(AUDITOR_ACTIONS)), AuditLog.entity_id == user.id)
    total = query.count()
    query = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(offset)
    if limit:
        query = query.limit(limit)
    return _rows(query.all(), AUDITOR_ACTIONS), total
