"""#583: structured JSON logging — unit tests for log_config.

No AWS / DB — exercises the formatter + configure_logging on the
stdlib logging machinery directly.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

_CONTAINER = Path(__file__).resolve().parents[1]
if str(_CONTAINER) not in sys.path:
    sys.path.insert(0, str(_CONTAINER))

import log_config  # noqa: E402


def _fmt(record):
    return log_config.JsonFormatter().format(record)


def test_basic_line_is_valid_json_with_core_fields():
    rec = logging.makeLogRecord({
        "name": "worker", "levelno": logging.INFO,
        "levelname": "INFO", "msg": "hello %s", "args": ("world",),
    })
    obj = json.loads(_fmt(rec))
    assert obj["level"] == "INFO"
    assert obj["logger"] == "worker"
    assert obj["msg"] == "hello world"   # %-args rendered
    assert "ts" in obj


def test_extra_fields_are_emitted_as_structured_keys():
    rec = logging.makeLogRecord({
        "name": "api", "levelno": logging.WARNING,
        "levelname": "WARNING", "msg": "over cap",
        "email": "alice@example.com", "pct": 142,
    })
    obj = json.loads(_fmt(rec))
    assert obj["email"] == "alice@example.com"
    assert obj["pct"] == 142


def test_exception_traceback_captured():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.makeLogRecord({
            "name": "x", "levelno": logging.ERROR,
            "levelname": "ERROR", "msg": "failed",
            "exc_info": sys.exc_info(),
        })
    obj = json.loads(_fmt(rec))
    assert "exc" in obj
    assert "ValueError: boom" in obj["exc"]


def test_non_serializable_extra_falls_back_to_str():
    rec = logging.makeLogRecord({
        "name": "x", "levelno": logging.INFO, "levelname": "INFO",
        "msg": "obj", "thing": object(),
    })
    # default=str → no exception, line is still valid JSON.
    obj = json.loads(_fmt(rec))
    assert "thing" in obj


def test_configure_logging_installs_single_json_handler(monkeypatch):
    monkeypatch.setenv("TG_LOG_FORMAT", "json")
    monkeypatch.setenv("TG_LOG_LEVEL", "DEBUG")
    log_config.configure_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, log_config.JsonFormatter)
    assert root.level == logging.DEBUG
    # idempotent — second call doesn't stack handlers.
    log_config.configure_logging()
    assert len(logging.getLogger().handlers) == 1


def test_plain_format_opt_out(monkeypatch):
    monkeypatch.setenv("TG_LOG_FORMAT", "plain")
    monkeypatch.delenv("TG_LOG_LEVEL", raising=False)
    log_config.configure_logging()
    fmtr = logging.getLogger().handlers[0].formatter
    assert not isinstance(fmtr, log_config.JsonFormatter)


def test_uvicorn_loggers_routed_to_root(monkeypatch):
    monkeypatch.setenv("TG_LOG_FORMAT", "json")
    log_config.configure_logging()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        assert lg.handlers == []
        assert lg.propagate is True


@pytest.fixture(autouse=True)
def _restore_logging():
    """Leave the root logger as configure_logging found it so these
    tests don't bleed into the rest of the suite."""
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = saved
