"""
Last-drift-sweep timestamp helper.

The governance-drift banner needs a "last checked" time. Under the
clean-slate model the drift job replaces the whole `governance_drift`
table each run, so a CLEAN sweep leaves no rows to carry a timestamp.
We stamp the run's time on an `admin_config` key instead, so the banner
shows a fresh "last checked" even when the current sweep found nothing.

Stored as an ISO-8601 UTC string in admin_config (same kv pattern as
jobs_paused_until). A missing/empty/malformed value reads as None
(no sweep has completed yet).
"""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from db.models import AdminConfig

LAST_DRIFT_SWEEP_AT_KEY = "last_drift_sweep_at"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def get_last_drift_sweep_at(db: Session) -> datetime | None:
    """The timestamp of the latest COMPLETED drift sweep, or None if
    none has run yet."""
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == LAST_DRIFT_SWEEP_AT_KEY)
        .first()
    )
    return _parse_iso(row.value if row else None)


def set_last_drift_sweep_at(db: Session, when: datetime) -> None:
    """Stamp the completed-sweep time. Read-modify-write on the shared
    admin_config kv row; caller owns the transaction/flush."""
    iso = when.isoformat()
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == LAST_DRIFT_SWEEP_AT_KEY)
        .first()
    )
    if row:
        row.value = iso
    else:
        db.add(AdminConfig(key=LAST_DRIFT_SWEEP_AT_KEY, value=iso))
