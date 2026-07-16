"""
Jira feature-flag helper (#447).

The Jira feature (Velocity & Cost "Jira" tab + the jira_sync /
jira_synth_seed worker jobs) is deferred to V1.3 and ships
OFF. The toggle is a RUNTIME admin_config flag — no env var, no
CFN param, no redeploy — flipped from the Org Settings
"Experimental features" section. This mirrors the
admin_config read pattern used by db/jobs_pause.py.

Config key `jira_enabled` holds the string "true"/"false". A
missing row, empty string, or any non-"true" value all mean
OFF (default-off).
"""
from __future__ import annotations
from sqlalchemy.orm import Session

from db.models import AdminConfig

JIRA_ENABLED_KEY = "jira_enabled"


def is_jira_enabled(db: Session) -> bool:
    """True iff admin_config['jira_enabled'] == "true". Absent
    row / empty / any other value → False (default OFF)."""
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == JIRA_ENABLED_KEY)
        .first()
    )
    return bool(row) and (row.value or "").strip().lower() == "true"


def set_jira_enabled(db: Session, enabled: bool) -> bool:
    """Persist the flag as "true"/"false". Returns the value
    set. Upsert — idempotent on re-write."""
    val = "true" if enabled else "false"
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == JIRA_ENABLED_KEY)
        .first()
    )
    if row:
        row.value = val
    else:
        db.add(AdminConfig(key=JIRA_ENABLED_KEY, value=val))
    db.flush()
    return enabled
