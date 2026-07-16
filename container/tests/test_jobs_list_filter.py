"""
#761: the Jobs list is built from job_runs history, so a RETIRED job
(metrics_aggregator, #725) lingers forever unless the list filters to
currently-scheduled jobs. This pins that filter: a retired job's
history rows are excluded; live jobs (incl. cur_spend_sync) are shown.
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def admin_client(pg_url, clean_db, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"))
    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        db.add(AdminRole(email="admin@test.com", role="org_admin"))
    from api.main import app
    with TestClient(app) as c:
        yield c


def _seed_run(job_name, status="succeeded"):
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import JobRun
    with get_db() as db:
        db.add(JobRun(
            job_name=job_name, status=status,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc)))


def test_retired_job_excluded_live_jobs_included(admin_client):
    # A retired job (metrics_aggregator) + a couple live ones in
    # history.
    _seed_run("metrics_aggregator")
    _seed_run("cur_spend_sync")
    _seed_run("deny_reconciler")

    body = admin_client.get("/api/jobs").json()
    names = {r["job_name"] for r in body["runs"]}
    assert "metrics_aggregator" not in names
    assert "cur_spend_sync" in names
    assert "deny_reconciler" in names


def test_live_job_set_matches_scheduler():
    # #761: the API-side mirror must match the scheduled set so the
    # filter neither hides a live job nor shows a retired one.
    # #762: quota_monitor removed (email alerting dropped) → 10 jobs.
    from api.routes.jobs import _LIVE_JOB_NAMES
    assert _LIVE_JOB_NAMES == frozenset({
        "deny_reconciler", "quota_reset_monthly",
        "pg_backup", "github_sync", "pr_classify", "pr_cost_rollup",
        "jira_sync", "service_account_monitor",
        "governance_drift_check", "cur_spend_sync",
    })


def test_retired_quota_reset_daily_not_triggerable():
    # #761: run_daily was removed with daily_tokens (#643); the
    # trigger entry is gone so it can't 500.
    from api.routes.jobs import _JOBS_BY_NAME
    assert "quota_reset_daily" not in _JOBS_BY_NAME
    assert "quota_reset_monthly" in _JOBS_BY_NAME
