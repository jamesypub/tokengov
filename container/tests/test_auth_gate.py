"""
Tests for api/auth_gate.py — the SPA + docs gate that
prevents unauthenticated access to the admin UI when
TG_AUTH_REQUIRE_LOGIN=1 (#183, AppSec V2226500622).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def gate_on(monkeypatch):
    """Enable the auth gate for the duration of the test
    and disable test-bypass so we exercise the real path."""
    monkeypatch.setenv("TG_AUTH_REQUIRE_LOGIN", "1")
    monkeypatch.delenv("TG_AUTH_TEST_TRUST", raising=False)


@pytest.fixture
def app_client(pg_url, clean_db, gate_on):
    from api.main import app
    with TestClient(app) as c:
        yield c


def test_gate_off_by_default(pg_url, clean_db, monkeypatch):
    """When TG_AUTH_REQUIRE_LOGIN is unset, the gate is a
    no-op — local dev / docker-compose users keep their
    open admin UI."""
    monkeypatch.delenv(
        "TG_AUTH_REQUIRE_LOGIN", raising=False)
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
    # No SPA bundle in test env so / 404s; the important
    # part is that we did NOT 302 to the login page.
    assert r.status_code != 302
    assert r.headers.get("location", "") != "/login"


def test_anonymous_root_redirects_to_login(app_client):
    """Anonymous GET / when the gate is on must 302 to
    the branded SPA /login page (#193). This is the
    AppSec-flagged surface."""
    r = app_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_anonymous_spa_path_redirects(app_client):
    """SPA fallback paths (e.g. /users) also redirect."""
    r = app_client.get(
        "/users", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_anonymous_docs_redirects(app_client):
    """/docs (Swagger) was previously also exposed
    unauthenticated. Must 302 too."""
    r = app_client.get("/docs", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_anonymous_openapi_redirects(app_client):
    r = app_client.get(
        "/openapi.json", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_anonymous_api_returns_401_json(app_client):
    """Anonymous /api/* must NOT redirect — XHR clients
    handle JSON 401 cleanly; a 302→HTML breaks fetch()."""
    r = app_client.get(
        "/api/users", follow_redirects=False)
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == "login_required"


def test_version_public(app_client):
    """/api/version stays public — used as an unauth
    health probe (curl, ALB target group, etc.)."""
    r = app_client.get(
        "/api/version", follow_redirects=False)
    assert r.status_code == 200


def test_csrf_public(app_client):
    """/api/csrf stays public — SPA needs it before
    the user has logged in."""
    r = app_client.get(
        "/api/csrf", follow_redirects=False)
    assert r.status_code == 200


def test_auth_login_public(app_client):
    """The login endpoint itself must always be reachable.
    OIDC isn't configured in tests so we expect 501 — the
    point is the gate didn't pre-empt it with a 302."""
    r = app_client.get(
        "/auth/login", follow_redirects=False)
    assert r.status_code in (302, 501)


def test_login_page_public(app_client):
    """/login is the branded SPA landing card (#193) and
    must render anonymously — that's the whole point of
    the redirect target."""
    r = app_client.get("/login", follow_redirects=False)
    # No SPA bundle in test env → 404. With bundle → 200.
    # The point is the gate did NOT pre-empt with a 302.
    assert r.status_code != 302


def test_assets_public(app_client):
    """SPA static assets must load on the anonymous /login
    page (#193). Without this, the React bundle that
    renders the branded card is itself 302'd to /login —
    a redirect loop with no visible page."""
    r = app_client.get(
        "/assets/index-fake.js", follow_redirects=False)
    # No bundle in test → 404. The gate did NOT 302.
    assert r.status_code != 302


def test_session_cookie_passes_through(app_client):
    """A request carrying a tg_session cookie bypasses the
    gate. Whether the cookie is valid is the auth layer's
    concern, not the gate's."""
    from db.session import get_db
    from db.models import WebSession
    sid = "test-session-xyz"
    with get_db() as db:
        db.add(WebSession(
            id=sid,
            email="admin@test.com",
            expires_at=datetime.now(timezone.utc)
                + timedelta(hours=1),
        ))
    app_client.cookies.set("tg_session", sid)
    r = app_client.get("/", follow_redirects=False)
    # Either 200 (SPA bundle present) or 404 (no bundle
    # in test env) — but never 302 to login.
    assert r.status_code != 302


def test_aws4_header_alone_is_gated(app_client):
    """#581: an AWS4-HMAC-SHA256 Authorization header NO LONGER
    skips the login gate. The SigV4 validator behind it
    (auth._validate_sigv4) was deleted with the desktop client in
    #576/#580, so the gate-pass was a dead affordance — an AWS4
    header is now just an unauthenticated request. With no
    test-email and no cookie it must be GATED: the /api/* path
    returns the 401 login_required JSON (not a pass-through, not a
    302).

    NB: the test-trust scripts/playwright send AWS4 + X-Tg-Test-Email
    together; they still pass via _is_test_bypass (covered by
    test_test_bypass_still_works), NOT via this dead SigV4 path."""
    r = app_client.get(
        "/api/users",
        headers={"Authorization":
                 "AWS4-HMAC-SHA256 Credential=fake"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert r.json().get("code") == "login_required"


def test_test_bypass_still_works(
    pg_url, clean_db, monkeypatch
):
    """When TG_AUTH_TEST_TRUST=1 + X-Tg-Test-Email is
    sent, the gate must let the request through — this
    is the local-dev / playwright path."""
    monkeypatch.setenv("TG_AUTH_REQUIRE_LOGIN", "1")
    monkeypatch.setenv("TG_AUTH_TEST_TRUST", "1")

    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        if not db.query(AdminRole).filter(
                AdminRole.email == "admin@test.com").first():
            db.add(AdminRole(
                email="admin@test.com", role="org_admin"))

    from api.main import app
    with TestClient(app) as c:
        r = c.get(
            "/api/users",
            headers={"X-Tg-Test-Email": "admin@test.com"},
            follow_redirects=False,
        )
    assert r.status_code == 200
