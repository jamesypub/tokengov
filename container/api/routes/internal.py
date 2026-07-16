"""
Internal endpoints — synchronous job triggers for testing/admin.

Requires the caller to be org_admin (auth via session cookie or
SigV4, same as the rest of /api/*). Earlier versions were
unauthenticated and relied on SG/ingress filtering, which is
fragile when the api task shares an ALB listener with /api/*.
"""
from __future__ import annotations

import logging

import inspect

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from api.auth import Scope, get_caller_email
from db.session import get_db

router = APIRouter(prefix="/internal")
log = logging.getLogger("internal")

_JOBS = {
    "deny_reconciler":
        ("worker.jobs.deny_reconciler", "run"),
    # #762: quota_monitor removed (email alerting dropped).
    # #761: quota_reset_daily dropped — run_daily was removed with the
    # daily_tokens column (#643).
    "quota_reset_monthly":
        ("worker.jobs.quota_reset", "run_monthly"),
    "pg_backup":
        ("worker.jobs.pg_backup", "run"),
    "jira_synth_seed":
        ("worker.jobs.jira_synth_seed", "run"),
    "seed_quota_metrics":
        ("worker.jobs.seed_quota_metrics", "run"),
    "governance_drift_check":
        ("worker.jobs.governance_drift_check", "run"),
    "cur_spend_sync":
        ("worker.jobs.cur_spend_sync", "run"),
}


def _get_db_dep():
    with get_db() as db:
        yield db


def _require_org_admin(
    email: str = Depends(get_caller_email),
    db: Session = Depends(_get_db_dep),
) -> str:
    Scope(email, db).require_org_admin()
    return email


@router.post(
    "/run-job/{name}",
    dependencies=[Depends(_require_org_admin)],
)
def run_job(name: str, request: Request):
    if name not in _JOBS:
        raise HTTPException(
            404, f"unknown job: {name}")
    mod_name, fn_name = _JOBS[name]
    import importlib
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, fn_name)
    # Forward query params that match the job's signature,
    # coerced to the param's annotated type. Jobs that take no
    # args (most of them) just get fn() — extra query params
    # are ignored. Lets seed_quota_metrics accept
    # ?spend_per_user=1.00 (#426) without bespoke routing.
    # Coerce by the annotation, accepting both real types and
    # the stringized form that `from __future__ import
    # annotations` produces (e.g. the literal "float").
    _COERCE = {
        int: int, float: float, str: str,
        "int": int, "float": float, "str": str,
    }
    kwargs = {}
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None  # builtin without a signature → no args
    if sig is not None:
        for pname, param in sig.parameters.items():
            if pname not in request.query_params:
                continue
            raw = request.query_params[pname]
            conv = _COERCE.get(param.annotation, lambda x: x)
            try:
                kwargs[pname] = conv(raw)
            except (TypeError, ValueError):
                raise HTTPException(
                    400,
                    f"bad value for '{pname}': {raw!r}",
                )
    try:
        fn(**kwargs)
        return {"job": name, "status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("job %s failed", name)
        raise HTTPException(
            500,
            f"{type(e).__name__}: see server logs",
        )
