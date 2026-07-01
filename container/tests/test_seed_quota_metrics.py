"""
Tests for the seed_quota_metrics internal job (#426): the
worker.jobs.seed_quota_metrics.run() seeder and the
/internal/run-job/seed_quota_metrics route (incl. the
spend_per_user query param + idempotency).
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


def _today():
    return datetime.now(timezone.utc).date()


def _seed_users(emails):
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        for e in emails:
            db.add(User(email=e))


def _spend(email):
    # #724: seeder now writes a cur_user_spend synthetic row for
    # the current hour (one per user). Query by email + synthetic
    # model — no time filter needed (single row per user).
    from db.session import get_db
    from db.models import CurUserSpend
    with get_db() as db:
        row = (
            db.query(CurUserSpend)
            .filter(
                CurUserSpend.email == email,
                CurUserSpend.model_id == "synthetic",
            )
            .first()
        )
        return row.spend_usd if row else None


def test_run_seeds_default_spend(pg_url, clean_db):
    from worker.jobs.seed_quota_metrics import run
    _seed_users(["a@test.com", "b@test.com"])
    out = run()
    assert out["seeded"] == 2
    assert _spend("a@test.com") == 0.50
    assert _spend("b@test.com") == 0.50


def test_run_idempotent_no_stacking(pg_url, clean_db):
    """Two runs overwrite, not stack (unique constraint on
    email,month,model_id)."""
    from worker.jobs.seed_quota_metrics import run
    from db.session import get_db
    from db.models import CurUserSpend
    _seed_users(["c@test.com"])
    run()
    run()
    assert _spend("c@test.com") == 0.50
    with get_db() as db:
        n = (
            db.query(CurUserSpend)
            .filter(
                CurUserSpend.email == "c@test.com",
                CurUserSpend.model_id == "synthetic",
            )
            .count()
        )
    assert n == 1


def test_run_custom_spend(pg_url, clean_db):
    from worker.jobs.seed_quota_metrics import run
    _seed_users(["d@test.com"])
    run(spend_per_user=1.25)
    assert _spend("d@test.com") == 1.25


@pytest.fixture
def client(pg_url, clean_db, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"),
    )
    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        db.add(AdminRole(
            email="admin@test.com", role="org_admin"))
    from api.main import app
    with TestClient(app) as c:
        yield c


def test_route_runs_and_seeds(client):
    _seed_users(["e@test.com", "f@test.com"])
    r = client.post(
        "/internal/run-job/seed_quota_metrics")
    assert r.status_code == 200
    assert r.json() == {
        "job": "seed_quota_metrics", "status": "ok"}
    assert _spend("e@test.com") == 0.50
    assert _spend("f@test.com") == 0.50


def test_route_respects_spend_per_user(client):
    _seed_users(["g@test.com"])
    r = client.post(
        "/internal/run-job/seed_quota_metrics"
        "?spend_per_user=1.00")
    assert r.status_code == 200
    assert _spend("g@test.com") == 1.00


def test_route_bad_spend_value_400(client):
    _seed_users(["h@test.com"])
    r = client.post(
        "/internal/run-job/seed_quota_metrics"
        "?spend_per_user=notanumber")
    assert r.status_code == 400
