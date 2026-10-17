"""The console's Today page (console cleanup, stage 2): where every console
login lands - what needs them now, a few counts, and what changed today.

  /admin/today

The Ultra Admin sees the whole firm: clients and auditors waiting to be
verified, module requests, clients with no auditor, renewals due and
anyone who can't sign in. An Auditor or Sub-Auditor sees the same for
their own clients only (verifying and module requests stay ARFA's).
Everything links to where it's dealt with; nothing is changed here.
"""

from datetime import datetime, time

from flask import render_template, url_for
from flask_login import current_user

from app.auth.decorators import admin_or_auditor_required
from app.einvoice.providers.common import IST
from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.package import ModuleRequest
from app.models.tenant import Tenant
from app.tenants import tenants_bp
from app.utils.auditor_scope import auditor_tenant_ids, visible_auditors
from app.utils.billing import auditor_access_status, tenant_access_status
from app.utils.changes import AUDITOR_ACTIONS, CLIENT_ACTIONS, describe
from app.utils.features import FEATURES

# Console actions worth showing under "Changed today", besides the
# package / module / settings / login ones the Changes list already knows.
# Views, sign-ins and exports are left out - they change nothing.
CONSOLE_ACTIONS = {
    "client_onboarded": "Onboarded",
    "client_verified": "Verified - billing cycle set",
    "client_billing_cycle_changed": "Billing cycle changed",
    "client_billing_marked_paid": "Marked paid for the period",
    "client_activated": "Resumed",
    "client_deactivated": "Paused",
    "client_auditor_reassigned": "Auditor changed",
    "client_allocated_to_sub_auditor": "Handed to a Sub-Auditor",
    "subscription_fee_set": "Subscription fee set",
    "tenant_gstin_added": "GSTIN added",
    "client_user_activated_by_firm": "A login unblocked",
    "client_user_deactivated_by_firm": "A login blocked",
    "auditor_created": "Auditor created",
    "sub_auditor_created": "Sub-Auditor created",
    "auditor_verified": "Auditor verified",
    "auditor_billing_cycle_changed": "Auditor's billing cycle changed",
    "auditor_billing_marked_paid": "Auditor marked paid",
    "auditor_activated": "Auditor resumed",
    "auditor_deactivated": "Auditor paused",
    "package_created": "Package created",
    "package_retired": "Package retired",
    "package_restored": "Package made available again",
}
SHOWN_ACTIONS = set(CONSOLE_ACTIONS) | set(CLIENT_ACTIONS) | set(AUDITOR_ACTIONS)
ROWS_PER_ITEM = 5
CHANGES_SHOWN = 12


def _state(status) -> str:
    """active / renewal / blocked / pending - the same words the Clients list filters by."""
    if status.blocked:
        return "pending" if status.reason == "pending_verification" else "blocked"
    return "renewal" if status.billing_notice else "active"


def _item(icon, tone, title, detail, url, action, rows=()):
    return {"icon": icon, "tone": tone, "title": title, "detail": detail, "url": url, "action": action,
            "rows": list(rows)[:ROWS_PER_ITEM], "more": max(0, len(rows) - ROWS_PER_ITEM)}


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _client_row(t, note=""):
    return {"label": t.display_name, "note": note, "url": url_for("tenants.detail", tenant_id=t.id)}


