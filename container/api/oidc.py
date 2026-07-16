"""
Okta OIDC client — Authorization Code + PKCE flow (#130).

Pure-ish helpers (no FastAPI dependency on the validation /
PKCE / state code paths) so they can be unit-tested with mocked
JWKS and without spinning up a live tenant.

Live HTTP is contained in `exchange_code()` and `discover()`,
both of which take an `http_get`/`http_post` callable so tests
can inject canned responses.
"""
from __future__ import annotations

import base64
import hashlib
import json as _json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

# authlib for JWKS-based id_token validation. We import lazily
# in functions that actually use it so tests can run without it
# installed when they monkeypatch verify_id_token.


# ── Config ────────────────────────────────────────────────────

@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    # Discovered lazily.
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    end_session_endpoint: Optional[str] = None


def config_from_env() -> Optional[OIDCConfig]:
    """Returns None if OIDC is not configured (env vars unset).

    The api should boot fine without OIDC — desktop SigV4
    callers don't need it.
    """
    issuer = os.environ.get("TG_OIDC_ISSUER", "").strip()
    client_id = os.environ.get("TG_OIDC_CLIENT_ID", "").strip()
    client_secret = os.environ.get(
        "TG_OIDC_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get(
        "TG_OIDC_REDIRECT_URI", "").strip()
    if not (issuer and client_id and client_secret and redirect_uri):
        return None
    return OIDCConfig(
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )


# ── PKCE + state helpers ──────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge_S256)."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(
        hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def new_state() -> str:
    return _b64url(secrets.token_bytes(24))


def new_session_id() -> str:
    return _b64url(secrets.token_bytes(32))


def new_csrf_token() -> str:
    return _b64url(secrets.token_bytes(32))


# ── Discovery + token exchange ────────────────────────────────

def discover(
    cfg: OIDCConfig,
    http_get: Callable[[str], dict],
) -> OIDCConfig:
    """Resolve the OIDC well-known config and return a
    populated `OIDCConfig`."""
    well_known = (
        cfg.issuer.rstrip("/")
        + "/.well-known/openid-configuration"
    )
    doc = http_get(well_known)
    return OIDCConfig(
        issuer=cfg.issuer,
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        redirect_uri=cfg.redirect_uri,
        authorization_endpoint=doc["authorization_endpoint"],
        token_endpoint=doc["token_endpoint"],
        jwks_uri=doc["jwks_uri"],
        end_session_endpoint=doc.get("end_session_endpoint"),
    )


def authorize_url(
    cfg: OIDCConfig,
    state: str,
    code_challenge: str,
    *,
    extra_scopes: tuple[str, ...] = (),
    identity_provider: Optional[str] = None,
) -> str:
    from urllib.parse import urlencode
    if not cfg.authorization_endpoint:
        raise ValueError("config not discovered")
    scopes = ("openid", "email", "profile") + tuple(extra_scopes)
    params = {
        "response_type":         "code",
        "client_id":             cfg.client_id,
        "redirect_uri":          cfg.redirect_uri,
        "scope":                 " ".join(scopes),
        "state":                 state,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
    }
    # Cognito-specific knob: skip the Hosted UI picker and go
    # straight to the named federated IdP. Other OIDC providers
    # ignore unknown params, so this is a no-op for vanilla Okta.
    if identity_provider:
        params["identity_provider"] = identity_provider
    return f"{cfg.authorization_endpoint}?{urlencode(params)}"


def logout_url(cfg: OIDCConfig, logout_uri: str) -> Optional[str]:
    """Build the hosted-UI logout URL the browser navigates to so the
    IdP session is terminated, not just tg's local session. Cognito's
    hosted logout is `<domain>/logout?client_id=&logout_uri=`; when the
    Cognito SAML IdP carries IDPSignout=true, Cognito then issues the
    SAML single-logout to the upstream IDC. `logout_uri` is the
    post-logout return (the app's /login) and MUST be registered in the
    app client's LogoutURLs or Cognito rejects it.

    Prefers the discovered `end_session_endpoint`; otherwise derives the
    `<domain>/logout` form from the authorization endpoint's origin.
    Returns None when neither is available — the caller then falls back
    to a plain local logout (the IdP redirect is best-effort on top of
    the unconditional local session delete)."""
    from urllib.parse import urlencode, urlsplit, urlunsplit

    base = (cfg.end_session_endpoint or "").strip()
    if not base:
        # Cognito has no end_session_endpoint in discovery; derive the
        # hosted-UI /logout from the authorization endpoint's origin
        # (https://<domain>/oauth2/authorize → https://<domain>/logout).
        auth = (cfg.authorization_endpoint or "").strip()
        if not auth:
            return None
        parts = urlsplit(auth)
        if not (parts.scheme and parts.netloc):
            return None
        base = urlunsplit((parts.scheme, parts.netloc, "/logout", "", ""))

    # Cognito's logout endpoint takes client_id + logout_uri (NOT the
    # OIDC-standard post_logout_redirect_uri/id_token_hint). This is the
    # hosted-UI form both the /logout and end_session paths accept here.
    params = {"client_id": cfg.client_id, "logout_uri": logout_uri}
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"


def exchange_code(
    cfg: OIDCConfig,
    code: str,
    code_verifier: str,
    http_post: Callable[[str, dict], dict],
) -> dict:
    """POST to the token endpoint and return the token response.

    `http_post(url, form_data)` should send
    `application/x-www-form-urlencoded` and return parsed JSON.
    """
    if not cfg.token_endpoint:
        raise ValueError("config not discovered")
    form = {
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  cfg.redirect_uri,
        "client_id":     cfg.client_id,
        "client_secret": cfg.client_secret,
        "code_verifier": code_verifier,
    }
    return http_post(cfg.token_endpoint, form)


# ── id_token verification ─────────────────────────────────────

class IdTokenError(Exception):
    """Raised when the id_token fails any validation check."""


def verify_id_token(
    id_token: str,
    cfg: OIDCConfig,
    jwks: dict,
    *,
    now: Optional[float] = None,
    leeway: int = 60,
) -> dict:
    """Validate signature + issuer + audience + expiry.

    Returns the claims dict on success. Raises IdTokenError on
    any failure.

    `jwks` is the parsed JSON Web Key Set. Tests pass a fixture
    JWKS; production fetches from `cfg.jwks_uri` and caches.
    """
    try:
        from authlib.jose import jwt, JsonWebKey
        from authlib.jose.errors import JoseError
    except ImportError as e:
        raise IdTokenError(f"authlib not installed: {e}")

    try:
        key_set = JsonWebKey.import_key_set(jwks)
        claims = jwt.decode(
            id_token,
            key_set,
            claims_options={
                "iss": {"essential": True, "value": cfg.issuer},
                "aud": {"essential": True, "value": cfg.client_id},
                "exp": {"essential": True},
            },
        )
        # authlib's validate() handles exp/nbf with leeway.
        claims.validate(now=now, leeway=leeway)
    except JoseError as e:
        raise IdTokenError(str(e))

    return dict(claims)
