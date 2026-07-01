"""Request-scoped log context (#587).

Builds on #583's structured JSON logging (container/log_config.py):
adds per-request correlation fields that flow into EVERY log line
with zero call-site changes.

Pattern (asgi-correlation-id / structlog merge_contextvars, both
cited in #587 — implemented inline in stdlib, no new dep):
  - two ContextVars (`request_id`, `caller`) set by the
    RequestContextMiddleware at the start of each request;
  - a logging.Filter that copies their current values onto every
    LogRecord, so #583's JsonFormatter emits them as fields.

The worker has no HTTP request, so the vars default to "-" — the
filter must never raise when they're unset.
"""
from __future__ import annotations

import contextvars
import logging
import re
import uuid

# Defaults chosen so a worker log line (or any pre-request log)
# renders cleanly rather than missing the field.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
caller_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "caller", default="-"
)
# #595: worker correlation. Set per job-run in worker/job_runner.py
# (= the JobRun row id), so all of a job's log lines correlate the
# way request_id does for an HTTP request. Default "-" on the api
# side / outside a job.
run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "run_id", default="-"
)

# Inbound X-Request-Id is attacker-controllable — bind it ONLY if
# it's a short, charset-safe token, else generate one. Guards
# against log-injection / log-forging (#587 security note).
_VALID_REQUEST_ID = re.compile(r"\A[A-Za-z0-9_.-]{1,64}\Z")


def new_request_id() -> str:
    return uuid.uuid4().hex


def coerce_request_id(inbound: str | None) -> str:
    """Honor a valid inbound X-Request-Id, else generate a fresh
    one. Malformed / oversized / missing → generated."""
    if inbound and _VALID_REQUEST_ID.match(inbound):
        return inbound
    return new_request_id()


class RequestContextFilter(logging.Filter):
    """Inject the current request_id + caller onto every record so
    #583's JsonFormatter emits them. Always succeeds (ContextVars
    have defaults), so worker + pre-request logs still format."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.caller = caller_var.get()
        record.run_id = run_id_var.get()  # #595: worker correlation
        return True


def install_request_context_filter() -> None:
    """Attach RequestContextFilter to the root logger's handlers
    (the ones #583's configure_logging() installed). Idempotent —
    won't double-attach. Call AFTER configure_logging()."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(
            isinstance(f, RequestContextFilter)
            for f in handler.filters
        ):
            handler.addFilter(RequestContextFilter())
