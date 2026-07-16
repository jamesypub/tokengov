"""
Tests for the global jobs-pause feature (#275).

Covers:
  - db.jobs_pause helpers (read/write/clear/expiry).
  - worker.job_runner short-circuits scheduled (no
    triggered_by) calls while pause is active, but allows
    manual (triggered_by != None) calls through.
  - /api/jobs returns pause_until.
  - POST /api/admin/jobs/pause sets the timestamp.
  - DELETE /api/admin/jobs/pause clears it.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


# ────────────────────────────────────────────────
# helper-level
# ────────────────────────────────────────────────

def test_helpers_default_unpaused(clean_db):
    from db.session import get_db
    from db.jobs_pause import (
        get_jobs_paused_until, is_paused,
    )
    with get_db() as db:
        assert get_jobs_paused_until(db) is None
        assert is_paused(db) is False


def test_helpers_set_then_get(clean_db):
    from db.session import get_db
    from db.jobs_pause import (
        set_jobs_paused_until, get_jobs_paused_until,
        is_paused,
    )
    with get_db() as db:
        until = set_jobs_paused_until(db, 30)
    with get_db() as db:
        got = get_jobs_paused_until(db)
        assert got is not None
        # Within 1 second of the original (DB roundtrip).
        assert abs((got - until).total_seconds()) < 1
        assert is_paused(db) is True


def test_helpers_clear(clean_db):
    from db.session import get_db
    from db.jobs_pause import (
        set_jobs_paused_until, clear_jobs_pause,
        get_jobs_paused_until,
    )
    with get_db() as db:
        set_jobs_paused_until(db, 30)
    with get_db() as db:
        clear_jobs_pause(db)
    with get_db() as db:
        assert get_jobs_paused_until(db) is None


def test_helpers_past_timestamp_treated_as_unpaused(clean_db):
    """A stale (past) row must read as not-paused so a
    forgotten pause doesn't become permanent."""
    from db.session import get_db
    from db.models import AdminConfig
    from db.jobs_pause import (
        JOBS_PAUSED_UNTIL_KEY, get_jobs_paused_until,
    )
    past = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat()
    with get_db() as db:
        db.add(AdminConfig(
            key=JOBS_PAUSED_UNTIL_KEY, value=past,
        ))
    with get_db() as db:
        assert get_jobs_paused_until(db) is None


def test_helpers_reject_zero_or_negative(clean_db):
    from db.session import get_db
    from db.jobs_pause import set_jobs_paused_until
    with get_db() as db:
        with pytest.raises(ValueError):
            set_jobs_paused_until(db, 0)
        with pytest.raises(ValueError):
            set_jobs_paused_until(db, -5)


# ────────────────────────────────────────────────
# job_runner short-circuit
# ────────────────────────────────────────────────

def test_scheduled_job_skipped_when_paused(clean_db):
    """A wrapped job called with triggered_by=None (the
    scheduler path) must short-circuit and NOT invoke the
    underlying fn or write a JobRun row."""
    from db.session import get_db
    from db.models import JobRun
    from db.jobs_pause import set_jobs_paused_until
    from worker.job_runner import job

    with get_db() as db:
        set_jobs_paused_until(db, 60)

    calls = []
    wrapped = job("test_job", lambda: calls.append("ran"))
    out = wrapped()
    assert calls == [], "fn must not be invoked while paused"
    assert isinstance(out, dict)
    assert out.get("skipped") is True

    # No JobRun row should have been created (we skipped the
    # whole wrapper body).
    with get_db() as db:
        assert db.query(JobRun).filter(
            JobRun.job_name == "test_job"
        ).count() == 0


def test_manual_job_bypasses_pause(clean_db):
    """When triggered_by != None (the UI 'Run now' path) the
    pause is bypassed — admin chose to override."""
    from db.session import get_db
    from db.models import JobRun
    from db.jobs_pause import set_jobs_paused_until
    from worker.job_runner import job

    with get_db() as db:
        set_jobs_paused_until(db, 60)

    calls = []
    def fn():
        calls.append("ran")
        return "ok"
    wrapped = job("manual_job", fn)
    wrapped(triggered_by="admin@test.com")
    assert calls == ["ran"]
    with get_db() as db:
        rows = db.query(JobRun).filter(
            JobRun.job_name == "manual_job"
        ).all()
        assert len(rows) == 1
        assert rows[0].status == "succeeded"
        assert rows[0].triggered_by == "admin@test.com"


def test_scheduled_job_runs_after_pause_expires(clean_db):
    from db.session import get_db
    from db.models import JobRun, AdminConfig
    from db.jobs_pause import JOBS_PAUSED_UNTIL_KEY
    from worker.job_runner import job

    # Seed a stale pause directly.
    past = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    with get_db() as db:
        db.add(AdminConfig(
            key=JOBS_PAUSED_UNTIL_KEY, value=past,
        ))

    calls = []
    wrapped = job("expired_job", lambda: calls.append("ran"))
    wrapped()
    assert calls == ["ran"]
    with get_db() as db:
        assert db.query(JobRun).filter(
            JobRun.job_name == "expired_job"
        ).count() == 1


# ────────────────────────────────────────────────
# API surface
# ────────────────────────────────────────────────

@pytest.fixture
def client(pg_url, clean_db):
    """Reuse the session-scoped pg_url fixture from conftest
    so we don't shut down the testcontainer mid-suite. The
    `clean_db` fixture truncates state between tests in this
    file."""
    import api.auth as auth_mod
    auth_mod._validate_request = (
        lambda req, db: ("admin@test.com", "session")
    )
    from api.main import app
    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        if not db.query(AdminRole).filter(
            AdminRole.email == "admin@test.com"
        ).first():
            db.add(AdminRole(
                email="admin@test.com", role="org_admin",
            ))
    with TestClient(app) as c:
        yield c


def test_api_get_jobs_includes_pause_until_null(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    body = r.json()
    assert "pause_until" in body
    assert body["pause_until"] is None


def test_api_pause_sets_until_then_clear(client):
    r = client.post(
        "/api/admin/jobs/pause", json={"minutes": 30},
    )
    assert r.status_code == 200, r.text
    pu = r.json()["pause_until"]
    assert pu is not None
    # GET reflects it.
    r = client.get("/api/jobs")
    assert r.json()["pause_until"] == pu
    # DELETE clears.
    r = client.delete("/api/admin/jobs/pause")
    assert r.status_code == 200
    assert r.json()["pause_until"] is None
    r = client.get("/api/jobs")
    assert r.json()["pause_until"] is None


def test_api_pause_rejects_invalid_minutes(client):
    r = client.post(
        "/api/admin/jobs/pause", json={"minutes": -1},
    )
    assert r.status_code == 400
    r = client.post(
        "/api/admin/jobs/pause", json={"minutes": 0},
    )
    assert r.status_code == 400
    r = client.post(
        "/api/admin/jobs/pause", json={"minutes": 99999},
    )
    assert r.status_code == 400  # >24h cap
