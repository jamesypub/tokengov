"""Per-request access logging + request-id correlation (#587).

RequestContextMiddleware:
  - sets the request_id contextvar (honoring a valid inbound
    X-Request-Id, else generating one) so ALL log lines this
    request produces carry the same id;
  - times the request and emits exactly ONE structured
    `http_access` line with method/path/status/latency_ms/
    request_id/caller;
  - returns X-Request-Id on the response (incl. the 500 path).

Ordering (Starlette runs middleware last-added-first): this is
added LAST in main.py so it's OUTERMOST — it sets request_id
before anything else logs and observes the final status.

Exception shape (https://www.starlette.io/middleware/): an
unhandled error propagates UP through BaseHTTPMiddleware without
a normal response, so `response.status_code` would miss it. We
log the access line in `finally` (status defaults to 500 if the
call raised) and re-raise so the app's catch-all
exception_handler (main.py) produces the JSON 500 body. Time is
in nanoseconds via perf_counter_ns (monotonic — no clock-skew /
no forbidden wall-clock dependency).
"""
from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from api.log_context import (
    caller_var,
    coerce_request_id,
    request_id_var,
)

log = logging.getLogger("api.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = coerce_request_id(request.headers.get("x-request-id"))
        # Set the contextvars fresh for THIS request. We do NOT
        # reset() them in finally: the catch-all
        # @app.exception_handler(Exception) runs on Starlette's
        # ServerErrorMiddleware, which is OUTSIDE this middleware —
        # so a reset here would leave the handler reading the
        # default "-". ContextVars are per-asyncio-task (each
        # request is its own task, copying the context at creation)
        # and we overwrite them at the start of every request, so
        # leaving them set cannot bleed across requests.
        request_id_var.set(rid)
        caller_var.set("-")  # auth layer rebinds when resolvable
        # Also stash on request.state so the exception handler can
        # read it from the SAME request object regardless of
        # context-propagation timing (belt-and-braces).
        request.state.request_id = rid
        start = time.perf_counter_ns()
        status = 500  # default if the call raises (see finally)
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-Id"] = rid
            return response
        finally:
            latency_ms = round(
                (time.perf_counter_ns() - start) / 1_000_000, 1
            )
            log.info(
                "http_access",
                extra={
                    "event": "http_access",
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "latency_ms": latency_ms,
                    # request_id + caller are added by the
                    # RequestContextFilter from the contextvars,
                    # but include request_id explicitly so the line
                    # is correct even if the filter isn't attached.
                    "request_id": rid,
                },
            )
