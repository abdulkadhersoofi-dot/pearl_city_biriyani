"""Helpers for the parallel `line_xxx[]` arrays the invoice, note and POS
forms post - one list per column, one entry per line."""

_TRUTHY = {"1", "true", "on", "y", "yes"}


def posted_flag(values, i) -> bool:
    """One checkbox/hidden-flag entry from a parallel line array - absent
    (an older form or client that never sent it) reads as False."""
    return i < len(values) and str(values[i]).strip().lower() in _TRUTHY