def today_context(user) -> dict:
    ids = auditor_tenant_ids(user)
    query = Tenant.query
    if ids is not None:
        query = query.filter(Tenant.id.in_(ids)) if ids else query.filter(db.false())
    clients = query.order_by(Tenant.legal_name).all()
    by_state = {"active": [], "renewal": [], "blocked": [], "pending": []}
    statuses = {}
    for t in clients:
        statuses[t.id] = tenant_access_status(t)
        by_state[_state(statuses[t.id])].append(t)
    ultra = user.is_super_admin
    clients_url = lambda show: url_for("tenants.directory", show=show)  # noqa: E731
    needs = []

    if by_state["pending"]:
        n = len(by_state["pending"])
        if ultra:
            needs.append(_item("badge-check", "warn", f"{_plural(n, 'client')} waiting to be verified",
                               "Check the details and set a billing cycle - they can't sign in until then.",
                               clients_url("pending"), "Verify",
                               [_client_row(t, f"onboarded {t.created_at.strftime('%d %b')}"
                                            + (f" by {t.auditor.name}" if t.auditor else ""))
                                for t in by_state["pending"]]))
        else:
            needs.append(_item("clock", "info", f"{_plural(n, 'client')} waiting for ARFA to verify",
                               "Nothing to do - they can sign in once ARFA sets a billing cycle.",
                               clients_url("pending"), "See them",
                               [_client_row(t, f"onboarded {t.created_at.strftime('%d %b')}")
                                for t in by_state["pending"]]))

    auditors = visible_auditors(user) if ultra else []
    if ultra:
        waiting = [a for a in auditors if a.billing_cycle is None]
        if waiting:
            needs.append(_item("user-cog", "warn", f"{_plural(len(waiting), 'auditor')} waiting to be verified",
                               "Set a billing cycle on their page so they can sign in.",
                               url_for("tenants.auditors_directory", show="pending"), "Verify",
                               [{"label": a.name, "note": a.role_label,
                                 "url": url_for("tenants.auditor_detail", auditor_id=a.id)} for a in waiting]))
        requests = (ModuleRequest.query.filter_by(status=ModuleRequest.PENDING)
                    .order_by(ModuleRequest.created_at).all())
        if requests:
            needs.append(_item("layout-grid", "warn", f"{_plural(len(requests), 'module request')} to decide",
                               "Clients asked ARFA to add a module to their package.",
                               url_for("tenants.detail", tenant_id=requests[0].tenant_id, tab="package", _anchor="sec-package"),
                               "Decide",
                               [{"label": r.tenant.display_name,
                                 "note": f"asks for {FEATURES[r.feature_key].label if r.feature_key in FEATURES else r.feature_key}",
                                 "url": url_for("tenants.detail", tenant_id=r.tenant_id, tab="package", _anchor="sec-package")}
                                for r in requests]))
        unallotted = [t for t in clients if t.auditor_id is None and t.billing_cycle is not None]
        if unallotted:
            needs.append(_item("users", "info", f"{_plural(len(unallotted), 'client')} with no auditor",
                               "Allot an auditor from the client's page.", clients_url("no_auditor"), "Allot",
                               [_client_row(t) for t in unallotted]))

    if by_state["blocked"]:
        rows = [_client_row(t, "paused" if statuses[t.id].reason == "paused" else "renewal overdue")
                for t in by_state["blocked"]]
        needs.append(_item("triangle-alert", "bad", f"{_plural(len(rows), 'client')} can't sign in",
                           "Paused, or the renewal is past its grace days.", clients_url("blocked"), "Open", rows))
    if by_state["renewal"]:
        rows = [_client_row(t, f"due {t.next_billing_due.strftime('%d %b')}" if t.next_billing_due else "reminder on")
                for t in by_state["renewal"]]
        needs.append(_item("calendar-clock", "warn", f"{_plural(len(rows), 'renewal')} due",
                           "Still signing in - mark the period paid once the money's in.", clients_url("renewal"),
                           "Open", rows))
    if ultra:
        overdue = [a for a in auditors if a.billing_cycle is not None and (
            auditor_access_status(a).billing_notice or auditor_access_status(a).reason == "billing_overdue")]
        if overdue:
            needs.append(_item("banknote", "warn", f"{_plural(len(overdue), 'auditor')} with a renewal due",
                               "Their own office subscription.", url_for("tenants.auditors_directory", show="renewal"),
                               "Open",
                               [{"label": a.name, "note": a.role_label,
                                 "url": url_for("tenants.auditor_detail", auditor_id=a.id)} for a in overdue]))

    changes, changes_today = _changes_today(ids)
    stats = [
        {"label": "Active clients", "value": len(by_state["active"]) + len(by_state["renewal"]),
         "icon": "building-2", "tone": "ok", "url": clients_url("active")},
        {"label": "Renewals due", "value": len(by_state["renewal"]), "icon": "calendar-clock", "tone": "warn",
         "url": clients_url("renewal")},
    ]
    if ultra or user.is_auditor:
        stats.append({"label": "Auditors" if ultra else "Sub-Auditors",
                      "value": len(auditors) if ultra else len(list(user.sub_auditors)), "icon": "user-cog",
                      "tone": "", "url": url_for("tenants.auditors_directory")})
    else:
        stats.append({"label": "Can't sign in", "value": len(by_state["blocked"]), "icon": "triangle-alert",
                      "tone": "bad", "url": clients_url("blocked")})
    stats.append({"label": "Changes today", "value": changes_today, "icon": "activity", "tone": "",
                  "url": url_for("tenants.audit_log") if ultra else None})
    return {"needs": needs, "stats": stats, "changes": changes, "client_count": len(clients)}


def _changes_today(ids):
    """(rows, count) of today's changes - every client's for the Ultra
    Admin; an auditor's own clients' otherwise."""
    start = datetime.combine(datetime.now(IST).date(), time.min, tzinfo=IST)   # midnight, Indian time
    query = AuditLog.query.filter(AuditLog.created_at >= start, AuditLog.action.in_(SHOWN_ACTIONS))
    if ids is not None:
        query = query.filter(AuditLog.tenant_id.in_(ids)) if ids else query.filter(db.false())
    count = query.count()
    rows = []
    for log in query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(CHANGES_SHOWN).all():
        text = CONSOLE_ACTIONS.get(log.action) or describe(log)
        tenant = Tenant.query.get(log.tenant_id) if log.tenant_id else None
        rows.append({"text": text, "when": log.created_at, "who": log.actor.name if log.actor else "System",
                     "client": tenant.display_name if tenant else None,
                     "url": url_for("tenants.detail", tenant_id=tenant.id) if tenant else None})
    return rows, count


def greeting(now=None) -> str:
    hour = (now or datetime.now(IST)).hour
    return "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")


@tenants_bp.route("/today")
@admin_or_auditor_required
def today():
    return render_template("tenants/today.html", greeting=greeting(), today=datetime.now(IST).date(),
                           **today_context(current_user))
