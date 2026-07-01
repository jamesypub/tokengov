"""
quota_reset — #643: with the per-day quota_metrics grain there are
no rolling counters to zero. Windowed reads (7d/30d/MTD/today) just
SUM the relevant usage_date range, so:
  - run_daily is GONE (daily_tokens column was dropped).
  - run_monthly is no longer a zeroing pass — "ignore prior months"
    is now implicit in the month-start filter. It becomes a
    retention prune: delete day-rows older than the retention
    window so the table doesn't grow unbounded. Keeps the monthly
    schedule slot meaningful instead of deleting it outright.
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta, timezone

from db.session import get_db
from db.models import CurUserSpend, User

log = logging.getLogger("worker.quota_reset")

# How many days of per-day rows to keep. Default 400 (>1y, so MTD /
# 30d / 7d windows and a full year of history all resolve). The CUR/
# Athena path remains the authoritative long-term billed-cost store;
# quota_metrics is the fast operational table, so a bounded window is
# fine. Env-overridable.
RETENTION_DAYS = int(os.environ.get("QUOTA_METRICS_RETENTION_DAYS", "400"))


def run_monthly() -> str:
    """Retention prune: delete per-day rows older than
    RETENTION_DAYS. No-op when nothing is that old."""
    cutoff = (
        datetime.now(timezone.utc).date()
        - timedelta(days=RETENTION_DAYS)
    )
    with get_db() as db:
        n = (
            db.query(CurUserSpend)
            .filter(CurUserSpend.usage_hour < cutoff)
            .delete(synchronize_session=False)
        )
        # Month rollover clears the spend-cap warn latch for every user
        # so a user who crossed the warn threshold last month gets a
        # fresh warn when they re-cross this month.
        db.query(User).update(
            {User.last_warn_sent_at: None},
            synchronize_session=False,
        )
    return (
        f"monthly prune: deleted {n} rows older than {cutoff}; "
        f"cleared warn latch for all users"
    )
