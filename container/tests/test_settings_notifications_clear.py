"""
PUT /api/settings/notifications must offer an explicit way to CLEAR a
stored SMTP password. A blank/absent `smtp_password` stays
keep-existing (never clobbers the secret); an explicit
`clear_smtp_password: true` removes it. Org-admin gated.

Route-level test (mirrors test_blocked_models.py): asserts the
`smtp_password_configured` boolean in the GET/PUT response shape — the
password value itself is never echoed.
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
        db.add(AdminRole(
            email="admin@test.com", role="org_admin"))
    from api.main import app
    with TestClient(app) as c:
        yield c


def _set_password(client) -> None:
    """Configure a host + password so smtp_password_configured is true."""
    r = client.put(
        "/api/settings/notifications",
        json={"smtp_host": "smtp.relay", "smtp_from": "a@x.test",
              "smtp_password": "s3cret"})
    assert r.status_code == 200, r.text
    assert r.json()["smtp_password_configured"] is True


def test_blank_password_keeps_existing(admin_client):
    """The keep-existing contract: a PUT with no/blank password leaves
    the stored credential intact (regression guard)."""
    _set_password(admin_client)
    r = admin_client.put(
        "/api/settings/notifications",
        json={"smtp_host": "smtp.relay2"})  # no password field
    assert r.status_code == 200, r.text
    assert r.json()["smtp_password_configured"] is True
    # An explicit blank string also keeps it.
    r = admin_client.put(
        "/api/settings/notifications",
        json={"smtp_password": "   "})
    assert r.json()["smtp_password_configured"] is True


def test_clear_flag_removes_stored_password(admin_client):
    """clear_smtp_password: true removes the stored credential."""
    _set_password(admin_client)
    r = admin_client.put(
        "/api/settings/notifications",
        json={"clear_smtp_password": True})
    assert r.status_code == 200, r.text
    assert r.json()["smtp_password_configured"] is False
    # Durable: a fresh GET still shows it cleared.
    got = admin_client.get("/api/settings/notifications").json()
    assert got["smtp_password_configured"] is False


def test_clear_wins_over_blank_password(admin_client):
    """When clear is set, a (blank) smtp_password in the same body does
    not resurrect keep-existing — the clear takes precedence."""
    _set_password(admin_client)
    r = admin_client.put(
        "/api/settings/notifications",
        json={"clear_smtp_password": True, "smtp_password": ""})
    assert r.json()["smtp_password_configured"] is False


def test_clear_falsey_is_noop(admin_client):
    """A falsey clear flag does nothing — the password is kept."""
    _set_password(admin_client)
    r = admin_client.put(
        "/api/settings/notifications",
        json={"clear_smtp_password": False})
    assert r.json()["smtp_password_configured"] is True
