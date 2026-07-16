"""
#643: shared date-window helpers for the per-day quota_metrics
grain. Centralizes the SUM-over-a-usage_date-range filters so every
consumer (deny_reconciler, service_account_monitor,
the API routes) computes "this month", "last 7 days", etc. the same
way and stays consistent if the windowing ever changes.

All dates are UTC. usage_date is a DATE column, so these return
python `date` objects to compare against it.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def month_start_utc() -> date:
    """First day of the current UTC month — the lower bound for the
    monthly spend sum that replaces the old `month == YYYY-MM`
    equality (preserves the per-email monthly total exactly)."""
    t = today_utc()
    return t.replace(day=1)


def window_start_utc(window: str) -> date | None:
    """Lower bound (inclusive) for a named window, or None for
    'all'. Windows:
      - '7d'  → last 7 days incl. today (today - 6)
      - '30d' → last 30 days incl. today (today - 29)
      - 'mtd' → month-to-date (first of month)  [DEFAULT semantics]
      - 'all' → None (no lower bound)
    Unknown values fall back to 'mtd' to preserve the prior
    month-to-date behavior."""
    t = today_utc()
    if window == "7d":
        return t - timedelta(days=6)
    if window == "30d":
        return t - timedelta(days=29)
    if window == "all":
        return None
    # 'mtd' and any unrecognized value
    return t.replace(day=1)
