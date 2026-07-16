"""#595: redaction, worker run_id/job events, reject WARNINGs.

Builds on #583 (log_config) + #587 (log_context). No AWS/DB for
the redaction + filter tests; job-events test stubs get_db.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_CONTAINER = Path(__file__).resolve().parents[1]
if str(_CONTAINER) not in sys.path:
    sys.path.insert(0, str(_CONTAINER))

import log_config as lc  # noqa: E402
from api import log_context as lx  # noqa: E402


# ── RedactionFilter ──────────────────────────────────────────────

def _redact_record(msg, **extra):
    f = lc.RedactionFilter()
    rec = logging.makeLogRecord({"msg": msg, "args": (), **extra})
    f.filter(rec)
    return rec


def test_redacts_authorization_header_value():
    rec = _redact_record("Authorization: Bearer sk-abcdef123456")
    assert "[REDACTED]" in rec.getMessage()
    assert "sk-abcdef123456" not in rec.getMessage()


def test_redacts_provider_api_keys():
    for secret in ["sk-abcdefgh12345678", "ghp_0123456789abcdef0123",
                   "AKIAQRSTUVWX1234", "xoxb-1234-5678-abcdefgh"]:
        rec = _redact_record(f"key is {secret} here")
        assert secret not in rec.getMessage()
        assert "[REDACTED]" in rec.getMessage()


def test_redacts_labeled_secret_assignment():
    rec = _redact_record("client_secret=supersecretvalue123")
    assert "supersecretvalue123" not in rec.getMessage()
    assert "[REDACTED]" in rec.getMessage()


def test_email_is_not_redacted():
    # email is the CUR attribution key — must survive
    rec = _redact_record("blocked user alice@example.com over cap")
    assert "alice@example.com" in rec.getMessage()


def test_redacts_string_extra_values():
    rec = _redact_record("auth attempt",
                         token="ghp_0123456789abcdef0123")
    # the string extra got redacted in place
    assert "ghp_0123456789abcdef0123" not in str(rec.__dict__["token"])
    assert "[REDACTED]" in rec.__dict__["token"]


def test_redaction_never_raises_on_weird_record():
    f = lc.RedactionFilter()
    rec = logging.makeLogRecord({"msg": "x", "obj": object()})
    assert f.filter(rec) is True  # no exception


# ── run_id contextvar + filter ──────────────────────────────────

def test_filter_injects_run_id():
    f = lx.RequestContextFilter()
    tok = lx.run_id_var.set("4242")
    try:
        rec = logging.makeLogRecord({"msg": "x"})
        f.filter(rec)
        assert rec.run_id == "4242"
    finally:
        lx.run_id_var.reset(tok)


def test_run_id_defaults_dash_off_a_job():
    f = lx.RequestContextFilter()
    rec = logging.makeLogRecord({"msg": "x"})
    f.filter(rec)
    assert rec.run_id == "-"


# ── worker job events + run_id binding ──────────────────────────

def _patch_jobrunner_db(jr, monkeypatch, run_id=4242):
    """Stub get_db / JobRun / pause so the wrapper runs sans Postgres.
    JobRun is a real class (its `id` class attr supports the
    `JobRun.id == run_id` query expression in the wrapper)."""
    class JobRun:
        id = run_id
        def __init__(self, **k):
            for kk, vv in k.items():
                setattr(self, kk, vv)
            self.id = run_id
    row = JobRun()
    class _Query:
        def filter(self, *a): return self
        def first(self): return row
    class _DB:
        def add(self, x): pass
        def flush(self): pass
        def query(self, *a): return _Query()
    import contextlib
    @contextlib.contextmanager
    def _get_db():
        yield _DB()
    monkeypatch.setattr(jr, "get_db", _get_db)
    monkeypatch.setattr(jr, "get_jobs_paused_until", lambda db: None)
    monkeypatch.setattr(jr, "JobRun", JobRun)


def test_job_emits_start_ok_with_run_id(caplog, monkeypatch):
    import worker.job_runner as jr
    _patch_jobrunner_db(jr, monkeypatch)

    wrapped = jr.job("demo_job", lambda: {"detail": "done"})
    with caplog.at_level(logging.INFO, logger="worker.job_runner"):
        wrapped(triggered_by="alice@example.com")

    events = [r for r in caplog.records
              if getattr(r, "event", None) in ("job.start", "job.ok")]
    assert {e.event for e in events} == {"job.start", "job.ok"}
    ok = next(e for e in events if e.event == "job.ok")
    assert ok.job == "demo_job"
    assert isinstance(ok.duration_ms, float)


def test_job_fail_emits_event_and_reraises(caplog, monkeypatch):
    import worker.job_runner as jr
    _patch_jobrunner_db(jr, monkeypatch, run_id=99)

    def _boom():
        raise ValueError("kaboom")
    wrapped = jr.job("boom_job", _boom)
    with caplog.at_level(logging.INFO, logger="worker.job_runner"):
        with pytest.raises(ValueError):
            wrapped()
    fails = [r for r in caplog.records
             if getattr(r, "event", None) == "job.fail"]
    assert len(fails) == 1
    assert fails[0].job == "boom_job"
    assert fails[0].exc_info is not None  # log.exception → traceback


# ── auth_gate / csrf reject WARNINGs ────────────────────────────

def test_auth_gate_logs_warning_on_anonymous_api(caplog, monkeypatch):
    import api.auth_gate as ag
    from starlette.requests import Request
    monkeypatch.setenv("TG_AUTH_REQUIRE_LOGIN", "1")

    mw = ag.AuthGateMiddleware(app=lambda *a: None)
    scope = {"type": "http", "method": "GET", "path": "/api/users",
             "headers": [], "query_string": b""}
    req = Request(scope)

    async def _call_next(_req):
        raise AssertionError("should not reach the app")

    import asyncio
    with caplog.at_level(logging.WARNING, logger="api.auth_gate"):
        resp = asyncio.run(mw.dispatch(req, _call_next))
    assert resp.status_code == 401
    recs = [r for r in caplog.records
            if getattr(r, "event", None) == "gate_reject"]
    assert recs and recs[0].path == "/api/users"
    assert recs[0].reason == "anonymous_api"
