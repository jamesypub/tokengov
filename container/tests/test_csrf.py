"""
CSRFMiddleware tests (#130).

Spins up a tiny FastAPI app with the middleware mounted so we
exercise the actual request/response path without needing
testcontainers or a DB.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.csrf import CSRFMiddleware


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.post("/api/users")
    def _post():
        return {"ok": True}

    @app.put("/api/users/x")
    def _put():
        return {"ok": True}

    @app.get("/api/whoami")
    def _whoami():
        return {"email": "x@y.com"}

    @app.post("/auth/logout")
    def _logout():
        return {"ok": True}

    return TestClient(app)


def test_get_passes_without_csrf(client):
    r = client.get("/api/whoami")
    assert r.status_code == 200


def test_aws4_header_no_longer_skips_csrf(client):
    """#581: the AWS4-HMAC-SHA256 skip was removed (desktop-SigV4
    affordance; the auth path it served was deleted in #576/#580).
    An AWS4 header is no longer special-cased — a cookie-session
    POST that carries it but no CSRF token is gated like any other
    (403), instead of being waved through on the header alone."""
    r = client.post(
        "/api/users", json={"email": "a@b.com"},
        cookies={"tg_session": "sid"},
        headers={"Authorization": "AWS4-HMAC-SHA256 ..."},
    )
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"]


def test_aws4_header_alone_falls_through(client):
    """An AWS4 header with NO cookies falls through the no-cookie
    branch (downstream auth rejects) — the header itself grants
    nothing. Mirrors test_unauthenticated_post_falls_through."""
    r = client.post(
        "/api/users", json={"email": "a@b.com"},
        headers={"Authorization": "AWS4-HMAC-SHA256 ..."},
    )
    assert r.status_code == 200


def test_post_with_session_no_csrf_returns_403(client):
    r = client.post(
        "/api/users", json={"email": "a@b.com"},
        cookies={"tg_session": "sid"},
    )
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"]


def test_post_with_matching_csrf_succeeds(client):
    r = client.post(
        "/api/users", json={"email": "a@b.com"},
        cookies={
            "tg_session": "sid",
            "tg_csrf": "TOK",
        },
        headers={"X-CSRF-Token": "TOK"},
    )
    assert r.status_code == 200


def test_post_with_mismatched_csrf_returns_403(client):
    r = client.post(
        "/api/users", json={"email": "a@b.com"},
        cookies={
            "tg_session": "sid",
            "tg_csrf": "TOK",
        },
        headers={"X-CSRF-Token": "DIFFERENT"},
    )
    assert r.status_code == 403


def test_logout_is_exempt(client):
    # Browsers can post to /auth/logout without first calling
    # /api/csrf — the route invalidates the session, that's the
    # whole point. CSRF check would only get in the way.
    r = client.post(
        "/auth/logout",
        cookies={"tg_session": "sid"},
    )
    assert r.status_code == 200


def test_unauthenticated_post_falls_through_to_auth(client):
    # No session cookie + no CSRF → middleware passes; the
    # downstream auth handler is what should reject this.
    # The dummy app accepts it, which proves middleware
    # didn't block.
    r = client.post(
        "/api/users", json={"email": "a@b.com"},
    )
    assert r.status_code == 200
