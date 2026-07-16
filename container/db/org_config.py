"""
Org-level config helpers.

`admin_config` is a kv store; `org_default_quota_usd` is the
key used for the org-wide default monthly Bedrock spend cap
(USD). Used by:

  - api/routes/admin_config.py — admin GET/PUT
  - api/routes/users.py — effective_quota_usd projection
  - worker/jobs/deny_reconciler.py — fallback cap when a
    user has no QuotaPolicy override

Resolution order for the org default:

  1. admin_config['org_default_quota_usd'] (preferred)
  2. legacy QuotaPolicy(scope='DEFAULT').monthly_cap_usd
  3. ORG_DEFAULT_QUOTA_USD = 1000.00 (hard fallback)

#746: the org-wide **blocked-model list** lives in the same kv
store under `blocked_models` — a JSON array of model_ids. It
drives the `Resource` set of the reconciler's model DENYLIST
(`DenyBlockedModels`). Empty/unset list = no model statement
emitted → **allow every model by default** (fail-open, owner
posture reversal 2026-06-07 of the #618/#626 allow-list). Spend
caps (QuotaDeny) enforce separately. Global for v1.1; per-user/
team sets are v1.1.1.
"""
from __future__ import annotations
import json
from sqlalchemy.orm import Session

from db.models import AdminConfig, QuotaPolicy

ORG_DEFAULT_QUOTA_KEY = "org_default_quota_usd"
ORG_DEFAULT_QUOTA_USD = 1000.00

# #746: renamed from approved_models. A different kv key, so a
# stale allow-list value from before the posture reversal does
# NOT silently become a block-list (it would invert the meaning
# of every entry). Old approved_models rows are simply ignored.
BLOCKED_MODELS_KEY = "blocked_models"

# #926: does tg OWN the user directory (provision logins), or does an
# external IdP? Models the BEHAVIOR, not the vendor — the only fork the
# login/onboarding logic needs is "do I provision logins or not," so a
# boolean future-proofs against Okta-vs-Ping-vs-any-SAML with zero code
# change. true → tg owns it (Cognito): "enable login" provisions a
# Cognito user, login page shows Cognito. false → an external IdP owns
# it: tg provisions nothing, users come from the IdP (JIT-authorized by
# the auth_routes gate). Per-provider connection details (issuer,
# client id, SAML metadata) belong to the future IdP-config screen, not
# here. Runtime-editable (config-as-data), replacing the env var.
TG_OWNS_DIRECTORY_KEY = "tg_owns_directory"


def get_org_default_quota_usd(db: Session) -> float:
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == ORG_DEFAULT_QUOTA_KEY)
        .first()
    )
    if row and row.value is not None:
        try:
            return float(row.value)
        except (TypeError, ValueError):
            pass
    legacy = (
        db.query(QuotaPolicy)
        .filter(QuotaPolicy.scope == "DEFAULT")
        .first()
    )
    if legacy is not None:
        return float(legacy.monthly_cap_usd)
    return ORG_DEFAULT_QUOTA_USD


def set_org_default_quota_usd(db: Session, value: float) -> float:
    v = float(value)
    if v < 0:
        raise ValueError("org_default_quota_usd must be >= 0")
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == ORG_DEFAULT_QUOTA_KEY)
        .first()
    )
    if row:
        row.value = str(v)
    else:
        db.add(AdminConfig(key=ORG_DEFAULT_QUOTA_KEY, value=str(v)))
    db.flush()
    return v


