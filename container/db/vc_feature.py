"""
Velocity & Cost feature-flag helper (#1056).

The V&C page (/velocity-cost) is a developing surface (#684) and
ships OFF behind a RUNTIME admin_config flag — no env var, no CFN
param, no redeploy — flipped from the Org Settings "Experimental
features" section, exactly like the Jira flag (#447, db/jira_feature.py).
When OFF, the V&C nav item is hidden and the route redirects.

Config key `vc_enabled` holds the string "true"/"false". A missing
row, empty string, or any non-"true" value all mean OFF (default-off).
"""
from __future__ import annotations
from sqlalchemy.orm import Session

from db.models import AdminConfig

VC_ENABLED_KEY = "vc_enabled"


def is_vc_enabled(db: Session) -> bool:
    """True iff admin_config['vc_enabled'] == "true". Absent
    row / empty / any other value → False (default OFF)."""
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == VC_ENABLED_KEY)
        .first()
    )
    return bool(row) and (row.value or "").strip().lower() == "true"


def set_vc_enabled(db: Session, enabled: bool) -> bool:
    """Persist the flag as "true"/"false". Returns the value
    set. Upsert — idempotent on re-write."""
    val = "true" if enabled else "false"
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == VC_ENABLED_KEY)
        .first()
    )
    if row:
        row.value = val
    else:
        db.add(AdminConfig(key=VC_ENABLED_KEY, value=val))
    db.flush()
    return enabled
