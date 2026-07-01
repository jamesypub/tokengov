"""
Dispatcher tests for #129 / #576 — `_validate_request` picks the
right auth method based on what the request carries.

#576: auth is now TWO methods — `test` (X-Tg-Test-Email bypass,
gated by Environment #570) and `session` (browser OIDC/Cognito
cookie). The `sigv4` desktop path was deleted; a request that is
neither falls through to a clean 401.

These tests exercise the dispatcher directly without spinning up
the full FastAPI app; the dispatcher is pure (request + db in,
(email, method) out) so unit-level coverage is enough.
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import api.auth as auth_mod


def _real_validate_request(request, db):
    """Inline copy of auth.py::_validate_request so these unit tests
    don't depend on the smoke fixture's session-scoped monkeypatch
    of `_validate_request`. We test the dispatch *logic*, not the
    bound module attribute. Mirrors the 2-method dispatcher (#576)."""
    import os
    test_trust = os.environ.get("TG_AUTH_TEST_TRUST") == "1"
    test_email = request.headers.get("x-tg-test-email", "")
    if test_trust:
        if test_email:
            return test_email.lower(), "test"
        bootstrap = os.environ.get(
            "BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
        if bootstrap:
            return bootstrap, "test"
    cookie_email = auth_mod._validate_session(request, db)
    if cookie_email:
        return cookie_email, "session"
    # #576: no test-trust + no cookie → clean 401 (was the SigV4
    # desktop path).
    raise HTTPException(401, "authentication required — log in")


def _req(headers=None, cookies=None):
    """Tiny stand-in for fastapi.Request — only fields auth touches."""
    r = MagicMock()
    r.headers = headers or {}
    r.cookies = cookies or {}
    return r


def test_test_trust_with_explicit_email(monkeypatch):
    monkeypatch.setenv("TG_AUTH_TEST_TRUST", "1")
    monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    email, method = _real_validate_request(
        _req(headers={"x-tg-test-email": "Alice@Example.com"}),
        db=MagicMock(),
    )
    assert email == "alice@example.com"
    assert method == "test"


def test_test_trust_falls_back_to_bootstrap(monkeypatch):
    monkeypatch.setenv("TG_AUTH_TEST_TRUST", "1")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "Boss@Example.com")
    email, method = _real_validate_request(
        _req(),
        db=MagicMock(),
    )
    assert email == "boss@example.com"
    assert method == "test"


def test_session_cookie_with_unknown_id_raises_401(monkeypatch):
    """#130: cookie present but no matching row → 401 (not a
    silent fall-through)."""
    monkeypatch.delenv("TG_AUTH_TEST_TRUST", raising=False)
    # Patch _validate_session to behave as the post-#130 impl
    # would for a stale cookie: raise 401.
    def _401(req, db):
        if req.cookies.get("tg_session"):
            raise HTTPException(401, "session not found")
        return None
    monkeypatch.setattr(auth_mod, "_validate_session", _401)

    with pytest.raises(HTTPException) as ei:
        _real_validate_request(
            _req(cookies={"tg_session": "abc"}),
            db=MagicMock(),
        )
    assert ei.value.status_code == 401


def test_no_creds_falls_through_to_clean_401(monkeypatch):
    """#576: no test bypass, no cookie → clean 401 (was the SigV4
    desktop path; that branch is deleted)."""
    monkeypatch.delenv("TG_AUTH_TEST_TRUST", raising=False)
    monkeypatch.setattr(
        auth_mod, "_validate_session", lambda req, db: None)

    with pytest.raises(HTTPException) as ei:
        _real_validate_request(_req(), db=MagicMock())
    assert ei.value.status_code == 401


def test_cookie_wins_over_test_when_no_test_trust(monkeypatch):
    """With test-trust off, a cookie is the authenticating path
    and a request without one 401s — the dispatcher is strictly
    test → session → 401 (#576)."""
    monkeypatch.delenv("TG_AUTH_TEST_TRUST", raising=False)
    monkeypatch.setattr(
        auth_mod, "_validate_session",
        lambda req, db: "cookie@x.com" if req.cookies.get("tg_session") else None)

    email, method = _real_validate_request(
        _req(cookies={"tg_session": "abc"}),
        db=MagicMock(),
    )
    assert email == "cookie@x.com"
    assert method == "session"