def get_blocked_models(db: Session) -> list[str]:
    """#746: the org-wide blocked-model list — a JSON array of
    model_ids. Returns [] when unset or malformed (a malformed
    value must not crash the reconciler; it falls back to 'no
    block-list configured' → allow every model)."""
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == BLOCKED_MODELS_KEY)
        .first()
    )
    if not row or row.value is None:
        return []
    try:
        val = json.loads(row.value)
    except (TypeError, ValueError):
        return []
    if not isinstance(val, list):
        return []
    # Keep only non-empty strings; preserve order, dedupe.
    out: list[str] = []
    seen: set[str] = set()
    for item in val:
        if isinstance(item, str) and item.strip():
            s = item.strip()
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def set_blocked_models(db: Session, ids: list[str]) -> list[str]:
    """#746: replace the blocked-model list. Validates that
    every entry is a non-empty string; stores a deduped,
    order-preserving JSON array. Raises ValueError on a
    non-list or non-string entry."""
    if not isinstance(ids, list):
        raise ValueError("blocked_models must be a list")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in ids:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                "each blocked model must be a non-empty string")
        s = item.strip()
        if s not in seen:
            seen.add(s)
            cleaned.append(s)
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == BLOCKED_MODELS_KEY)
        .first()
    )
    payload = json.dumps(cleaned, separators=(",", ":"))
    if row:
        row.value = payload
    else:
        db.add(AdminConfig(key=BLOCKED_MODELS_KEY, value=payload))
    db.flush()
    return cleaned


def get_blocked_models_updated_at(db: Session):
    """When the blocked-model list was last saved (admin_config.updated_at
    for the BLOCKED_MODELS_KEY row), or None if never set. The apply-status
    UI compares this against the last deny_reconciler run's finish time to
    show 'pending' (saved, not yet enforced) vs 'enforced' — a server-side,
    reload-durable signal (no client-only 'just saved' state)."""
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == BLOCKED_MODELS_KEY)
        .first()
    )
    return row.updated_at if row else None


# ── #926: tg_owns_directory (Cognito vs external IdP) ────────────────


def tg_owns_directory(db: Session) -> bool:
    """True when tg owns the user directory (Cognito provisions
    logins); False when an external IdP (Okta/Ping/Azure AD/any SAML)
    owns it. Defaults to True when the key is absent — a fresh install
    is Cognito-only (#926). Any non-"false" stored value reads True so
    a malformed row fails safe to the tg-owned default."""
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == TG_OWNS_DIRECTORY_KEY)
        .first()
    )
    if not row or row.value is None:
        return True
    return str(row.value).strip().lower() != "false"


def set_tg_owns_directory(db: Session, value: bool) -> bool:
    v = bool(value)
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == TG_OWNS_DIRECTORY_KEY)
        .first()
    )
    payload = "true" if v else "false"
    if row:
        row.value = payload
    else:
        db.add(AdminConfig(key=TG_OWNS_DIRECTORY_KEY, value=payload))
    db.flush()
    return v


# ── spend-estimate config (strategy + enforcement) ──────────────────
# The unbilled-spend projection (billed CUR + estimated lag window) is
# admin-configurable: which estimator (average|p90|peak) and how the
# estimate is used (off|warn|enforce). Both live in the same kv store;
# defaults are the safe, display-only posture (average + off) so there
# is no behavior change until an admin opts in. The reconciler reads
# the projected number ONLY in enforce mode.

SPEND_ESTIMATE_STRATEGY_KEY    = "spend_estimate_strategy"
SPEND_ESTIMATE_ENFORCEMENT_KEY = "spend_estimate_enforcement"


def get_spend_estimate_strategy(db: Session) -> str:
    """The estimator strategy (average|p90|peak). Default average;
    any unrecognized stored value coerces to average (never crashes
    the worker on a stale row)."""
    from db.spend_estimate import normalize_strategy
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == SPEND_ESTIMATE_STRATEGY_KEY)
        .first()
    )
    return normalize_strategy(row.value if row else None)


def set_spend_estimate_strategy(db: Session, value: str) -> str:
    from db.spend_estimate import normalize_strategy, VALID_STRATEGIES
    v = (value or "").strip().lower()
    if v not in VALID_STRATEGIES:
        raise ValueError(
            "strategy must be one of average|p90|peak")
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == SPEND_ESTIMATE_STRATEGY_KEY)
        .first()
    )
    if row:
        row.value = v
    else:
        db.add(AdminConfig(
            key=SPEND_ESTIMATE_STRATEGY_KEY, value=v))
    db.flush()
    return normalize_strategy(v)


