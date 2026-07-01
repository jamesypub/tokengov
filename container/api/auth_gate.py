"""
Auth-gate middleware (#183).

When TG_AUTH_REQUIRE_LOGIN=1, the SPA + docs surface is gated:
anonymous browser requests get 302'd to /auth/login. Without
this gate, mounting the React build at "/" served the admin UI
to anyone who could reach the ALB — the root cause flagged by
AWS AppSec ticket V2226500622.

Pass-through (always 200, even anonymous):
  - GET /api/version   — health probe
  - /api/csrf          — token mint
  - /auth/login, /auth/callback, /auth/logout
  - Requests carrying a tg_session cookie (auth.py validates it
    on the route)
  - X-Tg-Test-Email when TG_AUTH_TEST_TRUST=1 (dev/CI bypass)

Anonymous SPA / docs / fallback paths get 302 -> /auth/login.
Anonymous /api/* (no cookie, no test-trust) gets 401 JSON so the
SPA's fetch() surfaces a clear error instead of a redirect to
HTML.

#581: the former SigV4 gate-pass (Authorization: AWS4-HMAC-SHA256)
was removed — it let an AWS4-headed request past the gate, but the
validator behind it (auth._validate_sigv4) was deleted with the
desktop client in #576/#580, so it was a dead pass-through.
"""
from __future__ import annotations

import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

# #595: make the previously-silent gate rejections visible at
# WARNING (path/method/reason only — never bodies/headers/secrets).
log = logging.getLogger("api.auth_gate")


PUBLIC_PATHS = {
    "/api/version",
    "/api/csrf",
    "/auth/login",
    "/auth/callback",
    "/auth/logout",
    "/login",
}


def _is_enabled() -> bool:
    return os.environ.get(
        "TG_AUTH_REQUIRE_LOGIN", "").strip() == "1"


def _has_session(request: Request) -> bool:
    return bool(request.cookies.get("tg_session"))


def _is_test_bypass(request: Request) -> bool:
    if os.environ.get("TG_AUTH_TEST_TRUST") != "1":
        return False
    return bool(request.headers.get("x-tg-test-email"))


class AuthGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not _is_enabled():
            return await call_next(request)

        path = request.url.path
        if path in PUBLIC_PATHS:
            return await call_next(request)
        if path.startswith("/auth/"):
            return await call_next(request)
        # Static SPA assets must load on the anonymous /login
        # page (#193). Without this, /assets/index-*.js gets
        # 302'd and the branded card never renders.
        if path.startswith("/assets/") or path in (
                "/favicon.ico", "/vite.svg"):
            return await call_next(request)

        if (_has_session(request)
                or _is_test_bypass(request)):
            return await call_next(request)

        # Anonymous /api/* — return JSON so the SPA's fetch()
        # surfaces a real error, not a redirect-to-HTML that
        # confuses XHR.
        if path.startswith("/api/") or path.startswith(
                "/openapi") or path.startswith("/docs") \
                or path.startswith("/redoc"):
            if path.startswith("/api/"):
                log.warning(
                    "auth gate reject",
                    extra={"event": "gate_reject", "path": path,
                           "method": request.method,
                           "reason": "anonymous_api", "status": 401},
                )
                return JSONResponse(
                    {"detail": "login required",
                     "code": "login_required"},
                    status_code=401,
                )
            # /docs, /redoc, /openapi.json from a browser →
            # bounce through the branded SPA login (#193).
            log.warning(
                "auth gate redirect",
                extra={"event": "gate_reject", "path": path,
                       "method": request.method,
                       "reason": "anonymous_docs", "status": 302},
            )
            return RedirectResponse(
                "/login", status_code=302)

        # SPA root + asset fallback — bounce to the branded
        # /login page first (#193). The user lands on the
        # SPA's "Cost vs. Value" card, then clicks through
        # to /auth/login for the Cognito Hosted UI.
        log.warning(
            "auth gate redirect",
            extra={"event": "gate_reject", "path": path,
                   "method": request.method,
                   "reason": "anonymous_spa", "status": 302},
        )
        return RedirectResponse(
            "/login", status_code=302)
