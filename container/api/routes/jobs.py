from __future__ import annotations
import importlib
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import JobRun
from db.jobs_pause import (
    clear_jobs_pause,
    get_jobs_paused_until,
    set_jobs_paused_until,
)
from api.auth import get_caller_email, Scope
from worker.job_runner import job as wrap_job

log = logging.getLogger("api.jobs")

router = APIRouter()


def _db():
    with get_db() as db:
        yield db


def _scope(
    request: Request,
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


def _parse_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return list(v) if isinstance(v, list) else []
    except Exception:
        return []


def _run_dict(r: JobRun) -> dict:
    duration_ms = None
    if r.started_at and r.finished_at:
        duration_ms = int(
            (r.finished_at - r.started_at).total_seconds() * 1000
        )
    return {
        # UI keys on `pk` for the React row key — stringified
        # Postgres integer id.
        "pk":           str(r.id),
        "id":           r.id,
        "job_name":     r.job_name,
        "status":       r.status,
        "started_at":   r.started_at.isoformat()
                          if r.started_at else None,
        "finished_at":  r.finished_at.isoformat()
                          if r.finished_at else None,
        "detail":       r.detail,
        # NULL triggered_by = scheduler. UI also handles
        # the literal string "scheduler" the same way.
        "triggered_by": r.triggered_by or "scheduler",
        "blocked":      _parse_list(r.blocked),
        "unblocked":    _parse_list(r.unblocked),
        "error":        r.error,
        "duration_ms":  duration_ms,
    }


# #761: the job-run history (job_runs) keeps rows for jobs that
# have since been RETIRED (e.g. metrics_aggregator, #725), so a
# history-derived list shows ghosts forever. The scheduler registry
# lives in the worker process (not importable here without pulling
# the whole APScheduler wiring), so this is the API-side mirror of
# the currently-scheduled set in worker/main.py — keep the two in
# sync when a job is added/removed. The Jobs list filters history to
# these names so a retired job drops off once it's no longer
# scheduled. (vc_seed / vc_seed_synthetic are populate-only triggers,
# not scheduled jobs, so they're intentionally absent from history.)
_LIVE_JOB_NAMES = frozenset({
    "deny_reconciler",
    # #762: quota_monitor removed (email alerting dropped) — must
    # NOT be in the live-jobs filter or it references a job that no
    # longer exists. Keeps _LIVE_JOB_NAMES ⟷ worker/main.py parity
    # (10 scheduled jobs).
    "quota_reset_monthly",
    "pg_backup",
    "github_sync",
    "pr_classify",
    "pr_cost_rollup",
    "jira_sync",
    "service_account_monitor",
    "governance_drift_check",
    "cur_spend_sync",
})


@router.get("/jobs")
def list_jobs(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    # #761: filter to currently-scheduled jobs so retired jobs
    # (metrics_aggregator, #725) don't linger from old history rows.
    # Query a wider window then filter in Python — a retired job's
    # rows are sparse, so a SQL IN-filter on 200 rows isn't worth the
    # index churn here.
    runs = (
        db.query(JobRun)
        .filter(JobRun.job_name.in_(_LIVE_JOB_NAMES))
        .order_by(JobRun.started_at.desc())
        .limit(200)
        .all()
    )
    pause_until = get_jobs_paused_until(db)
    return {
        "runs": [_run_dict(r) for r in runs],
        "pause_until": (
            pause_until.isoformat() if pause_until else None
        ),
    }


@router.post("/admin/jobs/pause")
def pause_jobs(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Pause all scheduled jobs for `minutes` minutes (#275)."""
    scope.require_org_admin()
    minutes = (body or {}).get("minutes")
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        raise HTTPException(
            400, "minutes must be a positive number")
    # Cap at 24h so a typo can't pause the system for weeks.
    if minutes > 24 * 60:
        raise HTTPException(
            400, "minutes must be <= 1440 (24h)")
    until = set_jobs_paused_until(db, minutes)
    return {"pause_until": until.isoformat()}


@router.delete("/admin/jobs/pause")
def resume_jobs(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    clear_jobs_pause(db)
    return {"pause_until": None}


# Map of UI-trigger-able jobs. Mirrors api/routes/internal.py
# but exposed under /api/jobs/run for the React "Run quota
# sync now" button.
_JOBS_BY_NAME = {
    "deny_reconciler":
        ("worker.jobs.deny_reconciler", "run"),
    # #762: quota_monitor removed (email alerting dropped — in-app
    # quota display is the surface; deny_reconciler enforces).
    # #761: quota_reset_daily dropped — run_daily was removed with the
    # daily_tokens column (#643); the entry would 500 if triggered.
    "quota_reset_monthly":
        ("worker.jobs.quota_reset", "run_monthly"),
    "github_sync":
        ("worker.jobs.github_sync", "run"),
    "pr_classify":
        ("worker.jobs.pr_classify", "run"),
    "pr_cost_rollup":
        ("worker.jobs.pr_cost_rollup", "run"),
    # V&C demo seeders. Exposed so the populate script can hit
    # them via /api/jobs/run on both local-compose and remote
    # (ECS) installs without the docker-compose-exec dance the
    # shell version needed. (#251)
    "vc_seed":
        ("worker.jobs.vc_seed", "run"),
    "vc_seed_synthetic":
        ("worker.jobs.vc_seed_synthetic", "run"),
    # #649: on-demand governance-drift sweep (the daily job's
    # manual "run now" trigger).
    "governance_drift_check":
        ("worker.jobs.governance_drift_check", "run"),
    # #724: on-demand CUR spend sync.
    "cur_spend_sync":
        ("worker.jobs.cur_spend_sync", "run"),
}


@router.post("/jobs/run")
def run_jobs(
    body: dict = None,
    scope: Scope = Depends(_scope),
):
    """
    UI 'Check & enforce limits' trigger. Synchronously runs
    cur_spend_sync → deny_reconciler, mirroring the scheduled
    worker pipeline (#725: metrics_aggregator retired — CUR is
    the spend source now). Each call goes through the
    JobRun-logging wrapper so the Enforcement history table
    in the UI shows a row per click (with triggered_by =
    caller's email). Optional body: {"job": "<name>"} to run
    a single named job.
    """
    scope.require_org_admin()
    body = body or {}
    target = body.get("job")
    if target:
        if target not in _JOBS_BY_NAME:
            raise HTTPException(
                400, f"unknown job: {target}")
        names = [target]
    else:
        names = ["cur_spend_sync", "deny_reconciler"]

    blocked: list[str] = []
    unblocked: list[str] = []
    errors: list[dict] = []
    total_ms = 0
    for name in names:
        mod_name, fn_name = _JOBS_BY_NAME[name]
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name)
            wrapped = wrap_job(name, fn)
            import time
            t0 = time.monotonic()
            result = wrapped(triggered_by=scope.email)
            total_ms += int((time.monotonic() - t0) * 1000)
            if isinstance(result, dict):
                blocked.extend(result.get("blocked") or [])
                unblocked.extend(result.get("unblocked") or [])
        except Exception as e:
            log.exception("job %s failed", name)
            errors.append({
                "job": name,
                "error": f"{type(e).__name__}: {e}",
            })

    return {
        "blocked":     blocked,
        "unblocked":   unblocked,
        "duration_ms": total_ms,
        "errors":      errors,
    }
