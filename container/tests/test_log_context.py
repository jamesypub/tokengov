"""#587: request-id correlation + access logging + 500 capture.

Unit-tests the contextvar/filter/coercion logic and an
integration test of RequestContextMiddleware + the catch-all
exception handler on a tiny FastAPI app (no DB/AWS).
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

from api import log_context as lc  # noqa: E402


# ── coerce_request_id: honor valid, reject/replace malformed ──

def test_valid_inbound_request_id_honored():
    assert lc.coerce_request_id("abc123-DEF_.") == "abc123-DEF_."


def test_missing_request_id_generated():
    out = lc.coerce_request_id(None)
    assert lc._VALID_REQUEST_ID.match(out)
    assert len(out) == 32  # uuid4().hex


def test_malformed_request_id_replaced():
    # spaces, log-injection newline, and oversize → generated
    for bad in ["has space", "a\nb", "x" * 65, "evil;rm -rf",
                "with/slash"]:
        out = lc.coerce_request_id(bad)
        assert out != bad
        assert lc._VALID_REQUEST_ID.match(out)


# ── RequestContextFilter injects the vars onto records ──

def test_filter_injects_request_id_and_caller():
    f = lc.RequestContextFilter()
    rid_tok = lc.request_id_var.set("rid-xyz")
    caller_tok = lc.caller_var.set("alice@example.com")
    try:
        rec = logging.makeLogRecord({"msg": "x"})
        assert f.filter(rec) is True
        assert rec.request_id == "rid-xyz"
        assert rec.caller == "alice@example.com"
    finally:
        lc.request_id_var.reset(rid_tok)
        lc.caller_var.reset(caller_tok)


def test_filter_defaults_when_unset():
    # Fresh context (worker / pre-request): defaults, no error.
    f = lc.RequestContextFilter()
    rec = logging.makeLogRecord({"msg": "x"})
    assert f.filter(rec) is True
    assert rec.request_id == "-"
    assert rec.caller == "-"


def test_install_filter_is_idempotent():
    root = logging.getLogger()
    h = logging.StreamHandler()
    root.addHandler(h)
    try:
        lc.install_request_context_filter()
        lc.install_request_context_filter()
        n = sum(
            isinstance(flt, lc.RequestContextFilter)
            for flt in h.filters
        )
        assert n == 1
    finally:
        root.removeHandler(h)


# ── integration: middleware + 500 handler on a tiny app ──

@pytest.fixture()
def client():
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from starlette.testclient import TestClient
    from api.middleware_access import RequestContextMiddleware
    from api.log_context import request_id_var

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Mirror main.py: read the id off request.state (set by the
        # middleware on this request) — the contextvar reads its
        # default here since the handler runs outside the
        # request-context middleware.
        rid = getattr(request.state, "request_id", None) \
            or request_id_var.get()
        return JSONResponse(
            status_code=500,
            content={"detail": "internal error",
                     "code": "internal_error",
                     "request_id": rid},
            headers={"X-Request-Id": rid},
        )

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise ValueError("kaboom-secret-detail")

    # raise_server_exceptions=False so the handler runs (mimics prod)
    return TestClient(app, raise_server_exceptions=False)


def test_access_line_emitted_with_fields(client, caplog):
    with caplog.at_level(logging.INFO, logger="api.access"):
        r = client.get("/ok")
    assert r.status_code == 200
    recs = [r for r in caplog.records if r.name == "api.access"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec.method == "GET"
    assert rec.path == "/ok"
    assert rec.status == 200
    assert isinstance(rec.latency_ms, float)
    assert hasattr(rec, "request_id")


def test_response_carries_request_id_header(client):
    r = client.get("/ok")
    assert r.headers.get("X-Request-Id")


def test_inbound_request_id_honored_end_to_end(client):
    known = "deadbeef" * 4  # 32 valid chars
    r = client.get("/ok", headers={"X-Request-Id": known})
    assert r.headers["X-Request-Id"] == known


def test_500_shape_no_stacktrace_leak(client):
    r = client.get("/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "internal error"
    assert body["code"] == "internal_error"
    # a REAL generated id, not the unset-default "-"
    assert body["request_id"] and body["request_id"] != "-"
    assert len(body["request_id"]) == 32
    # the 500 response must carry the same id as the header
    assert r.headers["X-Request-Id"] == body["request_id"]
    # NEVER leak the exception text / stacktrace
    raw = json.dumps(body)
    assert "kaboom-secret-detail" not in raw
    assert "Traceback" not in raw
    assert "ValueError" not in raw


def test_access_line_on_500_path_defaults_status(client, caplog):
    with caplog.at_level(logging.INFO, logger="api.access"):
        client.get("/boom")
    recs = [r for r in caplog.records if r.name == "api.access"]
    assert len(recs) == 1
    # the finally-block default (500) is recorded even though the
    # handler produced the body downstream
    assert recs[0].status == 500
