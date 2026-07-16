"""
Global jobs-pause helpers (#275).

Stores `jobs_paused_until` in admin_config — an ISO-8601 UTC
timestamp. While the timestamp is in the future, the worker
scheduler short-circuits every job and the API surfaces the
state to the UI so admins can render a banner + per-row
"paused" badges.

A null/missing row, an empty string, or a timestamp in the
past all mean "not paused". The timestamp itself is the
source of truth — the scheduled re-resume happens by the wall
clock, no separate cleanup job needed.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session

from db.models import AdminConfig

JOBS_PAUSED_UNTIL_KEY = "jobs_paused_until"


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


def get_jobs_paused_until(db: Session) -> datetime | None:
    """Returns the active pause expiry, or None when not
    paused (no row, empty value, malformed value, or past
    timestamp)."""
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == JOBS_PAUSED_UNTIL_KEY)
        .first()
    )
    dt = _parse_iso(row.value if row else None)
    if dt is None:
        return None
    if dt <= datetime.now(timezone.utc):
        return None
    return dt


def set_jobs_paused_until(db: Session, minutes: float) -> datetime:
    if minutes is None or minutes <= 0:
        raise ValueError("minutes must be a positive number")
    until = datetime.now(timezone.utc) + timedelta(
        minutes=float(minutes),
    )
    iso = until.isoformat()
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == JOBS_PAUSED_UNTIL_KEY)
        .first()
    )
    if row:
        row.value = iso
    else:
        db.add(AdminConfig(key=JOBS_PAUSED_UNTIL_KEY, value=iso))
    db.flush()
    return until


def clear_jobs_pause(db: Session) -> None:
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == JOBS_PAUSED_UNTIL_KEY)
        .first()
    )
    if row:
        # Empty string instead of delete — idempotent reads.
        row.value = ""
        db.flush()


def is_paused(db: Session) -> bool:
    return get_jobs_paused_until(db) is not None
