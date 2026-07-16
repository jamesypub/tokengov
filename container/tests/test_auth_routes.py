"""
Tests for api/auth_routes.py — the Okta OIDC browser flow
(#130). The pure-OIDC helpers are covered in test_oidc.py;
these tests cover the route surface: cookie handling, state
validation, the admin-only gate after token exchange, the
bootstrap admin path, and the 501 when OIDC is unconfigured.
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient

from api import oidc as oidc_mod


def _cfg():
    return oidc_mod.OIDCConfig(
        issuer="https://test.okta.com",
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://tg.test/auth/callback",
        authorization_endpoint=(
            "https://test.okta.com/oauth2/v1/authorize"),
        token_endpoint=(
            "https://test.okta.com/oauth2/v1/token"),
        jwks_uri="https://test.okta.com/oauth2/v1/keys",
    )


@pytest.fixture
def app_client(pg_url, clean_db):
    """Plain TestClient, no SigV4 patching — these tests
    exercise the unauthenticated /auth/* endpoints."""
    from api.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def oidc_configured(monkeypatch):
    """Wire api.auth_routes to a fake OIDC config so
    /auth/login and /auth/callback don't 501."""
    import api.auth_routes as ar
    monkeypatch.setattr(ar, "_CFG", _cfg())
    return ar


def test_login_unconfigured_returns_501(app_client):
    """If TG_OIDC_* env vars are unset and no _CFG cached,
    /auth/login must return 501 (not 500) so misconfig is
    obvious."""
    import api.auth_routes as ar
    # Force re-discovery from env (which is unset in CI):
    ar._CFG = None
    r = app_client.get(
        "/auth/login", follow_redirects=False)
    assert r.status_code == 501


def test_login_sets_state_and_pkce_cookies(
    app_client, oidc_configured
):
    """/auth/login redirects to Okta and seeds the two
    short-lived cookies the callback consumes."""
    r = app_client.get(
        "/auth/login", follow_redirects=False)
    assert r.status_code == 302
    assert "test.okta.com" in r.headers["location"]
    cookies = r.cookies
    assert "tg_oidc_state" in cookies
    assert "tg_pkce" in cookies


def test_login_redirect_skips_picker_cognito(
    app_client, oidc_configured, monkeypatch
):
    """Picker bypass (#197): when TG_OIDC_OKTA_PROVIDER_NAME is
    unset, /auth/login appends identity_provider=COGNITO so
    Cognito skips the Hosted UI picker and lands directly on
    the password form."""
    monkeypatch.delenv(
        "TG_OIDC_OKTA_PROVIDER_NAME", raising=False)
    r = app_client.get(
        "/auth/login", follow_redirects=False)
    assert r.status_code == 302
    assert "identity_provider=COGNITO" in r.headers["location"]


def test_login_redirect_skips_picker_okta(
    app_client, oidc_configured, monkeypatch
):
    """Picker bypass (#197): when an external IdP owns the directory
    (#926: tg_owns_directory false) AND TG_OIDC_OKTA_PROVIDER_NAME is
    set, /auth/login uses that federated name instead of COGNITO."""
    from db.session import get_db
    from db.org_config import set_tg_owns_directory
    with get_db() as db:
        set_tg_owns_directory(db, False)
    monkeypatch.setenv(
        "TG_OIDC_OKTA_PROVIDER_NAME", "MyOkta")
    r = app_client.get(
        "/auth/login", follow_redirects=False)
    assert r.status_code == 302
    assert "identity_provider=MyOkta" in r.headers["location"]


def test_login_routes_to_runtime_saml_even_when_owns_true(
    app_client, oidc_configured, monkeypatch
):
    """Picker-bounce regression: a runtime SAML IdP configured from
    Settings must route the SSO hand-off to that provider EVEN WHEN
    tg_owns_directory is still true — otherwise /auth/login handed off
    identity_provider=COGNITO and Cognito re-showed its picker instead
    of proceeding to the IdP. Matches /auth/providers' `federated`
    logic."""
    _set_owns_directory(True)          # owns stays true...
    _set_saml_provider("CompanyIdc")   # ...but a runtime SAML name is set
    r = app_client.get(
        "/auth/login", follow_redirects=False)
    assert r.status_code == 302
    # Routes to the SAML provider (picker-bypass), NOT COGNITO.
    assert "identity_provider=CompanyIdc" in r.headers["location"]
    assert "identity_provider=COGNITO" not in r.headers["location"]


def test_login_explicit_identity_provider_override(
    app_client, oidc_configured, monkeypatch
):
    """Caller can override via ?identity_provider=… — used by
    the SPA when Okta is wired but the user wants the COGNITO
    bootstrap-admin password path."""
    monkeypatch.setenv(
        "TG_OIDC_OKTA_PROVIDER_NAME", "MyOkta")
    r = app_client.get(
        "/auth/login",
        params={"identity_provider": "COGNITO"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "identity_provider=COGNITO" in r.headers["location"]
    assert "identity_provider=MyOkta" not in r.headers["location"]


def test_callback_state_mismatch_rejected(
    app_client, oidc_configured
):
    """Wrong state cookie → 400. Defends against CSRF /
    replay."""
    app_client.cookies.set("tg_oidc_state", "expected")
    app_client.cookies.set("tg_pkce", "verifier")
    r = app_client.get(
        "/auth/callback",
        params={"code": "x", "state": "WRONG"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "state mismatch" in r.json()["detail"]


def test_callback_missing_pkce_rejected(
    app_client, oidc_configured
):
    """state matches but PKCE verifier cookie was lost
    (e.g. session expired) → 400, not a confusing 500
    from the token exchange."""
    app_client.cookies.set("tg_oidc_state", "s1")
    r = app_client.get(
        "/auth/callback",
        params={"code": "c1", "state": "s1"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "PKCE" in r.json()["detail"]


def test_callback_okta_error_propagated(
    app_client, oidc_configured
):
    """If Okta sends ?error=… on the callback, route
    surfaces it as 400 (not 500)."""
    r = app_client.get(
        "/auth/callback",
        params={"error": "access_denied"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_callback_non_admin_rejected(
    app_client, oidc_configured, monkeypatch
):
    """Token exchange OK, id_token verifies, email is
    valid — but the email is NOT in admin_roles. Must
    return 403, not silently mint a session."""
    import api.auth_routes as ar

    monkeypatch.setattr(ar.oidc_mod, "exchange_code",
        lambda *a, **kw: {
            "id_token": "fake", "refresh_token": "rt"})
    monkeypatch.setattr(ar, "_get_jwks", lambda cfg: {})
    monkeypatch.setattr(ar.oidc_mod, "verify_id_token",
        lambda tok, cfg, jwks: {"email": "outsider@test.com"})

    app_client.cookies.set("tg_oidc_state", "s1")
    app_client.cookies.set("tg_pkce", "v1")
    r = app_client.get(
        "/auth/callback",
        params={"code": "c1", "state": "s1"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "not registered" in r.json()["detail"]


def test_callback_bootstrap_seeds_first_admin(
    app_client, oidc_configured, monkeypatch
):
    """If admin_roles is empty AND the email matches
    BOOTSTRAP_ADMIN_EMAIL, callback seeds an org_admin
    row for them and finishes the login successfully."""
    import api.auth_routes as ar

    monkeypatch.setenv(
        "BOOTSTRAP_ADMIN_EMAIL", "first@test.com")
    monkeypatch.setattr(ar.oidc_mod, "exchange_code",
        lambda *a, **kw: {"id_token": "fake"})
    monkeypatch.setattr(ar, "_get_jwks", lambda cfg: {})
    monkeypatch.setattr(ar.oidc_mod, "verify_id_token",
        lambda tok, cfg, jwks: {"email": "first@test.com"})

    app_client.cookies.set("tg_oidc_state", "s1")
    app_client.cookies.set("tg_pkce", "v1")
    r = app_client.get(
        "/auth/callback",
        params={"code": "c1", "state": "s1"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    assert "tg_session" in r.cookies

    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        seeded = db.query(AdminRole).filter(
            AdminRole.email == "first@test.com",
            AdminRole.role == "org_admin",
        ).first()
        assert seeded is not None


def _set_owns_directory(value: bool):
    from db.session import get_db
    from db.org_config import set_tg_owns_directory
    with get_db() as db:
        set_tg_owns_directory(db, value)


def _grant(email: str, role: str):
    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        db.add(AdminRole(email=email, role=role))


def _set_saml_provider(name: str):
    """Mark SSO as the configured method (a SAML provider name set)."""
    from db.session import get_db
    from db.auth_config import set_saml_config
    with get_db() as db:
        set_saml_config(db, provider_name=name,
                        metadata_xml="<x/>", email_attribute="email")


# ── Gap 1: auth error redirects to the SPA login (not a dead 401) ──

def test_callback_missing_email_redirects_to_login_with_error(
    app_client, oidc_configured, monkeypatch
):
    """A SAML email-attribute mismatch → no email claim. Instead of a
    bare 401 dead-end, redirect to the SPA /login?error=... so the page
    shows the message + the recovery affordance (Gap 1)."""
    import api.auth_routes as ar
    monkeypatch.setenv(
        "TG_OIDC_REDIRECT_URI", "https://tg.test/auth/callback")
    monkeypatch.setattr(ar.oidc_mod, "exchange_code",
        lambda *a, **kw: {"id_token": "fake"})
    monkeypatch.setattr(ar, "_get_jwks", lambda cfg: {})
    monkeypatch.setattr(ar.oidc_mod, "verify_id_token",
        lambda tok, cfg, jwks: {})  # no email claim

    app_client.cookies.set("tg_oidc_state", "s1")
    app_client.cookies.set("tg_pkce", "v1")
    r = app_client.get(
        "/auth/callback",
        params={"code": "c1", "state": "s1"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://tg.test/login?error=")
    assert "email" in loc.lower()


# ── Org-admin gate on the break-glass recovery path ──

def test_recovery_password_path_blocks_member_when_sso_on(
    app_client, oidc_configured, monkeypatch
):
    """SSO is the configured method and a registered MEMBER signs in via
    the COGNITO password page (no federated `identities` claim). Recovery
    is org-admin-only → 403 (the member must use SSO)."""
    import api.auth_routes as ar
    _set_saml_provider("CompanyIdc")
    _grant("member@test.com", "team_admin")  # not org_admin
    monkeypatch.setattr(ar.oidc_mod, "exchange_code",
        lambda *a, **kw: {"id_token": "fake"})
    monkeypatch.setattr(ar, "_get_jwks", lambda cfg: {})
    monkeypatch.setattr(ar.oidc_mod, "verify_id_token",
        lambda tok, cfg, jwks: {"email": "member@test.com"})  # no identities

    app_client.cookies.set("tg_oidc_state", "s1")
    app_client.cookies.set("tg_pkce", "v1")
    r = app_client.get(
        "/auth/callback",
        params={"code": "c1", "state": "s1"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "org admin" in r.json()["detail"].lower()


def test_recovery_password_path_allows_org_admin_when_sso_on(
    app_client, oidc_configured, monkeypatch
):
    """Same setup but the recoverer is an org_admin → the break-glass
    password sign-in succeeds (session minted)."""
    import api.auth_routes as ar
    _set_saml_provider("CompanyIdc")
    _grant("boss@test.com", "org_admin")
    monkeypatch.setattr(ar.oidc_mod, "exchange_code",
        lambda *a, **kw: {"id_token": "fake"})
    monkeypatch.setattr(ar, "_get_jwks", lambda cfg: {})
    monkeypatch.setattr(ar.oidc_mod, "verify_id_token",
        lambda tok, cfg, jwks: {"email": "boss@test.com"})  # no identities

    app_client.cookies.set("tg_oidc_state", "s1")
    app_client.cookies.set("tg_pkce", "v1")
    r = app_client.get(
        "/auth/callback",
        params={"code": "c1", "state": "s1"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    assert "tg_session" in r.cookies


def test_federated_member_login_unaffected_when_sso_on(
    app_client, oidc_configured, monkeypatch
):
    """A MEMBER signing in via the IdP (federated → carries an
    `identities` claim) is admitted normally — the org-admin gate is
    only for the COGNITO password bypass, not federated logins."""
    import api.auth_routes as ar
    _set_saml_provider("CompanyIdc")
    _grant("fed@test.com", "team_admin")
    monkeypatch.setattr(ar.oidc_mod, "exchange_code",
        lambda *a, **kw: {"id_token": "fake"})
    monkeypatch.setattr(ar, "_get_jwks", lambda cfg: {})
    monkeypatch.setattr(ar.oidc_mod, "verify_id_token",
        lambda tok, cfg, jwks: {
            "email": "fed@test.com",
            "identities": [{"providerName": "CompanyIdc"}],
        })

    app_client.cookies.set("tg_oidc_state", "s1")
    app_client.cookies.set("tg_pkce", "v1")
    r = app_client.get(
        "/auth/callback",
        params={"code": "c1", "state": "s1"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_auth_providers_cognito_when_tg_owns_directory(app_client, monkeypatch):
    """#926: when tg owns the directory (default), /auth/providers
    reports Cognito on + provisioning on, federation off — even if a
    stale Okta env name is present (the DB flag is authoritative)."""
    monkeypatch.setenv("TG_OIDC_OKTA_PROVIDER_NAME", "MyOkta")
    monkeypatch.delenv("TG_OIDC_OKTA_DISPLAY_NAME", raising=False)
    _set_owns_directory(True)
    r = app_client.get("/auth/providers")
    assert r.status_code == 200
    assert r.json() == {
        "cognito": True,
        "okta": False,
        "okta_display_name": None,
        "cognito_provisioning": True,
    }


def test_auth_providers_federated_when_idp_external(app_client, monkeypatch):
    """#926: when an external IdP owns the directory (flag false) and
    the federated provider name is set, /auth/providers turns OFF
    Cognito + provisioning and surfaces the federated button. With no
    runtime label or display-name env, the button label falls back to
    the generic default 'Login with Your SSO'."""
    monkeypatch.setenv("TG_OIDC_OKTA_PROVIDER_NAME", "MyOkta")
    monkeypatch.delenv("TG_OIDC_OKTA_DISPLAY_NAME", raising=False)
    _set_owns_directory(False)
    r = app_client.get("/auth/providers")
    assert r.status_code == 200
    assert r.json() == {
        "cognito": False,
        "okta": True,
        "okta_display_name": "Login with Your SSO",
        "cognito_provisioning": False,
    }


def test_auth_providers_provisioning_tracks_db_flag(
    app_client, monkeypatch
):
    """#926: cognito_provisioning tracks the tg_owns_directory DB flag
    (config-as-data, runtime-editable), NOT TG_AUTH_PROVIDER env. The
    Admins panel gates its invite checkbox off this field."""
    monkeypatch.delenv("TG_OIDC_OKTA_PROVIDER_NAME", raising=False)
    _set_owns_directory(True)
    r = app_client.get("/auth/providers")
    assert r.status_code == 200
    assert r.json()["cognito_provisioning"] is True

    # Flip the DB flag — no redeploy, no env change.
    _set_owns_directory(False)
    r2 = app_client.get("/auth/providers")
    assert r2.json()["cognito_provisioning"] is False


def test_auth_providers_custom_display_name(
    app_client, monkeypatch
):
    """#226/#926: tenants label the federated button per-tenant via
    TG_OIDC_OKTA_DISPLAY_NAME — surfaced only when the IdP is external
    (flag false)."""
    monkeypatch.setenv(
        "TG_OIDC_OKTA_PROVIDER_NAME", "MyOkta")
    monkeypatch.setenv(
        "TG_OIDC_OKTA_DISPLAY_NAME", "Acme SSO")
    _set_owns_directory(False)
    r = app_client.get("/auth/providers")
    assert r.status_code == 200
    assert r.json()["okta_display_name"] == "Acme SSO"


def test_auth_providers_no_auth_required(app_client):
    """#226: /auth/providers must be reachable without a
    session cookie — the SPA fetches it on the login page,
    before login."""
    # No cookies set, no Authorization header.
    r = app_client.get("/auth/providers")
    assert r.status_code == 200


# ── runtime SAML provider name + label precedence ────────────────────


def _set_saml(name: str, label: str | None = None):
    from db.session import get_db
    from db.auth_config import set_saml_config, set_sso_button_label
    with get_db() as db:
        set_saml_config(db, provider_name=name,
                        metadata_url="https://idp/metadata")
        if label is not None:
            set_sso_button_label(db, label)


def test_auth_providers_runtime_saml_surfaces_button(
    app_client, monkeypatch
):
    """A SAML IdP configured at runtime (admin_config) surfaces the
    federated button — even without flipping tg_owns_directory or
    setting any env — and uses the runtime button label."""
    monkeypatch.delenv("TG_OIDC_OKTA_PROVIDER_NAME", raising=False)
    monkeypatch.delenv("TG_OIDC_OKTA_DISPLAY_NAME", raising=False)
    _set_saml("HpeIdc", label="Login with HPE SSO")
    r = app_client.get("/auth/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["okta"] is True
    assert body["cognito"] is False
    assert body["okta_display_name"] == "Login with HPE SSO"


def test_login_runtime_saml_name_beats_env(app_client, monkeypatch):
    """/auth/login routes to the runtime SAML provider name over a
    stale TG_OIDC_OKTA_PROVIDER_NAME env (DB-first precedence)."""
    monkeypatch.setenv("TG_OIDC_OKTA_PROVIDER_NAME", "StaleEnv")
    _set_owns_directory(False)
    _set_saml("RuntimeIdc")
    import api.auth_routes as ar
    monkeypatch.setattr(ar, "_CFG", _cfg())
    r = app_client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 302
    assert "identity_provider=RuntimeIdc" in r.headers["location"]
    assert "identity_provider=StaleEnv" not in r.headers["location"]


def test_csrf_endpoint_issues_token(app_client):
    """/api/csrf must return a token + Set-Cookie even
    without a session — the SPA fetches it before
    login."""
    r = app_client.get("/api/csrf")
    assert r.status_code == 200
    body = r.json()
    assert "csrf_token" in body
    assert body["csrf_token"]
    assert "tg_csrf" in r.cookies


# ── IdP single-logout via Cognito hosted logout ─────────────────────


def _set_saml_signout(name, signout):
    from db.session import get_db
    from db.auth_config import set_saml_config
    with get_db() as db:
        set_saml_config(db, provider_name=name,
                        metadata_url="https://idp/metadata",
                        idp_signout=signout)


def _seed_session(sid="sess-1", email="u@test.com"):
    from datetime import datetime, timezone, timedelta
    from db.session import get_db
    from db.models import WebSession
    with get_db() as db:
        db.add(WebSession(
            id=sid, email=email,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8)))


def test_logout_no_saml_returns_204(app_client):
    """No SAML configured → plain local logout (204), as today."""
    _seed_session("s204")
    r = app_client.post(
        "/auth/logout",
        cookies={"tg_session": "s204"},
        follow_redirects=False)
    assert r.status_code == 204
    # local session deleted
    from db.session import get_db
    from db.models import WebSession
    with get_db() as db:
        assert db.query(WebSession).filter(
            WebSession.id == "s204").first() is None


def test_logout_saml_signout_off_returns_204(app_client):
    """SAML configured but idp_signout OFF → still 204 (no IdP redirect)."""
    _set_saml_signout("HpeIdc", False)
    _seed_session("soff")
    r = app_client.post(
        "/auth/logout",
        cookies={"tg_session": "soff"},
        follow_redirects=False)
    assert r.status_code == 204


def test_logout_saml_signout_on_returns_logout_url(
    app_client, oidc_configured, monkeypatch
):
    """SAML + idp_signout ON → 200 with a Cognito hosted logout URL
    pointing back at the app's /login, and the local session is still
    deleted."""
    monkeypatch.setenv(
        "TG_OIDC_REDIRECT_URI", "https://tg.test/auth/callback")
    _set_saml_signout("HpeIdc", True)
    _seed_session("son")
    r = app_client.post(
        "/auth/logout",
        cookies={"tg_session": "son"},
        follow_redirects=False)
    assert r.status_code == 200
    url = r.json()["logout_url"]
    # Derived from the auth endpoint's origin → <domain>/logout, with
    # client_id + logout_uri=<app>/login.
    assert url.startswith("https://test.okta.com/logout")
    assert "client_id=cid" in url
    assert "logout_uri=https%3A%2F%2Ftg.test%2Flogin" in url
    # local session still gone
    from db.session import get_db
    from db.models import WebSession
    with get_db() as db:
        assert db.query(WebSession).filter(
            WebSession.id == "son").first() is None


def test_logout_saml_signout_on_falls_back_when_no_redirect_uri(
    app_client, oidc_configured, monkeypatch
):
    """Even with SAML+signout, if the return URI can't be built (no
    redirect_uri) logout must still clear the session — fall back to
    204, never leave a live session."""
    monkeypatch.delenv("TG_OIDC_REDIRECT_URI", raising=False)
    _set_saml_signout("HpeIdc", True)
    _seed_session("sfb")
    r = app_client.post(
        "/auth/logout",
        cookies={"tg_session": "sfb"},
        follow_redirects=False)
    assert r.status_code == 204
    from db.session import get_db
    from db.models import WebSession
    with get_db() as db:
        assert db.query(WebSession).filter(
            WebSession.id == "sfb").first() is None


def test_oidc_logout_url_helper():
    """logout_url builds the hosted-UI form; prefers end_session_endpoint;
    returns None when neither endpoint is known."""
    cfg = _cfg()
    url = oidc_mod.logout_url(cfg, "https://tg.test/login")
    assert url.startswith("https://test.okta.com/logout?")
    assert "client_id=cid" in url

    cfg_es = oidc_mod.OIDCConfig(
        issuer="https://x", client_id="cid", client_secret="s",
        redirect_uri="https://tg.test/auth/callback",
        end_session_endpoint="https://test.okta.com/oauth2/v1/logout")
    url2 = oidc_mod.logout_url(cfg_es, "https://tg.test/login")
    assert url2.startswith("https://test.okta.com/oauth2/v1/logout?")

    cfg_none = oidc_mod.OIDCConfig(
        issuer="https://x", client_id="cid", client_secret="s",
        redirect_uri="https://tg.test/auth/callback")
    assert oidc_mod.logout_url(cfg_none, "https://tg.test/login") is None
