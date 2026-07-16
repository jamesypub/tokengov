"""Diagnostics check-engine + per-category mapping tests.

Pure unit tests with fake ctx/clients (no testcontainers/AWS) — they
exercise the engine's error-isolation + summary aggregation + filters,
the per-category status mapping, and the read-only-verb invariant.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from diagnostics.model import (
    CheckResult, Check, run_all, PASS, WARN, FAIL, ERROR, INFO,
)


# ── Fakes ────────────────────────────────────────────────────────────

class FakeCtx:
    """A DiagContext stand-in. `clients` maps service→MagicMock; every
    attribute access on a client is recorded so a test can assert only
    read verbs were called."""

    def __init__(self, *, account_id="123456789012",
                 configured_account_id="123456789012",
                 region="us-east-1", tg_version="v1.1.1-gabcdef",
                 environment="prod", consumer_role_name="tg-consumer",
                 deny_policy_name="tg-BedrockQuotaDeny",
                 db_rows=None, clients=None):
        self.account_id = account_id
        self._configured_account_id = configured_account_id
        self.region = region
        self.tg_version = tg_version
        self.environment = environment
        self.consumer_role_name = consumer_role_name
        self.deny_policy_name = deny_policy_name
        self.athena_workgroup = "tg-cur-analytics"
        self.athena_database = "tg_cur"
        self.cur_table_name = "data"
        self._clients = clients or {}

    @property
    def configured_account_id(self):
        return self._configured_account_id

    def client(self, service):
        if service not in self._clients:
            self._clients[service] = MagicMock(name=f"{service}_client")
        return self._clients[service]


def _mk(status, cid="cat.slug", category="cat"):
    return CheckResult(
        id=cid, title="t", status=status, category=category,
        severity=INFO, detail="d", remediation="" if status == PASS
        else "fix")


def _check(cid, category, fn):
    return Check(cid, "t", category, INFO, fn)


# ── Engine: error isolation ──────────────────────────────────────────

def test_raising_check_becomes_error_and_does_not_abort():
    def ok(ctx):
        return _mk(PASS, "a.ok", "a")

    def boom(ctx):
        raise RuntimeError("kaboom")

    def also_ok(ctx):
        return _mk(PASS, "b.ok", "b")

    checks = [_check("a.ok", "a", ok),
              _check("a.boom", "a", boom),
              _check("b.ok", "b", also_ok)]
    out = run_all(FakeCtx(), checks=checks)
    by_id = {c["id"]: c for c in out["checks"]}
    assert by_id["a.boom"]["status"] == ERROR
    assert "kaboom" in by_id["a.boom"]["detail"]
    # The other two still ran.
    assert by_id["a.ok"]["status"] == PASS
    assert by_id["b.ok"]["status"] == PASS
    assert out["summary"]["error"] == 1
    assert out["summary"]["total"] == 3


def test_error_does_not_set_summary_status():
    # A broken check must not mask a real pass nor manufacture a fail.
    def boom(ctx):
        raise ValueError("x")

    def good(ctx):
        return _mk(PASS, "a.ok", "a")

    out = run_all(FakeCtx(), checks=[
        _check("a.boom", "a", boom), _check("a.ok", "a", good)])
    assert out["summary"]["status"] == PASS


def test_check_returning_non_checkresult_is_error():
    out = run_all(FakeCtx(), checks=[
        _check("a.bad", "a", lambda ctx: "not a result")])
    assert out["checks"][0]["status"] == ERROR


# ── Engine: summary aggregation (worst non-error) ────────────────────

@pytest.mark.parametrize("statuses,expected", [
    ([PASS, PASS], PASS),
    ([PASS, WARN], WARN),
    ([WARN, FAIL], FAIL),
    ([PASS, FAIL, WARN], FAIL),
    ([ERROR, PASS], PASS),      # error ignored
    ([ERROR], PASS),            # all-error → pass (nothing failing)
    ([FAIL, ERROR], FAIL),      # error doesn't mask the fail
])
def test_summary_status_is_worst_non_error(statuses, expected):
    checks = [
        _check(f"c.{i}", "c", (lambda s: (lambda ctx: _mk(s, f"c.{i}", "c")))(s))
        for i, s in enumerate(statuses)
    ]
    # rebind properly (avoid late-binding on i/s)
    checks = []
    for i, s in enumerate(statuses):
        def make(s=s, i=i):
            return lambda ctx: _mk(s, f"c.{i}", "c")
        checks.append(_check(f"c.{i}", "c", make()))
    out = run_all(FakeCtx(), checks=checks)
    assert out["summary"]["status"] == expected


def test_summary_counts_and_categories_with_issues():
    checks = [
        _check("a.p", "a", lambda ctx: _mk(PASS, "a.p", "a")),
        _check("a.w", "a", lambda ctx: _mk(WARN, "a.w", "a")),
        _check("b.f", "b", lambda ctx: _mk(FAIL, "b.f", "b")),
    ]
    out = run_all(FakeCtx(), checks=checks)
    s = out["summary"]
    assert (s["pass"], s["warn"], s["fail"], s["error"]) == (1, 1, 1, 0)
    assert s["categories_with_issues"] == ["a", "b"]


def test_every_result_has_checked_at_and_duration():
    out = run_all(FakeCtx(), checks=[
        _check("a.p", "a", lambda ctx: _mk(PASS, "a.p", "a"))])
    r = out["checks"][0]
    assert r["checked_at"]           # non-empty ISO stamp
    assert isinstance(r["duration_ms"], int)


def test_pass_has_empty_remediation_nonpass_has_it():
    out = run_all(FakeCtx(), checks=[
        _check("a.p", "a", lambda ctx: _mk(PASS, "a.p", "a")),
        _check("a.f", "a", lambda ctx: _mk(FAIL, "a.f", "a"))])
    by_id = {c["id"]: c for c in out["checks"]}
    assert by_id["a.p"]["remediation"] == ""
    assert by_id["a.f"]["remediation"] != ""


def test_wire_object_shape():
    out = run_all(FakeCtx(), checks=[
        _check("a.p", "a", lambda ctx: _mk(PASS, "a.p", "a"))])
    for k in ("schema_version", "generated_at", "tg_version",
              "account_id", "region", "summary", "checks"):
        assert k in out
    assert out["tg_version"] == "v1.1.1-gabcdef"
    assert out["account_id"] == "123456789012"


# ── Engine: filters ──────────────────────────────────────────────────

def _abc_checks():
    return [
        _check("a.one", "a", lambda ctx: _mk(PASS, "a.one", "a")),
        _check("b.one", "b", lambda ctx: _mk(PASS, "b.one", "b")),
        _check("c.one", "c", lambda ctx: _mk(PASS, "c.one", "c")),
    ]


def test_only_filters_by_category():
    out = run_all(FakeCtx(), only="a", checks=_abc_checks())
    assert [c["id"] for c in out["checks"]] == ["a.one"]


def test_only_filters_by_check_id():
    out = run_all(FakeCtx(), only="b.one", checks=_abc_checks())
    assert [c["id"] for c in out["checks"]] == ["b.one"]


def test_skip_omits_category():
    out = run_all(FakeCtx(), skip="a", checks=_abc_checks())
    assert sorted(c["id"] for c in out["checks"]) == ["b.one", "c.one"]


def test_only_accepts_comma_list():
    out = run_all(FakeCtx(), only="a,c", checks=_abc_checks())
    assert sorted(c["id"] for c in out["checks"]) == ["a.one", "c.one"]


# ── Read-only-verb invariant ─────────────────────────────────────────

_MUTATING_PREFIXES = (
    "attach", "detach", "put", "create", "update", "delete",
    "set_", "start_query_execution",  # start_query is fine for Athena;
    # excluded below — see note.
)

# Athena's read path legitimately uses start_query_execution +
# get_query_execution + get_query_results (a SELECT). Those are read
# semantics even though 'start' isn't a Get/List/Describe verb. We
# assert every *IAM/ECS/ECR/Glue/S3/STS* call is a read verb, and allow
# the Athena SELECT trio explicitly.
_ALLOWED_NON_READ = {
    "start_query_execution", "get_query_execution",
    "get_query_results",
}
_READ_PREFIXES = ("get", "list", "describe", "batch_get", "simulate")


def test_all_real_checks_use_only_read_verbs():
    """Run every real phase-1 check against fully-mocked clients and
    assert no mutating boto3 verb was invoked."""
    from diagnostics.checks import all_checks

    called = []

    class RecordingClient:
        def __init__(self, name):
            self._name = name

        def __getattr__(self, verb):
            def _call(*a, **kw):
                called.append((self._name, verb))
                # Return benign empty-ish shapes so checks don't crash;
                # any KeyError just becomes an ERROR result (fine here).
                return _FLEXIBLE
            return _call

    class Flexible(dict):
        def __getattr__(self, k):
            return _FLEXIBLE

        def __call__(self, *a, **kw):
            return _FLEXIBLE

        def get(self, *a, **kw):
            return _FLEXIBLE

        def __iter__(self):
            return iter([])

    _FLEXIBLE = Flexible()

    clients = {svc: RecordingClient(svc)
               for svc in ("sts", "iam", "ecs", "ecr", "athena",
                           "glue", "s3")}

    class DBCtx:
        def __enter__(self):
            return _DB

        def __exit__(self, *a):
            return False

    class _DBSession:
        def query(self, *a, **kw):
            return self

        def filter(self, *a, **kw):
            return self

        def order_by(self, *a, **kw):
            return self

        def first(self):
            return None

        def scalar(self):
            return None

    _DB = _DBSession()

    ctx = FakeCtx(clients=clients)
    ctx.db = lambda: DBCtx()

    run_all(ctx, checks=all_checks())

    bad = [
        (svc, verb) for (svc, verb) in called
        if verb not in _ALLOWED_NON_READ
        and not verb.startswith(_READ_PREFIXES)
    ]
    assert not bad, f"non-read boto3 verbs invoked: {bad}"
