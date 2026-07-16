"""
Spend projection: billed CUR + estimated unbilled gap.

tg's spend is CUR-billed and ≤24h lagged, so the UI shows spend hours
behind real usage and a runaway user isn't visible (or enforceable)
until CUR delivers. This module projects the **unbilled lag window**
(last-billed-CUR-hour → now) from the principal's own recent billed
hourly rate — no new AWS surface, no re-enabling invocation logs. The
estimate never overwrites billed spend; it's a separate derived number:

    estimated_unbilled = hourly_rate(strategy) × unbilled_hours
    projected_mtd      = billed_mtd + estimated_unbilled

The functions here are **pure** (operate on already-fetched billed
hourly spend values) so the estimator math is unit-testable without a
DB or AWS. The worker/API supply the inputs; the reconciler reads the
projection only in `enforce` mode.

Owner-resolved parameters (build to these):
  - Rate window = trailing 7 days (the rate reflects *current*
    behavior; billed_mtd stays month-to-date — the cap is monthly).
  - Strategy is admin-pickable: average (default) | p90 | peak.
  - p90 = the value at rank ceil(0.90 × N) of the sorted active-hour
    spend (a position in the distribution, robust to one freak hour).
  - Low-sample fallback: < 10 active hours → use Average (a p90/peak
    over few points ≈ the max — unstable for new/light users).
  - unbilled_hours capped at 36h (> the CUR lag) so a user who simply
    stopped using Bedrock isn't projected forever.
  - Projection = flat rate × hours (dense, low-variance usage; profile
    replay is a future refinement, out of scope).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

STRATEGY_AVERAGE = "average"
STRATEGY_P90     = "p90"
STRATEGY_PEAK    = "peak"
VALID_STRATEGIES = (STRATEGY_AVERAGE, STRATEGY_P90, STRATEGY_PEAK)

ENFORCE_OFF     = "off"
ENFORCE_WARN    = "warn"
ENFORCE_ENFORCE = "enforce"
VALID_ENFORCEMENTS = (ENFORCE_OFF, ENFORCE_WARN, ENFORCE_ENFORCE)

# Owner-resolved constants.
RATE_WINDOW_DAYS    = 7
UNBILLED_HOURS_CAP  = 36
LOW_SAMPLE_MIN_HOURS = 10


def normalize_strategy(value: Optional[str]) -> str:
    """Coerce a stored/requested strategy to a valid one; unknown or
    None → the default (average). Keeps the estimator total: a stale
    config value can never crash the worker."""
    v = (value or "").strip().lower()
    return v if v in VALID_STRATEGIES else STRATEGY_AVERAGE


def normalize_enforcement(value: Optional[str]) -> str:
    """Coerce a stored enforcement mode to a valid one; unknown or
    None → the safe default (off, display-only)."""
    v = (value or "").strip().lower()
    return v if v in VALID_ENFORCEMENTS else ENFORCE_OFF


def hourly_rate(active_hour_spend: list[float], strategy: str) -> float:
    """The per-active-hour $ rate under the chosen strategy, over the
    principal's recent billed **active** hours (hours with spend > 0;
    the caller filters idle hours out so the rate reflects burn while
    working, not a diluted 24/7 average).

    - average  : mean of the active-hour values.
    - p90      : value at rank ceil(0.90 × N) of the ascending sort
                 (1-indexed) — a position, robust to one freak hour.
    - peak     : max active-hour value (worst-case; over-projects).

    Low-sample fallback: with fewer than LOW_SAMPLE_MIN_HOURS active
    hours, a p90/peak over few points ≈ the max and is unstable, so
    fall back to average. Returns 0.0 for an empty sample."""
    vals = [float(v) for v in active_hour_spend if v and v > 0]
    if not vals:
        return 0.0
    strat = normalize_strategy(strategy)
    n = len(vals)
    # Low-sample: p90/peak collapse toward the max — use average.
    if n < LOW_SAMPLE_MIN_HOURS:
        strat = STRATEGY_AVERAGE
    if strat == STRATEGY_AVERAGE:
        return sum(vals) / n
    if strat == STRATEGY_PEAK:
        return max(vals)
    # p90: rank = ceil(0.90 × N), 1-indexed into the ascending sort.
    ordered = sorted(vals)
    rank = math.ceil(0.90 * n)
    rank = max(1, min(rank, n))   # clamp into [1, n]
    return ordered[rank - 1]


def clamp_unbilled_hours(hours: float) -> float:
    """Clamp the unbilled gap to [0, UNBILLED_HOURS_CAP]. The cap stops
    projecting a user who simply stopped using Bedrock (the gap would
    otherwise grow without bound past the CUR lag)."""
    if hours is None or hours < 0:
        return 0.0
    return min(float(hours), float(UNBILLED_HOURS_CAP))


def is_low_sample(active_hours: int) -> bool:
    """True when the rate sample is too thin for a stable p90/peak —
    the UI marks the estimate low-confidence and the rate falls back to
    average."""
    return active_hours < LOW_SAMPLE_MIN_HOURS


def project(
    *,
    billed_mtd: float,
    active_hour_spend: list[float],
    unbilled_hours: float,
    strategy: str,
) -> dict:
    """Compute the full projection for one principal. Pure — the caller
    supplies billed MTD, the trailing-window active-hour $ list, and the
    raw unbilled-hour gap. Returns the numbers the API/reconciler use:

        {billed, estimated, projected, unbilled_hours, rate, strategy,
         active_hours, low_sample}

    `strategy` is the *requested* strategy; `low_sample` flags when the
    rate silently fell back to average (so the UI can say so)."""
    billed = float(billed_mtd or 0.0)
    requested = normalize_strategy(strategy)
    active = [float(v) for v in active_hour_spend if v and v > 0]
    n = len(active)
    capped_hours = clamp_unbilled_hours(unbilled_hours)
    rate = hourly_rate(active, requested)
    estimated = round(rate * capped_hours, 4)
    return {
        "billed": round(billed, 4),
        "estimated": estimated,
        "projected": round(billed + estimated, 4),
        "unbilled_hours": round(capped_hours, 2),
        "rate": round(rate, 4),
        "strategy": requested,
        "active_hours": n,
        "low_sample": is_low_sample(n),
    }


# ── DB-backed projection (worker + API) ──────────────────────────────


def project_for_principal(
    db,
    email: str,
    *,
    billed_mtd: float,
    strategy: str,
    now: Optional[datetime] = None,
) -> dict:
    """Compute the projection for one principal from CurUserSpend.

    Fetches the trailing-RATE_WINDOW_DAYS per-hour billed spend (active
    hours only, summed across model/region within each hour), derives
    the unbilled gap from the latest billed `usage_hour` → now, and
    returns :func:`project`'s dict. `billed_mtd` is passed in by the
    caller (the reconciler already sums it; the API can too) so this
    doesn't re-sum the month.

    Kept out of the pure section because it touches the DB — the math
    it delegates to (:func:`hourly_rate`, :func:`project`) stays pure
    and unit-tested. `now` is injectable for tests."""
    from sqlalchemy import func
    from db.models import CurUserSpend

    if now is None:
        now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=RATE_WINDOW_DAYS)

    # Per-hour billed $ across the trailing window (sum models/regions
    # within each usage_hour bucket); active hours are spend>0.
    hour_rows = (
        db.query(
            CurUserSpend.usage_hour.label("hour"),
            func.sum(CurUserSpend.spend_usd).label("spend"),
        )
        .filter(CurUserSpend.email == email)
        .filter(CurUserSpend.usage_hour >= window_start)
        .group_by(CurUserSpend.usage_hour)
        .all()
    )
    active_hour_spend = [
        float(r.spend or 0) for r in hour_rows if (r.spend or 0) > 0]

    # The latest billed hour anywhere for this principal — the unbilled
    # gap is the time since AWS last billed it. No billed data → no
    # projection (gap 0).
    latest = (
        db.query(func.max(CurUserSpend.usage_hour))
        .filter(CurUserSpend.email == email)
        .scalar()
    )
    if latest is None:
        unbilled = 0.0
    else:
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        # CUR buckets are hour-START; a freshly-billed hour is "done"
        # one hour after its start, so the gap is now - (latest + 1h).
        gap_secs = (now - (latest + timedelta(hours=1))).total_seconds()
        unbilled = max(0.0, gap_secs / 3600.0)

    return project(
        billed_mtd=billed_mtd,
        active_hour_spend=active_hour_spend,
        unbilled_hours=unbilled,
        strategy=strategy,
    )
