"""
#447: the jira_enabled runtime feature flag, surfaced + set
via /admin/config (the admin_config kv store). Default OFF.
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def admin_client(pg_url, clean_db, monkeypatch):
    """TestClient with the caller resolved to a seeded
    org_admin (admin/config is org-admin-gated)."""
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"))
    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        db.add(AdminRole(
            email="admin@test.com", role="org_admin"))
    from api.main import app
    with TestClient(app) as c:
        yield c


def test_jira_enabled_defaults_false(admin_client):
    """Fresh DB (no jira_enabled row) → GET reports false."""
    r = admin_client.get("/api/admin/config")
    assert r.status_code == 200
    assert r.json()["jira_enabled"] is False


def test_put_jira_enabled_roundtrips(admin_client):
    """PUT true persists; GET reflects it; PUT false clears."""
    r = admin_client.put(
        "/api/admin/config", json={"jira_enabled": True})
    assert r.status_code == 200
    assert r.json()["jira_enabled"] is True
    assert admin_client.get(
        "/api/admin/config").json()["jira_enabled"] is True

    r2 = admin_client.put(
        "/api/admin/config", json={"jira_enabled": False})
    assert r2.json()["jira_enabled"] is False
    assert admin_client.get(
        "/api/admin/config").json()["jira_enabled"] is False


def test_put_jira_enabled_rejects_non_bool(admin_client):
    """Non-boolean jira_enabled → 400, not a silent coerce."""
    r = admin_client.put(
        "/api/admin/config", json={"jira_enabled": "true"})
    assert r.status_code == 400


def test_jira_flag_independent_of_quota(admin_client):
    """Setting jira_enabled doesn't disturb the existing
    org_default_quota_usd key, and vice-versa."""
    admin_client.put(
        "/api/admin/config", json={"org_default_quota_usd": 250})
    admin_client.put(
        "/api/admin/config", json={"jira_enabled": True})
    body = admin_client.get("/api/admin/config").json()
    assert body["jira_enabled"] is True
    assert body["org_default_quota_usd"] == 250
