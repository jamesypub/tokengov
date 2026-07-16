"""
Double-submit CSRF middleware (#130).

Cookie-authenticated browser requests carry both:
  * `tg_session` cookie (HTTP-only) — the auth credential
  * `tg_csrf`    cookie (JS-readable) — a CSRF nonce

For state-changing requests, the SPA reads `tg_csrf` and echoes
it back as the `X-CSRF-Token` header. An attacker on a third-
party origin cannot read the cookie (SameSite=Lax + Secure +
the SPA's allow-list) and cannot send the header from a forged
form, so the mismatch blocks the request.

#581: the former SigV4 skip (Authorization: AWS4-HMAC-SHA256)
was removed — it was a desktop-client affordance, and the SigV4
auth path it served was deleted in #576/#580. CSRF protection is
cookie-session-scoped; non-cookie callers (test-trust via
X-Tg-Test-Email) carry no tg_csrf cookie and fall through the
no-cookie branch below.
"""
from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# #595: surface CSRF rejections at WARNING (path/method/reason —
# never the token values).
log = logging.getLogger("api.csrf")


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
EXEMPT_PATHS = {
    "/auth/login",
    "/auth/callback",
    "/auth/logout",
    "/api/csrf",
    # whoami is read-only but used for first-load identity probe.
    # It's a GET so SAFE_METHODS already covers it; listing for
    # clarity in case its method ever changes.
    "/api/whoami",
}


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in SAFE_METHODS:
            return await call_next(request)
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        cookie = request.cookies.get("tg_csrf")
        header = request.headers.get("x-csrf-token")
        if not cookie:
            # No CSRF cookie + cookie auth attempted = either
            # an unauthenticated client (will 401 in auth) or a
            # client that never called /api/csrf. Be loud.
            sess = request.cookies.get("tg_session")
            if sess:
                log.warning(
                    "csrf reject",
                    extra={"event": "csrf_reject",
                           "path": request.url.path,
                           "method": request.method,
                           "reason": "missing_csrf_cookie",
                           "status": 403},
                )
                return JSONResponse(
                    {"detail": "missing CSRF cookie — "
                               "GET /api/csrf first"},
                    status_code=403,
                )
            # No session cookie either — fall through to auth,
            # which will 401 with a clearer message.
            return await call_next(request)

        if not header or header != cookie:
            log.warning(
                "csrf reject",
                extra={"event": "csrf_reject",
                       "path": request.url.path,
                       "method": request.method,
                       "reason": "csrf_token_mismatch",
                       "status": 403},
            )
            return JSONResponse(
                {"detail": "CSRF token mismatch"},
                status_code=403,
            )
        return await call_next(request)
