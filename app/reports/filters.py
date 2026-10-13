"""Shared filter parsing for the Stock, Total sales, and Sales-by-item
reports (app.reports.routes) - one period (day/month/custom range) plus
an optional branch selection (single or multiple), read from the same
query-string shape on every report page so one filter bar
(templates/reports/_filter_bar.html) works for all three.
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from app.models.user import User, UserRole

DEFAULT_RANGE_DAYS = 30


@dataclass
class ReportFilters:
    mode: str  # "range" | "day" | "month"
    start: date
    end: date
    branch_ids: list[int] | None  # None means "all branches"
    # Echoed back so the filter bar can re-select exactly what was
    # submitted, not just whatever `start`/`end` resolved to.
    day_value: str
    month_value: str


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_month(value: str | None) -> tuple[int, int] | None:
    """value is an HTML <input type=month> value: "YYYY-MM"."""
    if not value:
        return None
    try:
        year_str, month_str = value.split("-")
        year, month = int(year_str), int(month_str)
        if 1 <= month <= 12:
            return year, month
    except (ValueError, IndexError):
        pass
    return None


def parse_report_filters(args, default_range_days: int = DEFAULT_RANGE_DAYS) -> ReportFilters:
    today = date.today()
    mode = args.get("mode") or "range"
    if mode not in ("range", "day", "month"):
        mode = "range"

    day_value = args.get("date") or today.isoformat()
    month_value = args.get("month") or f"{today.year:04d}-{today.month:02d}"

    if mode == "day":
        d = _parse_date(args.get("date")) or today
        start = end = d
        day_value = d.isoformat()
    elif mode == "month":
        parsed = _parse_month(args.get("month"))
        year, month = parsed or (today.year, today.month)
        last_day = calendar.monthrange(year, month)[1]
        start, end = date(year, month, 1), date(year, month, last_day)
        month_value = f"{year:04d}-{month:02d}"
    else:
        start = _parse_date(args.get("start")) or today - timedelta(days=default_range_days)
        end = _parse_date(args.get("end")) or today
        if start > end:
            start, end = end, start

    raw_branch_ids = [v for v in args.getlist("branch_id") if v.isdigit()]
    branch_ids = [int(v) for v in raw_branch_ids] or None

    return ReportFilters(mode=mode, start=start, end=end, branch_ids=branch_ids, day_value=day_value, month_value=month_value)


def branch_choices(tenant) -> list[User]:
    """Every active branch (Staff) login under this tenant, for the
    filter bar's multi-select - not tenant_query, since report routes
    sometimes build this from the Super Admin's drill-down too (tenant
    passed explicitly, not necessarily current_user's own)."""
    return (
        User.query.filter_by(tenant_id=tenant.id, role=UserRole.STAFF, is_active=True)
        .order_by(User.name)
        .all()
    )
