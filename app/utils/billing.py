"""Whether a tenant's own users (Client Admin and every branch under it),
or an Auditor-hierarchy login (Auditor/Sub-Auditor), can use the app
right now, and whether they should be warned that they're about to lose
that. Both share the exact same four fields and the same gate logic
below - `Tenant` bills a client, `User` (role Auditor/Sub-Auditor) bills
that auditor's own office - see `_billing_status`.

Three independent gates, any one of which cuts access off (checked in
this order, first match wins):
  - `billing_cycle is None` - pending verification. Every new client or
    Auditor/Sub-Auditor, whoever creates them, starts with no billing
    cycle at all; only the Ultra Admin's `tenants.set_billing_cycle` /
    `tenants.set_auditor_billing_cycle` sets one and lifts this gate.
    The same action can change it later too (e.g. monthly -> yearly) -
    this isn't a one-time "verify" step.
  - `is_active` - the Ultra Admin's (or, for a tenant, an Auditor's
    within their own scope) manual pause, no date attached.
  - `next_billing_due` + BILLING_GRACE_DAYS - a recurring monthly or
    yearly reminder (per `billing_cycle`). The Ultra Admin's "mark this
    period paid" advances it by one month or one year; nothing else
    does. The grace window after it is a notice, not yet a cutoff - only
    once the grace window itself passes unpaid does this gate also
    block. There is deliberately no hard expiry date on top of this.

On top of the calendar-driven notice above, `manual_alarm_active` lets
the Ultra Admin force the same notice banner on at any time (or silence
one that's currently ringing) regardless of where the calendar cycle
actually is - see tenants.trigger_alarm/mute_alarm (and their auditor
equivalents). It only ever affects the notice, never the block.

Enforced in two places (see app.auth.routes._login_view and
app.__init__._register_request_hooks): once at login, so a blocked
login can't sign in to begin with, and again on every request of an
existing session, so one that becomes blocked while someone is already
signed in is cut off immediately rather than at their next login. An
admin impersonating a tenant user or an auditor (tenants.act_as /
tenants.act_as_auditor) bypasses both checks - the whole point of "act
as" is for the firm to still be able to resolve the problem (e.g. mark
them paid) without the gate it's trying to lift also blocking the firm
itself.
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

BILLING_GRACE_DAYS = 3


def add_one_month(on_date: date) -> date:
    """Calendar-correct +1 month, clamped to the target month's last day
    (31 Jan -> 28/29 Feb, not an overflow into March)."""
    year = on_date.year + on_date.month // 12
    month = on_date.month % 12 + 1
    day = min(on_date.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def add_one_year(on_date: date) -> date:
    """Calendar-correct +1 year, clamped for a 29 Feb anchor on a
    non-leap target year."""
    year = on_date.year + 1
    day = min(on_date.day, calendar.monthrange(year, on_date.month)[1])
    return date(year, on_date.month, day)


def advance_billing_cycle(subject, on_date: date) -> date:
    """One step of whichever cycle this tenant/auditor is on."""
    from app.models.tenant import BillingCycle

    if subject.billing_cycle == BillingCycle.YEARLY:
        return add_one_year(on_date)
    return add_one_month(on_date)


def next_due_after(anchor: date, cycle, on_date: date) -> date:
    """The smallest cycle-boundary date, counting forward from `anchor`
    in steps of `cycle`, that is strictly after `on_date`.

    Used only by tenants.resync_billing_cycle/resync_auditor_billing_cycle -
    an explicit, Ultra-Admin-triggered repair action for a `next_billing_due`
    that's drifted from reality (e.g. a tenant whose cycle got "mashed up"
    by repeated clicks before mark_billing_paid's idempotency guards
    existed). It recomputes from the one date that never drifts - the
    fixed `cycle_anchor_date` - rather than compounding onto whatever
    `next_billing_due` currently holds, so it's a true resync, not
    another relative nudge. Never called from the ordinary per-payment
    `mark_billing_paid` flow, which deliberately keeps advancing from the
    tenant's own established due date (see there for why).
    """
    from app.models.tenant import BillingCycle

    step = add_one_year if cycle == BillingCycle.YEARLY else add_one_month
    candidate = anchor
    # A sane upper bound - even a decade-old monthly cycle is ~120 steps.
    for _ in range(2000):
        if candidate > on_date:
            return candidate
        candidate = step(candidate)
    return candidate


@dataclass
class TenantAccessStatus:
    blocked: bool
    reason: str | None = None  # "pending_verification" | "paused" | "billing_overdue"
    message: str | None = None
    # True only when not blocked but within the post-due-date grace
    # window (or the Ultra Admin forced it on) - a heads-up, not yet a
    # lockout.
    billing_notice: bool = False
    billing_notice_message: str | None = None


def _billing_status(subject, pending_message: str, paused_message: str) -> TenantAccessStatus:
    if subject.billing_cycle is None:
        return TenantAccessStatus(blocked=True, reason="pending_verification", message=pending_message)

    if not subject.is_active:
        return TenantAccessStatus(blocked=True, reason="paused", message=paused_message)

    today = date.today()
    cycle_label = "yearly" if subject.billing_cycle.value == "yearly" else "monthly"

    if subject.next_billing_due:
        grace_end = subject.next_billing_due + timedelta(days=BILLING_GRACE_DAYS)
        if today > grace_end:
            return TenantAccessStatus(
                blocked=True,
                reason="billing_overdue",
                message=(
                    f"This account's {cycle_label} renewal was due on {subject.next_billing_due.strftime('%d %b %Y')} "
                    "and the grace period has ended. Contact ARFA to continue."
                ),
            )
        if today >= subject.next_billing_due:
            days_left = (grace_end - today).days
            return TenantAccessStatus(
                blocked=False,
                billing_notice=True,
                billing_notice_message=(
                    f"{cycle_label.title()} renewal was due on {subject.next_billing_due.strftime('%d %b %Y')} - "
                    f"{days_left} day{'s' if days_left != 1 else ''} left before access is paused. "
                    "Contact ARFA once paid."
                ),
            )

    if subject.manual_alarm_active:
        return TenantAccessStatus(
            blocked=False,
            billing_notice=True,
            billing_notice_message="Billing reminder: ARFA has flagged this account for payment. Please contact ARFA.",
        )

    return TenantAccessStatus(blocked=False)


def tenant_access_status(tenant) -> TenantAccessStatus:
    return _billing_status(
        tenant,
        pending_message="This account is awaiting verification by ARFA before it can be used.",
        paused_message="This account has been paused. Contact your ARFA account manager to resume.",
    )


def auditor_access_status(auditor) -> TenantAccessStatus:
    return _billing_status(
        auditor,
        pending_message="This account is awaiting a billing cycle from the Ultra Admin before it can be used.",
        paused_message="This account has been paused. Contact the Ultra Admin to resume.",
    )