def get_spend_estimate_enforcement(db: Session) -> str:
    """The enforcement mode (off|warn|enforce). Default off
    (display-only); unrecognized → off (safe)."""
    from db.spend_estimate import normalize_enforcement
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == SPEND_ESTIMATE_ENFORCEMENT_KEY)
        .first()
    )
    return normalize_enforcement(row.value if row else None)


def set_spend_estimate_enforcement(db: Session, value: str) -> str:
    from db.spend_estimate import (
        normalize_enforcement, VALID_ENFORCEMENTS)
    v = (value or "").strip().lower()
    if v not in VALID_ENFORCEMENTS:
        raise ValueError(
            "enforcement must be one of off|warn|enforce")
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == SPEND_ESTIMATE_ENFORCEMENT_KEY)
        .first()
    )
    if row:
        row.value = v
    else:
        db.add(AdminConfig(
            key=SPEND_ESTIMATE_ENFORCEMENT_KEY, value=v))
    db.flush()
    return normalize_enforcement(v)


# ── spend-cap alert thresholds (warn % + exceeded on/off) ───────────
# Spend-cap email notifications are admin-configurable: the warn
# threshold (a percent of the cap at which the user + their admin get
# a heads-up) and whether an over-cap (block) event emits an email at
# all. Both live in the same kv store; defaults are warn at 80% and
# exceeded-email on — a sensible heads-up posture out of the box.

SPEND_ALERT_WARN_PCT_KEY  = "spend_alert_warn_pct"
SPEND_ALERT_EXCEEDED_KEY  = "spend_alert_exceeded"


def get_spend_alert_warn_pct(db: Session) -> int:
    """The warn threshold as a percent of cap (1..100). Default 80;
    a malformed or out-of-range stored value coerces to 80 (never
    crashes the reconciler on a stale row)."""
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == SPEND_ALERT_WARN_PCT_KEY)
        .first()
    )
    if not row or row.value is None:
        return 80
    try:
        v = int(row.value)
    except (TypeError, ValueError):
        return 80
    if v < 1 or v > 100:
        return 80
    return v


def set_spend_alert_warn_pct(db: Session, value: int) -> int:
    v = int(value)
    if v < 1 or v > 100:
        raise ValueError("warn_pct must be between 1 and 100")
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == SPEND_ALERT_WARN_PCT_KEY)
        .first()
    )
    if row:
        row.value = str(v)
    else:
        db.add(AdminConfig(
            key=SPEND_ALERT_WARN_PCT_KEY, value=str(v)))
    db.flush()
    return v


def get_spend_alert_exceeded(db: Session) -> bool:
    """Whether an over-cap (block) event emits an email. Default True;
    stored "false" → False, any other value → True (fail to the
    heads-up default)."""
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == SPEND_ALERT_EXCEEDED_KEY)
        .first()
    )
    if not row or row.value is None:
        return True
    return str(row.value).strip().lower() != "false"


def set_spend_alert_exceeded(db: Session, value: bool) -> bool:
    v = bool(value)
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == SPEND_ALERT_EXCEEDED_KEY)
        .first()
    )
    payload = "true" if v else "false"
    if row:
        row.value = payload
    else:
        db.add(AdminConfig(
            key=SPEND_ALERT_EXCEEDED_KEY, value=payload))
    db.flush()
    return v


def seed_tg_owns_directory(db: Session, env_provider: str | None) -> None:
    """One-time seed on bootstrap/migration (#926). Insert the key only
    when absent — DB is the source of truth thereafter, so this never
    overwrites an operator's later change. For an EXISTING install we
    seed from the current TG_AUTH_PROVIDER once (okta → false, else →
    true) so a live Okta deployment doesn't silently flip to Cognito."""
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == TG_OWNS_DIRECTORY_KEY)
        .first()
    )
    if row is not None:
        return  # already seeded — DB wins, never re-seed
    owns = (env_provider or "").strip().lower() != "okta"
    db.add(AdminConfig(
        key=TG_OWNS_DIRECTORY_KEY,
        value="true" if owns else "false",
    ))
    db.flush()
