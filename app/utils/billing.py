"""Whether a tenant's own users (Client Admin and every branch under it)
can use the app right now, and whether they should be warned that they're
about to lose that.

Four independent gates, any one of which cuts a tenant off (checked in
this order, first match wins):
  - `Tenant.billing_cycle is None` - pending verification. Every new
    client, whoever creates them (Ultra Admin or Auditor), starts with no
    billing cycle at all; only the Ultra Admin's verification
    (tenants.verify_client) sets one and lifts this gate.
  - `Tenant.is_active` - the Ultra Admin's (or an Auditor's, within their
    own scope) manual pause, no date attached.
  - `Tenant.valid_until` - a hard subscription end date. Only the Ultra
    Admin can move it forward (tenants.renew_access).
  - `Tenant.next_billing_due` + BILLING_GRACE_DAYS - a recurring monthly
    or yearly reminder (per `Tenant.billing_cycle`), independent of
    valid_until. The Ultra Admin's "mark this period paid"
    (tenants.mark_billing_paid) advances it by one month or one year;
    nothing else does. The grace window after it is a notice, not yet a
    cutoff - only once the grace window itself passes unpaid does this
    gate also block.

On top of the calendar-driven notice above, `Tenant.manual_alarm_active`
lets the Ultra Admin force the same notice banner on at any time (or
silence one that's currently ringing) regardless of where the calendar
cycle actually is - see tenants.trigger_alarm/mute_alarm. It only ever
affects the notice, never the block.

Enforced in two places (see app.auth.routes._login_view and
app.__init__._register_request_hooks): once at login, so a blocked
tenant's user can't sign in to begin with, and again on every request of
an existing session, so a tenant that becomes blocked while someone is
already signed in is cut off immediately rather than at their next
login. The Ultra Admin (or an Auditor) impersonating a tenant's user
(tenants.act_as) bypasses both checks - the whole point of "act as" is
for the firm to still be able to resolve the client's problem (e.g. mark
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


def advance_billing_cycle(tenant, on_date: date) -> date:
    """One step of whichever cycle this tenant is on."""
    from app.models.tenant import BillingCycle

    if tenant.billing_cycle == BillingCycle.YEARLY:
        return add_one_year(on_date)
    return add_one_month(on_date)


@dataclass
class TenantAccessStatus:
    blocked: bool
    reason: str | None = None  # "pending_verification" | "paused" | "expired" | "billing_overdue"
    message: str | None = None
    # True only when not blocked but within the post-due-date grace
    # window (or the Ultra Admin forced it on) - a heads-up, not yet a
    # lockout.
    billing_notice: bool = False
    billing_notice_message: str | None = None


def tenant_access_status(tenant) -> TenantAccessStatus:
    if tenant.billing_cycle is None:
        return TenantAccessStatus(
            blocked=True,
            reason="pending_verification",
            message="This account is awaiting verification by ARFA before it can be used.",
        )

    if not tenant.is_active:
        return TenantAccessStatus(
            blocked=True,
            reason="paused",
            message="This account has been paused. Contact your ARFA account manager to resume.",
        )

    today = date.today()

    if tenant.valid_until and today > tenant.valid_until:
        return TenantAccessStatus(
            blocked=True,
            reason="expired",
            message=(
                f"This account's access expired on {tenant.valid_until.strftime('%d %b %Y')}. "
                "Contact ARFA to renew."
            ),
        )

    cycle_label = "yearly" if tenant.billing_cycle.value == "yearly" else "monthly"

    if tenant.next_billing_due:
        grace_end = tenant.next_billing_due + timedelta(days=BILLING_GRACE_DAYS)
        if today > grace_end:
            return TenantAccessStatus(
                blocked=True,
                reason="billing_overdue",
                message=(
                    f"This account's {cycle_label} renewal was due on {tenant.next_billing_due.strftime('%d %b %Y')} "
                    "and the grace period has ended. Contact ARFA to continue."
                ),
            )
        if today >= tenant.next_billing_due:
            days_left = (grace_end - today).days
            return TenantAccessStatus(
                blocked=False,
                billing_notice=True,
                billing_notice_message=(
                    f"{cycle_label.title()} renewal was due on {tenant.next_billing_due.strftime('%d %b %Y')} - "
                    f"{days_left} day{'s' if days_left != 1 else ''} left before access is paused. "
                    "Contact ARFA once paid."
                ),
            )

    if tenant.manual_alarm_active:
        return TenantAccessStatus(
            blocked=False,
            billing_notice=True,
            billing_notice_message="Billing reminder: ARFA has flagged this account for payment. Please contact ARFA.",
        )

    return TenantAccessStatus(blocked=False)
