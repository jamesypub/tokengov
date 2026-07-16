"""
Integration tests for the /api/integrations/jira router.

The /myself probe is patched out so tests don't hit the
network. We verify CRUD, scope enforcement, and that the
plaintext fallback is used when boto3 is unavailable.
"""
from __future__ import annotations
import json
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(pg_url, clean_db, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"),
    )

    # Patch the Jira /myself probe so we don't hit the network.
    import api.routes.integrations_jira as ij
    monkeypatch.setattr(
        ij, "_probe",
        lambda url, email, token: {
            "ok": True,
            "account_id": "acct-fake",
            "display_name": "Test Bot",
        },
    )
    # Force plaintext fallback path (no boto3).
    monkeypatch.setattr(ij, "_sm_client", lambda: None)

    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        db.add(AdminRole(
            email="admin@test.com", role="org_admin",
        ))

    from api.main import app
    with TestClient(app) as c:
        yield c


def _set_caller(monkeypatch, email: str):
    """Switch the auth dependency to a specific email mid-test."""
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: (email, "session"), raising=True,
    )


def test_add_list_delete_site(client):
    r = client.post(
        "/api/integrations/jira",
        json={
            "site_url":   "https://acme.atlassian.net",
            "auth_email": "ci@acme.com",
            "token":      "DUMMY",
            "projects":   ["PROJ", "DATA"],
        },
    )
    assert r.status_code == 201, r.text
    site = r.json()
    assert site["host"] == "acme.atlassian.net"
    assert site["projects"] == ["PROJ", "DATA"]
    assert site["token_storage"] == "plaintext"
    sid = site["id"]

    listed = client.get("/api/integrations/jira").json()
    assert any(s["id"] == sid for s in listed)

    rd = client.delete(f"/api/integrations/jira/{sid}")
    assert rd.status_code == 200
    listed2 = client.get("/api/integrations/jira").json()
    assert all(s["id"] != sid for s in listed2)


def test_duplicate_site_rejected(client):
    body = {
        "site_url":   "https://x.atlassian.net",
        "auth_email": "ci@x.com",
        "token":      "T",
        "projects":   ["X"],
    }
    assert client.post("/api/integrations/jira", json=body).status_code == 201
    r = client.post("/api/integrations/jira", json=body)
    assert r.status_code == 409


def test_invalid_url_rejected(client):
    r = client.post(
        "/api/integrations/jira",
        json={
            "site_url": "not-a-url",
            "auth_email": "ci@x.com",
            "token": "T",
            "projects": ["X"],
        },
    )
    assert r.status_code == 400


def test_member_forbidden(client, monkeypatch):
    # Add a site while the caller is admin.
    r = client.post(
        "/api/integrations/jira",
        json={
            "site_url": "https://m.atlassian.net",
            "auth_email": "ci@m.com",
            "token": "T",
            "projects": ["M"],
        },
    )
    sid = r.json()["id"]

    # Switch identity to a non-admin and re-issue the calls.
    _set_caller(monkeypatch, "user@test.com")
    assert client.get("/api/integrations/jira").status_code == 403
    assert client.post(
        "/api/integrations/jira",
        json={
            "site_url": "https://q.atlassian.net",
            "auth_email": "ci@q.com",
            "token": "T",
            "projects": ["Q"],
        },
    ).status_code == 403
    assert client.delete(
        f"/api/integrations/jira/{sid}",
    ).status_code == 403


def test_test_connection_returns_200(client):
    r = client.post(
        "/api/integrations/jira",
        json={
            "site_url": "https://t.atlassian.net",
            "auth_email": "ci@t.com",
            "token": "T",
            "projects": ["T"],
        },
    )
    sid = r.json()["id"]
    rt = client.post(f"/api/integrations/jira/{sid}/test")
    assert rt.status_code == 200
    body = rt.json()
    assert body["ok"] is True
    assert body["display_name"] == "Test Bot"


def test_patch_projects(client):
    r = client.post(
        "/api/integrations/jira",
        json={
            "site_url": "https://p.atlassian.net",
            "auth_email": "ci@p.com",
            "token": "T",
            "projects": ["A"],
        },
    )
    sid = r.json()["id"]
    rp = client.patch(
        f"/api/integrations/jira/{sid}",
        json={"projects": ["A", "B", "C"]},
    )
    assert rp.status_code == 200
    assert rp.json()["projects"] == ["A", "B", "C"]
