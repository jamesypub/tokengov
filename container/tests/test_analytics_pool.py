"""
Tests for the /api/analytics/run DB-connection lifecycle.

A Cost-Report run polls Athena synchronously for up to ~55s. The bug:
the handler held its pooled DB connection across that wait, so a few
overlapping runs (or the SPA firing data calls while one polls) drained
the pool → QueuePool TimeoutError on every DB-backed route → 502. The
fix scopes the session to ONLY the fast cache read + cache write and
releases the connection BEFORE the Athena poll loop.

These assert the invariant directly: while the (mocked) Athena poll
runs, ZERO pooled connections are checked out.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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
        db.add(AdminRole(email="admin@test.com", role="org_admin"))

    from api.main import app
    with TestClient(app) as c:
        yield c


def test_run_holds_no_db_connection_during_athena_poll(client, monkeypatch):
    """While the handler polls Athena, the pool has zero checked-out
    connections — proof the request-scoped session was released before
    the poll (the connection-leak fix)."""
    import api.routes.analytics as an
    from db.session import engine

    monkeypatch.setattr(an, "ATHENA_RESULTS_BUCKET", "s3://results/")

    checkedout_during_poll = []

    class _FakeAthena:
        def get_named_query(self, NamedQueryId):
            return {"NamedQuery": {"QueryString": "SELECT 1"}}

        def start_query_execution(self, **kw):
            return {"QueryExecutionId": "exec-1"}

        def get_query_execution(self, QueryExecutionId):
            # The poll runs here — capture the live checked-out count.
            # If the handler still held its request session, this would
            # be >= 1; the fix makes it 0.
            checkedout_during_poll.append(engine.pool.checkedout())
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

        def get_query_results(self, QueryExecutionId, MaxResults=1000):
            return {"ResultSet": {"Rows": [
                {"Data": [{"VarCharValue": "model"},
                          {"VarCharValue": "actual_usd"}]},
                {"Data": [{"VarCharValue": "us.anthropic.x"},
                          {"VarCharValue": "1.23"}]},
            ]}}

    class _FakeSession:
        def client(self, name):
            return _FakeAthena()

    monkeypatch.setattr(an, "get_aws_session", lambda: _FakeSession())

    r = client.post("/api/analytics/run",
                    json={"query_id": "q1", "refresh": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 1
    assert body["columns"] == ["model", "actual_usd"]

    # The poll ran at least once, and EVERY poll observation saw zero
    # checked-out pooled connections.
    assert checkedout_during_poll, "Athena poll never ran"
    assert all(n == 0 for n in checkedout_during_poll), (
        f"DB connection held during the Athena poll: "
        f"checkedout={checkedout_during_poll}")


def test_run_persists_result_to_cache_after_poll(client, monkeypatch):
    """The result is still written to AnalyticsCache (in a fresh
    short-lived session after the poll) — a second non-refresh run
    returns the cached payload without re-polling Athena."""
    import api.routes.analytics as an

    monkeypatch.setattr(an, "ATHENA_RESULTS_BUCKET", "s3://results/")
    poll_calls = {"n": 0}

    class _FakeAthena:
        def get_named_query(self, NamedQueryId):
            return {"NamedQuery": {"QueryString": "SELECT 1"}}

        def start_query_execution(self, **kw):
            return {"QueryExecutionId": "exec-2"}

        def get_query_execution(self, QueryExecutionId):
            poll_calls["n"] += 1
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

        def get_query_results(self, QueryExecutionId, MaxResults=1000):
            return {"ResultSet": {"Rows": [
                {"Data": [{"VarCharValue": "model"}]},
                {"Data": [{"VarCharValue": "us.anthropic.x"}]},
            ]}}

    class _FakeSession:
        def client(self, name):
            return _FakeAthena()

    monkeypatch.setattr(an, "get_aws_session", lambda: _FakeSession())

    # First run (refresh) populates the cache.
    r1 = client.post("/api/analytics/run",
                     json={"query_id": "q2", "refresh": True})
    assert r1.status_code == 200
    assert poll_calls["n"] >= 1
    before = poll_calls["n"]

    # Second run (no refresh) is served from cache — Athena not polled.
    r2 = client.post("/api/analytics/run",
                     json={"query_id": "q2"})
    assert r2.status_code == 200
    assert r2.json().get("cached") is True
    assert poll_calls["n"] == before, "cache miss — Athena re-polled"
