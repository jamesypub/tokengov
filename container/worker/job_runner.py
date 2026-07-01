"""Wraps each job function with JobRun logging to Postgres."""
from __future__ import annotations
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from db.session import get_db
from db.models import JobRun
from db.jobs_pause import get_jobs_paused_until
# #595: bind the JobRun id to the run_id log contextvar so every
# log line a job emits correlates (the worker analogue of #587's
# request_id), and emit stable job.start/ok/fail events.
from api.log_context import run_id_var

log = logging.getLogger("worker.job_runner")


def job(name: str, fn: Callable) -> Callable:
    """
    Wrap a job function so each call writes a JobRun row.

    The wrapped function may return either:
      - a string  → stored as `detail`
      - a dict    → may carry keys `detail`, `blocked`,
                    `unblocked` (lists of emails).

    The wrapper signature accepts `triggered_by` so manual
    runs from the UI can stamp the caller's email; the
    scheduled APScheduler call passes nothing and the row
    stays NULL (the API surfaces NULL → "scheduler").

    Honors the global pause: if admin_config.jobs_paused_until
    is in the future AND the call came from the scheduler
    (no triggered_by), the wrapper logs "job skipped: global
    pause active until <ts>" and exits without writing a
    JobRun row. Manual runs from the UI bypass the pause —
    an admin clicking "Run now" while the system is paused
    has explicitly chosen to override.
    """
    def wrapper(triggered_by: Optional[str] = None):
        if triggered_by is None:
            with get_db() as db:
                paused_until = get_jobs_paused_until(db)
            if paused_until is not None:
                log.info(
                    "job skipped: global pause active until %s "
                    "(job=%s)",
                    paused_until.isoformat(), name,
                )
                return {
                    "detail": (
                        f"skipped: paused until "
                        f"{paused_until.isoformat()}"
                    ),
                    "skipped": True,
                }
        with get_db() as db:
            run = JobRun(
                job_name=name,
                status="running",
                triggered_by=triggered_by,
            )
            db.add(run)
            db.flush()
            run_id = run.id
        # #595: correlate every log line this job emits with the
        # JobRun id (worker analogue of request_id).
        rid_token = run_id_var.set(str(run_id))
        start_ns = time.perf_counter_ns()
        log.info(
            "job.start",
            extra={"event": "job.start", "job": name,
                   "triggered_by": triggered_by or "scheduler"},
        )
        result: dict = {}
        try:
            raw = fn()
            if isinstance(raw, dict):
                result = raw
            else:
                result = {"detail": raw}
            is_skipped = bool(result.get("skipped"))
            final_status = "skipped" if is_skipped else "succeeded"
            with get_db() as db:
                r = db.query(JobRun).filter(JobRun.id == run_id).first()
                if r:
                    r.status = final_status
                    r.finished_at = datetime.now(timezone.utc)
                    r.detail = result.get("detail") or "ok"
                    blocked = result.get("blocked") or []
                    unblocked = result.get("unblocked") or []
                    r.blocked = json.dumps(blocked) if blocked else None
                    r.unblocked = (
                        json.dumps(unblocked) if unblocked else None
                    )
            dur_ms = round((time.perf_counter_ns() - start_ns) / 1e6, 1)
            log.info(
                "job.ok",
                extra={"event": "job.ok", "job": name,
                       "status": final_status, "duration_ms": dur_ms},
            )
        except Exception as e:
            dur_ms = round((time.perf_counter_ns() - start_ns) / 1e6, 1)
            # #595: traceback via log.exception (exc_info → the JSON
            # formatter's `exc` field); structured job.fail event.
            log.exception(
                "job.fail",
                extra={"event": "job.fail", "job": name,
                       "duration_ms": dur_ms},
            )
            with get_db() as db:
                r = db.query(JobRun).filter(JobRun.id == run_id).first()
                if r:
                    r.status = "failed"
                    r.finished_at = datetime.now(timezone.utc)
                    r.detail = str(e)
                    r.error = f"{type(e).__name__}: {e}"
            raise
        finally:
            run_id_var.reset(rid_token)
        return result
    return wrapper
