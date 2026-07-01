"""Structured application logging (#583).

One JSON object per line on stdout, so both surfaces capture
reviewable, greppable, machine-parseable logs:
  - ECS: the awslogs driver ships stdout to CloudWatch
    /ecs/tg-container (retention configurable, default 7d — see
    cfn/tg-container-stack.yaml LogRetentionDays).
  - local docker-compose: stdout → the json-file logging driver
    with size+file caps (docker-compose.yml), the local analogue
    of retention.

Goals (#583): reproduce user-reported issues + proactively spot
problems in the log.

Stdlib only — no new deps. Configure once at each entrypoint
(api.main, worker.main) via configure_logging().

Env:
  TG_LOG_FORMAT  json (default) | plain   — plain = the old
                 human format, handy for local tailing.
  TG_LOG_LEVEL   DEBUG|INFO|WARNING|ERROR  (default INFO)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys

# Attributes the stdlib LogRecord always carries — anything NOT in
# here that a caller attached via `extra={...}` is emitted as a
# structured field, so call sites can enrich a line (e.g.
# log.info("quota over cap", extra={"email": e, "pct": 142})).
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {
    "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Structured extras (anything the caller attached).
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack"] = self.formatStack(record.stack_info)
        return json.dumps(out, default=str)


_PLAIN_FMT = "%(asctime)s %(levelname)s %(name)s %(message)s"


# #595: redact secrets before they reach a handler. Matches common
# token/secret shapes in the formatted message + any string `extra`
# values. Email is NEVER redacted — it's the CUR per-user
# attribution key (repo memory feedback_role_session_name_email).
_REDACTION_PATTERNS = [
    # Authorization: Bearer <token> / Basic <b64> / AWS4-HMAC...
    re.compile(
        r"(?i)(authorization\s*[:=]\s*)(\S+)"),
    # bearer <token> anywhere
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{6,})"),
    # provider-style API keys: sk-..., ghp_..., xoxb-..., AKIA...
    re.compile(
        r"\b(sk-[A-Za-z0-9]{8,}"
        r"|gh[pousr]_[A-Za-z0-9]{16,}"
        r"|xox[baprs]-[A-Za-z0-9\-]{8,}"
        r"|AKIA[0-9A-Z]{12,})\b"),
    # generic "token"/"secret"/"password"/"api_key" = <value>
    re.compile(
        r"(?i)\b(token|secret|password|passwd|api[_-]?key"
        r"|client[_-]?secret)\b(\s*[:=]\s*)(\S+)"),
]
_REDACTED = "[REDACTED]"


def _redact(text: str) -> str:
    out = text
    for pat in _REDACTION_PATTERNS:
        # keep the labelling group(s), redact the value group.
        if pat.groups >= 3:
            out = pat.sub(lambda m: m.group(1) + m.group(2) + _REDACTED, out)
        elif pat.groups == 2:
            out = pat.sub(lambda m: m.group(1) + _REDACTED, out)
        else:
            out = pat.sub(_REDACTED, out)
    return out


class RedactionFilter(logging.Filter):
    """Redact secret-shaped substrings from the rendered message +
    string extras, in place, before any handler formats them.
    Runs on every record (api + worker)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            red = _redact(msg)
            if red != msg:
                record.msg = red
                record.args = ()
            # string extras attached via extra={...}
            for k, v in list(record.__dict__.items()):
                if isinstance(v, str) and k not in _RESERVED:
                    rv = _redact(v)
                    if rv != v:
                        record.__dict__[k] = rv
        except Exception:  # noqa: BLE001 - logging must never break
            pass
        return True


def configure_logging() -> None:
    """Install the configured formatter on the root logger.

    Idempotent — replaces existing handlers so a re-import (or
    uvicorn's own basicConfig) doesn't double-log. Call once at
    process start.
    """
    level_name = os.environ.get("TG_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = os.environ.get("TG_LOG_FORMAT", "json").lower()

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "plain":
        handler.setFormatter(logging.Formatter(_PLAIN_FMT))
    else:
        handler.setFormatter(JsonFormatter())
    # #595: redact secrets on the way out (api + worker).
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # uvicorn installs its own handlers on these; route them
    # through ours so request logs are JSON too (no duplicate
    # emission — propagate to root, drop their handlers).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
