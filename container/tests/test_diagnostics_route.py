"""GET /api/diagnostics route tests: org_admin scoping + filters.

Uses the shared conftest `pg_url` session container (NOT a competing
one) so the global db.session engine is bound once for the whole test
session — a private container here would leave a dead engine bound when
it tore down, breaking later suites. Auth is stubbed via monkeypatch
(function-scoped, auto-reverting). These assert the ROUTE contract
(auth scoping, wire shape, filters), not the check verdicts (unit tests
cover those).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(pg_url, clean_db, monkeypatch):
    from db.session import get_db
    from db.models import AdminRole

    with get_db() as db:
        db.add(AdminRole(email="admin@test.com", role="org_admin"))
        db.add(AdminRole(email="member@test.com", role="team_admin"))

    holder = {"email": "nobody@test.com"}
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: (holder["email"], "session"))

    from api.main import app

    def _as(email):
        holder["email"] = email
        return TestClient(app)

    return _as


def test_diagnostics_requires_org_admin(client):
    # A caller with no org_admin admin_roles row → 403.
    r = client("nobody@test.com").get("/api/diagnostics")
    assert r.status_code == 403


def test_diagnostics_org_admin_gets_wire_object(client):
    r = client("admin@test.com").get("/api/diagnostics")
    assert r.status_code == 200
    body = r.json()
    for k in ("schema_version", "generated_at", "summary", "checks"):
        assert k in body
    assert isinstance(body["checks"], list) and body["checks"]
    for c in body["checks"]:
        for f in ("id", "title", "status", "category", "severity",
                  "detail", "remediation", "checked_at"):
            assert f in c


def test_diagnostics_only_governance_filters(client):
    r = client("admin@test.com").get("/api/diagnostics?only=governance")
    assert r.status_code == 200
    cats = {c["category"] for c in r.json()["checks"]}
    assert cats == {"governance"}


def test_diagnostics_skip_cur_pipeline(client):
    r = client("admin@test.com").get("/api/diagnostics?skip=cur_pipeline")
    assert r.status_code == 200
    cats = {c["category"] for c in r.json()["checks"]}
    assert "cur_pipeline" not in cats
