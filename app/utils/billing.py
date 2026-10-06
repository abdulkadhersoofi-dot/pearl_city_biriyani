"""Whether a tenant's own users (Client Admin and every branch under it)
can use the app right now, and whether they should be warned that they're
about to lose that.

Three independent gates, any one of which cuts a tenant off:
  - `Tenant.is_active` - a Super Admin's manual pause, no date attached.
  - `Tenant.valid_until` - a hard subscription end date. Only a Super
    Admin can move it forward (tenants.renew_access).
  - `Tenant.next_billing_due` + BILLING_GRACE_DAYS - a recurring monthly
    reminder, independent of valid_until. A Super Admin's "mark this
    month paid" (tenants.mark_billing_paid) advances it by one calendar
    month; nothing else does. The grace window after it is a notice, not
    yet a cutoff - only once the grace window itself passes unpaid does
    this gate also block.

Enforced in two places (see app.auth.routes._login_view and
app.__init__._register_request_hooks): once at login, so a blocked
tenant's user can't sign in to begin with, and again on every request of
an existing session, so a tenant that becomes blocked while someone is
already signed in is cut off immediately rather than at their next
login. A Super Admin impersonating a tenant's user (tenants.act_as)
bypasses both checks - the whole point of "act as" is for the firm to
still be able to resolve the client's problem (e.g. mark them paid)
without the gate it's trying to lift also blocking the firm itself.
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


@dataclass
class TenantAccessStatus:
    blocked: bool
    reason: str | None = None  # "paused" | "expired" | "billing_overdue"
    message: str | None = None
    # True only when not blocked but within the post-due-date grace
    # window - a heads-up, not yet a lockout.
    billing_notice: bool = False
    billing_notice_message: str | None = None


def tenant_access_status(tenant) -> TenantAccessStatus:
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

    if tenant.next_billing_due:
        grace_end = tenant.next_billing_due + timedelta(days=BILLING_GRACE_DAYS)
        if today > grace_end:
            return TenantAccessStatus(
                blocked=True,
                reason="billing_overdue",
                message=(
                    f"This account's monthly renewal was due on {tenant.next_billing_due.strftime('%d %b %Y')} "
                    "and the grace period has ended. Contact ARFA to continue."
                ),
            )
        if today >= tenant.next_billing_due:
            days_left = (grace_end - today).days
            return TenantAccessStatus(
                blocked=False,
                billing_notice=True,
                billing_notice_message=(
                    f"Monthly renewal was due on {tenant.next_billing_due.strftime('%d %b %Y')} - "
                    f"{days_left} day{'s' if days_left != 1 else ''} left before access is paused. "
                    "Contact ARFA once paid."
                ),
            )

    return TenantAccessStatus(blocked=False)
