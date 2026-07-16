"""
Routes for the Okta OIDC browser login flow (#130).

`/auth/login`     — start: PKCE + state cookies, 302 to the IdP
`/auth/callback`  — exchange + verify, set session cookie
`/auth/logout`    — delete the session row + cookies; with a SAML IdP
                    + idp_signout on, also returns a Cognito hosted
                    logout URL for the SPA to navigate to (so the IDC
                    session is terminated, not just tg's local one).
                    Otherwise 204.
`/api/csrf`       — issue a fresh CSRF token (also sets cookie)

These routes are mounted unconditionally; if OIDC env vars
aren't configured, they return 501 so misconfig surfaces.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import (
    APIRouter, Cookie, Depends, HTTPException, Request, Response,
)
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from api import oidc as oidc_mod
from db.session import get_db
from db.models import AdminRole, WebSession
from db.org_config import tg_owns_directory
from db.auth_config import (
    saml_provider_name,
    get_sso_button_label,
    get_sso_button_label_raw,
    get_saml_config,
)

log = logging.getLogger("api.auth_routes")

router = APIRouter()

SESSION_COOKIE = "tg_session"
CSRF_COOKIE    = "tg_csrf"
PKCE_COOKIE    = "tg_pkce"   # code_verifier
STATE_COOKIE   = "tg_oidc_state"

SESSION_TTL = timedelta(hours=8)
PKCE_TTL    = 600  # seconds; covers slowest user click-through


def _db():
    with get_db() as db:
        yield db


def _cookie_secure() -> bool:
    """`Secure` is mandatory in prod. Off only when explicitly
    set, so dev-over-http works without weakening production."""
    return os.environ.get(
        "TG_COOKIE_INSECURE", "") != "1"


def _set_session_cookie(resp: Response, value: str,
                         max_age: int):
    resp.set_cookie(
        SESSION_COOKIE, value,
        max_age=max_age,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )


def _set_csrf_cookie(resp: Response, value: str,
                      max_age: int):
    # CSRF cookie is readable by JS on purpose (double-submit
    # pattern). Still SameSite=Lax + Secure.
    resp.set_cookie(
        CSRF_COOKIE, value,
        max_age=max_age,
        httponly=False,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )


def _delete_cookie(resp: Response, name: str):
    resp.delete_cookie(name, path="/")


# ── Discovery cache ───────────────────────────────────────────

_CFG: Optional[oidc_mod.OIDCConfig] = None
_JWKS_CACHE: dict = {"jwks": None, "ts": 0.0}
_JWKS_TTL = 3600.0


def _http_get(url: str) -> dict:
    r = httpx.get(url, timeout=10.0)
    r.raise_for_status()
    return r.json()


def _http_post(url: str, form: dict) -> dict:
    r = httpx.post(url, data=form, timeout=10.0)
    r.raise_for_status()
    return r.json()


def _ensure_cfg() -> oidc_mod.OIDCConfig:
    global _CFG
    if _CFG is not None and _CFG.token_endpoint:
        return _CFG
    base = oidc_mod.config_from_env()
    if base is None:
        raise HTTPException(
            501,
            "OIDC not configured (TG_OIDC_ISSUER + "
            "TG_OIDC_CLIENT_ID + TG_OIDC_CLIENT_SECRET + "
            "TG_OIDC_REDIRECT_URI)",
        )
    _CFG = oidc_mod.discover(base, _http_get)
    return _CFG


def _get_jwks(cfg: oidc_mod.OIDCConfig) -> dict:
    now = time.time()
    if (_JWKS_CACHE["jwks"] is not None
            and now - _JWKS_CACHE["ts"] < _JWKS_TTL):
        return _JWKS_CACHE["jwks"]
    jwks = _http_get(cfg.jwks_uri)
    _JWKS_CACHE["jwks"] = jwks
    _JWKS_CACHE["ts"] = now
    return jwks


# ── Routes ────────────────────────────────────────────────────

def _default_identity_provider(db: Session) -> Optional[str]:
    # Picker bypass (#197). The Cognito Hosted UI picker is
    # unbranded chrome that mismatches the rest of the SPA — we
    # always pass identity_provider= so users land directly on
    # either the federated IdP or the COGNITO password form.
    # #926: the login-page provider derives from the DB flag
    # (config-as-data), not the install env. When an external IdP is
    # federated → the federated provider name; else → COGNITO. The name
    # comes from the runtime SAML config (admin_config) first, env as
    # fallback, so configuring an IdP from Settings routes the button
    # with no redeploy. Cognito ignores unknown providers, so a stale
    # name is a harmless no-op.
    #
    # A runtime SAML IdP set from Settings implies the directory is
    # externally owned, so route to it EVEN WHEN tg_owns_directory is
    # still true — matching /auth/providers' `federated` logic
    # (federated = saml_name OR (env_name AND not owns)). Previously this
    # required `not owns`, so with owns=true + a runtime SAML name the
    # SSO button handed off identity_provider=COGNITO and Cognito
    # re-showed its picker instead of proceeding to the IdP (the
    # picker-bounce regression).
    owns = tg_owns_directory(db)
    saml_name = saml_provider_name(db)
    if saml_name:
        return saml_name
    if not owns:
        env_name = os.environ.get(
            "TG_OIDC_OKTA_PROVIDER_NAME", "").strip() or None
        if env_name:
            return env_name
    return "COGNITO"


@router.get("/auth/providers")
def auth_providers(db: Session = Depends(_db)):
    """Public endpoint — surfaces which IdPs are wired so the
    login page can render the right affordances. Returns
    `{cognito, okta, okta_display_name, cognito_provisioning}`.

    #926: `cognito` (tg owns the directory → password login + the
    AdminCreateUser/"Send invite" path) and `okta` (an external IdP
    federates) are now driven by the runtime DB flag
    `tg_owns_directory`, NOT the install-time TG_AUTH_PROVIDER env —
    so flipping the flag changes the login page with no redeploy.
    `cognito_provisioning` (#357) tracks the same flag: the Admins
    panel keys its "Send invite" checkbox off it."""
    owns = tg_owns_directory(db)
    # The federated provider name + button label come from the runtime
    # SAML config (admin_config) first, env as fallback — so a Settings
    # change flips the login page with no redeploy. A SAML IdP
    # configured at runtime implies the directory is externally owned,
    # so surface the federated button even if the tg_owns_directory flag
    # wasn't flipped separately.
    saml_name = saml_provider_name(db)
    okta_name = saml_name or os.environ.get(
        "TG_OIDC_OKTA_PROVIDER_NAME", "").strip()
    federated = bool(okta_name) and (not owns or bool(saml_name))
    # When tg owns the directory it's a Cognito deployment: password
    # login on, federation off. When an external IdP owns identities (or
    # a runtime SAML IdP is wired): surface the federated button, Cognito
    # provisioning off (users come from the IdP).
    display = None
    if federated:
        # Runtime button label wins when explicitly set; otherwise fall
        # back to the legacy per-tenant display-name env, then the
        # generic default ("Login with Your SSO") via get_sso_button_label.
        display = get_sso_button_label_raw(db)
        if display is None:
            env_disp = os.environ.get(
                "TG_OIDC_OKTA_DISPLAY_NAME", "").strip()
            display = env_disp or get_sso_button_label(db)
    return {
        "cognito": owns and not federated,
        "okta": federated,
        "okta_display_name": display,
        "cognito_provisioning": owns,
    }


@router.get("/auth/login")
def auth_login(
    request: Request,
    identity_provider: Optional[str] = None,
    db: Session = Depends(_db),
):
    # Caller can override via ?identity_provider= (e.g. the SPA
    # explicitly asking for COGNITO when Okta is wired but the
    # user wants to use the bootstrap-admin password path). When
    # omitted, default to the Hosted-UI-picker-skipping value.
    if not identity_provider:
        identity_provider = _default_identity_provider(db)
    cfg = _ensure_cfg()
    state = oidc_mod.new_state()
    verifier, challenge = oidc_mod.new_pkce_pair()
    url = oidc_mod.authorize_url(
        cfg, state, challenge,
        identity_provider=identity_provider,
    )
    resp = RedirectResponse(url, status_code=302)
    # Short-lived; only used during the round-trip.
    resp.set_cookie(
        STATE_COOKIE, state,
        max_age=PKCE_TTL,
        httponly=True, secure=_cookie_secure(),
        samesite="lax", path="/",
    )
    resp.set_cookie(
        PKCE_COOKIE, verifier,
        max_age=PKCE_TTL,
        httponly=True, secure=_cookie_secure(),
        samesite="lax", path="/",
    )
    return resp


@router.get("/auth/callback")
def auth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(_db),
    state_cookie: Optional[str] = Cookie(None, alias=STATE_COOKIE),
    pkce_cookie:  Optional[str] = Cookie(None, alias=PKCE_COOKIE),
):
    if error:
        raise HTTPException(
            400, f"Okta returned error: {error}")
    if not code or not state:
        raise HTTPException(400, "missing code or state")
    if not state_cookie or state != state_cookie:
        # Mismatch = CSRF/replay attempt or expired flow.
        raise HTTPException(400, "state mismatch")
    if not pkce_cookie:
        raise HTTPException(400, "missing PKCE verifier cookie")

    cfg = _ensure_cfg()
    try:
        token_resp = oidc_mod.exchange_code(
            cfg, code, pkce_cookie, _http_post)
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            401, f"token exchange failed: {e}")

    id_token = token_resp.get("id_token")
    if not id_token:
        raise HTTPException(
            401, "token response missing id_token")
    refresh_token = token_resp.get("refresh_token")

    jwks = _get_jwks(cfg)
    try:
        claims = oidc_mod.verify_id_token(id_token, cfg, jwks)
    except oidc_mod.IdTokenError as e:
        raise HTTPException(401, f"id_token invalid: {e}")

    email = (claims.get("email") or "").strip().lower()
    if not email:
        # A misconfigured SAML email attribute is the most common cause
        # — and it dead-ended at a bare 401 with no way back. Redirect
        # to the SPA login with the error so the page shows the message
        # AND the org-admin break-glass recovery link (Gap 1).
        redir = _login_error_redirect(
            "Sign-in failed: the identity provider returned no email "
            "address. Check the SAML email attribute mapping, or use "
            "org-admin recovery below.")
        if redir is not None:
            return redir
        raise HTTPException(
            401, "id_token has no email claim — "
                 "ensure the Okta app grants 'email' scope")

    # Bootstrap path mirrors lifespan in main.py: first-ever
    # admin gets seeded from BOOTSTRAP_ADMIN_EMAIL.
    bootstrap_email = os.environ.get(
        "BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    if bootstrap_email and email == bootstrap_email:
        existing = db.query(AdminRole).count()
        if existing == 0:
            db.add(AdminRole(
                email=email,
                role="org_admin",
                granted_by="oidc-bootstrap",
            ))
            db.flush()

    # #927: admit anyone with an authz row — admin OR member. The gate
    # is "registered in tg → let in"; the SESSION is then scoped by
    # role (Scope.is_org_admin/is_team_admin gate admin surfaces; a
    # member reaches only member-scoped views). A row is created by an
    # admin's "Enable login" action (or a role grant). Anyone with no
    # row is still rejected.
    has_role = (
        db.query(AdminRole)
        .filter(AdminRole.email == email)
        .first()
    )
    if not has_role:
        raise HTTPException(
            403,
            f"{email} is not registered in tg — "
            "ask your org_admin to enable your login")

    # Org-admin gate on the break-glass recovery path. When SSO is the
    # configured method, the COGNITO password page is the emergency
    # bypass — and is reserved for an org admin who can actually repair
    # SSO. A federated login carries an `identities` claim (the IdP it
    # came from); a native COGNITO password login does not. So: SSO
    # configured + no federated identity + not an org_admin ⇒ refuse
    # (a member must sign in via SSO, not the password bypass). This is
    # a real bypass surface, so we keep an audit line on use.
    saml_on = bool(saml_provider_name(db))
    via_federation = bool(claims.get("identities"))
    if saml_on and not via_federation:
        is_org_admin = (
            db.query(AdminRole)
            .filter(AdminRole.email == email,
                    AdminRole.role == "org_admin")
            .first()
        )
        if not is_org_admin:
            raise HTTPException(
                403,
                "Password recovery sign-in is restricted to org admins "
                "while SSO is enabled — please sign in with SSO.")
        log.warning(
            "break-glass recovery sign-in (COGNITO password path) by "
            "org_admin %s while SSO is configured", email)

    sid = oidc_mod.new_session_id()
    expires = datetime.now(timezone.utc) + SESSION_TTL
    ua = request.headers.get("user-agent", "")[:500]
    ip = (request.client.host if request.client else None)

    db.add(WebSession(
        id=sid,
        email=email,
        expires_at=expires,
        refresh_token=refresh_token,
        user_agent=ua,
        ip=ip,
    ))
    db.flush()

    # Land back on the SPA root.
    resp = RedirectResponse("/", status_code=302)
    _set_session_cookie(
        resp, sid, max_age=int(SESSION_TTL.total_seconds()))
    _set_csrf_cookie(
        resp, oidc_mod.new_csrf_token(),
        max_age=int(SESSION_TTL.total_seconds()))
    _delete_cookie(resp, STATE_COOKIE)
    _delete_cookie(resp, PKCE_COOKIE)
    return resp


def _login_return_uri() -> Optional[str]:
    """The post-logout landing — the app's /login, derived from the
    OIDC redirect_uri's origin (…/auth/callback → …/login). Must be a
    registered LogoutURL on the app client or Cognito rejects it. The
    SAML apply registers exactly this string in the client's
    LogoutURLs, so both sides share one derivation. None when the
    redirect_uri isn't configured."""
    from api.cognito_saml import login_return_uri
    return login_return_uri()


def _login_error_redirect(message: str):
    """A 302 to the SPA login page carrying `?error=<message>` so an
    authentication failure shows the message AND the recovery affordance
    instead of dead-ending at a bare HTTP error (Gap 1). Returns None if
    the login URI can't be derived (caller falls back to raising)."""
    base = _login_return_uri()
    if not base:
        return None
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}error={quote(message)}"
    return RedirectResponse(url, status_code=302)


