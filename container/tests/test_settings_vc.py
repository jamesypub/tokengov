"""
#1056: the vc_enabled runtime feature flag, surfaced + set via
/admin/config (admin_config kv store) and read on /api/whoami so
every role can hide the V&C nav item. Default OFF.
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def admin_client(pg_url, clean_db, monkeypatch):
    """TestClient with the caller resolved to a seeded org_admin
    (admin/config is org-admin-gated)."""
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


def test_vc_enabled_defaults_false(admin_client):
    """Fresh DB (no vc_enabled row) → GET reports false."""
    r = admin_client.get("/api/admin/config")
    assert r.status_code == 200
    assert r.json()["vc_enabled"] is False


def test_put_vc_enabled_roundtrips(admin_client):
    """PUT true persists; GET reflects it; PUT false clears."""
    r = admin_client.put(
        "/api/admin/config", json={"vc_enabled": True})
    assert r.status_code == 200
    assert r.json()["vc_enabled"] is True
    assert admin_client.get(
        "/api/admin/config").json()["vc_enabled"] is True

    r2 = admin_client.put(
        "/api/admin/config", json={"vc_enabled": False})
    assert r2.json()["vc_enabled"] is False
    assert admin_client.get(
        "/api/admin/config").json()["vc_enabled"] is False


def test_put_vc_enabled_rejects_non_bool(admin_client):
    """Non-boolean vc_enabled → 400, not a silent coerce."""
    r = admin_client.put(
        "/api/admin/config", json={"vc_enabled": "true"})
    assert r.status_code == 400


def test_vc_flag_independent_of_jira(admin_client):
    """Setting vc_enabled doesn't disturb jira_enabled, and
    vice-versa — the two experimental flags are independent."""
    admin_client.put(
        "/api/admin/config", json={"jira_enabled": True})
    admin_client.put(
        "/api/admin/config", json={"vc_enabled": True})
    body = admin_client.get("/api/admin/config").json()
    assert body["vc_enabled"] is True
    assert body["jira_enabled"] is True
    # flip vc off, jira stays on
    admin_client.put(
        "/api/admin/config", json={"vc_enabled": False})
    body = admin_client.get("/api/admin/config").json()
    assert body["vc_enabled"] is False
    assert body["jira_enabled"] is True


def test_whoami_surfaces_vc_enabled(admin_client):
    """whoami carries vc_enabled (every role reads it for the nav
    gate — /admin/config 403s for non-admins). Default false; ON
    after a flip."""
    assert admin_client.get(
        "/api/whoami").json()["vc_enabled"] is False
    admin_client.put(
        "/api/admin/config", json={"vc_enabled": True})
    assert admin_client.get(
        "/api/whoami").json()["vc_enabled"] is True
