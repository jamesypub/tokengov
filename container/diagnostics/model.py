"""CheckResult + the check engine.

A `CheckResult` is the typed verdict of one read-only check. `run_all`
runs a selected set of checks, isolates failures (a raising check
becomes status="error" and never aborts the run), records per-check
wall-clock, and assembles the wire object the API/CLI/web consume.

`summary.status` is the WORST of pass/warn/fail across checks; an
`error` (a broken check) is counted but does NOT set summary.status — a
broken check must not mask a real `fail` nor manufacture one. `severity`
is orthogonal to `status` (it drives sorting/coloring only, never the
summary).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

# Status values (the check verdict). `error` = the check itself broke.
PASS = "pass"
WARN = "warn"
FAIL = "fail"
ERROR = "error"

# Severity (orthogonal to status — sorting/coloring only).
INFO = "info"
WARNING = "warning"
CRITICAL = "critical"

SCHEMA_VERSION = "1"

# Worst-first ordering for summary.status. `error` is deliberately
# absent — it never sets the summary (see module docstring).
_SEVERITY_ORDER = {FAIL: 3, WARN: 2, PASS: 1}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CheckResult:
    id: str            # "<category>.<slug>", e.g. "governance.deny-attached"
    title: str
    status: str        # pass | warn | fail | error
    category: str
    severity: str      # info | warning | critical (orthogonal to status)
    detail: str
    remediation: str = ""   # "" on pass
    checked_at: str = ""    # ISO-8601 UTC (stamped by the engine if unset)
    docs_url: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Check:
    """A single check: identity metadata + a run(ctx) callable that
    returns a CheckResult. `run` should be read-only and may raise — the
    engine catches it and produces an `error` result."""
    id: str
    title: str
    category: str
    severity: str
    run: object          # Callable[[ctx], CheckResult]
    docs_url: str = ""


def _summary_status(statuses: list[str]) -> str:
    """Worst of pass/warn/fail across the checks; error is ignored.
    Empty (all-error, or no checks) → pass (nothing failing)."""
    worst = PASS
    worst_rank = _SEVERITY_ORDER[PASS]
    for s in statuses:
        rank = _SEVERITY_ORDER.get(s)   # None for ERROR → skipped
        if rank is not None and rank > worst_rank:
            worst, worst_rank = s, rank
    return worst


def _selected(checks, only, skip):
    """Filter checks by `only`/`skip`, each a comma-joined string or a
    list of category names OR full check ids. None = no filter."""
    def _norm(v):
        if v is None:
            return None
        if isinstance(v, str):
            v = v.split(",")
        return {x.strip() for x in v if str(x).strip()}

    only_set = _norm(only)
    skip_set = _norm(skip)

    out = []
    for c in checks:
        keys = {c.category, c.id}
        if only_set is not None and not (keys & only_set):
            continue
        if skip_set is not None and (keys & skip_set):
            continue
        out.append(c)
    return out


def run_all(ctx, only=None, skip=None, checks=None) -> dict:
    """Run the selected checks and return the wire object.

    `ctx` is the DiagContext passed to each check's run(). `only`/`skip`
    filter by category or check id. `checks` overrides the default
    registry (used by tests). A check that raises becomes an `error`
    result carrying the exception message — the run never aborts.
    """
    if checks is None:
        from diagnostics.checks import all_checks
        checks = all_checks()

    selected = _selected(checks, only, skip)

    results: list[CheckResult] = []
    for c in selected:
        started = time.monotonic()
        try:
            res = c.run(ctx)
            if not isinstance(res, CheckResult):
                raise TypeError(
                    f"check {c.id} returned {type(res).__name__}, "
                    "expected CheckResult")
        except Exception as e:  # noqa: BLE001 — isolate every check
            res = CheckResult(
                id=c.id,
                title=c.title,
                status=ERROR,
                category=c.category,
                severity=c.severity,
                detail=f"Check raised {type(e).__name__}: {e}",
                remediation=(
                    "This diagnostic check errored — it does not by "
                    "itself indicate a problem with the checked "
                    "resource. Re-run; if it persists, the check may "
                    "need a task-role grant or is hitting a "
                    "misconfigured dependency."),
                docs_url=c.docs_url,
            )
        # Stamp engine-owned fields (a check may set its own checked_at;
        # fill it if blank). duration is always engine-measured.
        res.duration_ms = int((time.monotonic() - started) * 1000)
        if not res.checked_at:
            res.checked_at = _now_iso()
        results.append(res)

    statuses = [r.status for r in results]
    counts = {
        PASS: statuses.count(PASS),
        WARN: statuses.count(WARN),
        FAIL: statuses.count(FAIL),
        ERROR: statuses.count(ERROR),
    }
    cats_with_issues = sorted({
        r.category for r in results if r.status in (WARN, FAIL)
    })

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "tg_version": ctx.tg_version,
        "account_id": ctx.account_id,
        "region": ctx.region,
        "summary": {
            "status": _summary_status(statuses),
            "total": len(results),
            "pass": counts[PASS],
            "warn": counts[WARN],
            "fail": counts[FAIL],
            "error": counts[ERROR],
            "categories_with_issues": cats_with_issues,
        },
        "checks": [r.to_dict() for r in results],
    }