@router.post("/auth/logout")
def auth_logout(
    request: Request,
    db: Session = Depends(_db),
    sid: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
):
    # Local logout is UNCONDITIONAL and always happens first — never
    # leave a live tg session, even if the IdP-logout redirect below
    # can't be built.
    if sid:
        db.query(WebSession).filter(
            WebSession.id == sid).delete()

    # When a SAML IdP is configured AND idp_signout is on, a plain local
    # logout isn't enough: the IDC browser session stays live, so the
    # SSO button would silently re-login. Hand the SPA a Cognito hosted
    # logout URL to navigate to — Cognito (with IDPSignout=true on the
    # SAML IdP) then propagates the single-logout to IDC. Best-effort on
    # top of the local delete: if anything needed to build the URL is
    # missing, fall through to the plain 204.
    logout_redirect = None
    try:
        cfg = get_saml_config(db)
        if cfg.get("configured") and cfg.get("idp_signout"):
            return_uri = _login_return_uri()
            if return_uri:
                oidc_cfg = _ensure_cfg()
                logout_redirect = oidc_mod.logout_url(
                    oidc_cfg, return_uri)
    except Exception:  # noqa: BLE001 — IdP logout is best-effort
        logout_redirect = None

    if logout_redirect:
        resp = Response(
            content=json.dumps({"logout_url": logout_redirect}),
            media_type="application/json",
            status_code=200,
        )
    else:
        resp = Response(status_code=204)
    _delete_cookie(resp, SESSION_COOKIE)
    _delete_cookie(resp, CSRF_COOKIE)
    return resp


@router.get("/api/csrf")
def get_csrf():
    """Issue a fresh CSRF token for the SPA. Idempotent — the
    SPA can call this on load to populate the cookie + a JS
    variable for the X-CSRF-Token header."""
    token = oidc_mod.new_csrf_token()
    resp = Response(
        content=json.dumps({"csrf_token": token}),
        media_type="application/json",
    )
    _set_csrf_cookie(
        resp, token,
        max_age=int(SESSION_TTL.total_seconds()))
    return resp
