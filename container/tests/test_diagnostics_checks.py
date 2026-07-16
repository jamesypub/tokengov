"""Per-category diagnostics check mapping tests.

Stubs the reused helpers (cur_health_result, governance.verify, DB
reads, ECS/ECR reads) so each check's status mapping is asserted in
isolation — no testcontainers/AWS.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from diagnostics.model import PASS, WARN, FAIL


class FakeCtx:
    """A DiagContext stand-in (local copy — tests/ isn't a package)."""

    def __init__(self, *, account_id="123456789012",
                 configured_account_id="123456789012",
                 region="us-east-1", tg_version="v1.1.1-gabcdef",
                 environment="prod", consumer_role_name="tg-consumer",
                 deny_policy_name="tg-BedrockQuotaDeny", clients=None):
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


# ── identity ─────────────────────────────────────────────────────────

def _sts(account="123456789012", arn="arn:aws:sts::123456789012:x"):
    c = MagicMock()
    c.get_caller_identity.return_value = {"Account": account, "Arn": arn}
    return c


def test_identity_caller_pass_sets_account():
    from diagnostics.checks import identity
    ctx = FakeCtx(account_id="", clients={"sts": _sts()})
    r = identity.check_caller(ctx)
    assert r.status == PASS
    assert ctx.account_id == "123456789012"


def test_identity_caller_fail_on_expired_creds():
    from diagnostics.checks import identity
    from botocore.exceptions import ClientError
    c = MagicMock()
    c.get_caller_identity.side_effect = ClientError(
        {"Error": {"Code": "ExpiredToken"}}, "GetCallerIdentity")
    ctx = FakeCtx(clients={"sts": c})
    r = identity.check_caller(ctx)
    assert r.status == FAIL
    assert "credential" in r.remediation.lower()


def test_identity_account_match_fail_on_mismatch():
    from diagnostics.checks import identity
    ctx = FakeCtx(configured_account_id="123456789012",
                  clients={"sts": _sts(account="123456789012")})
    r = identity.check_account_match(ctx)
    assert r.status == FAIL
    assert "123456789012" in r.detail and "123456789012" in r.detail


def test_identity_region_fail_off_us_east_1():
    from diagnostics.checks import identity
    r = identity.check_region(FakeCtx(region="us-west-2"))
    assert r.status == FAIL


# ── cur_pipeline: health-state mapping ───────────────────────────────

@pytest.mark.parametrize("state,expected", [
    ("healthy", PASS),
    ("principal_blank", WARN),
    ("absent", FAIL),
    ("columns_missing", FAIL),
    ("principal_absent_from_export", FAIL),
])
def test_cur_health_state_maps(monkeypatch, state, expected):
    from diagnostics.checks import cur_pipeline
    import api.routes.cur as cur_mod
    monkeypatch.setattr(
        cur_mod, "cur_health_result",
        lambda: {"status": state, "detail": None if state == "healthy"
                 else f"{state} detail"})
    r = cur_pipeline.check_health(FakeCtx())
    assert r.status == expected
    if expected != PASS:
        assert r.remediation  # non-pass carries a remediation


def _freshness_ctx(hours_ago):
    ctx = FakeCtx()
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
          if hours_ago is not None else None)

    class _DB:
        def __enter__(self_):
            return _S(ts)

        def __exit__(self_, *a):
            return False

    class _S:
        def __init__(self_, ts):
            self_._ts = ts

        def query(self_, *a):
            return self_

        def scalar(self_):
            return self_._ts
    ctx.db = lambda: _DB()
    return ctx


@pytest.mark.parametrize("hours,expected", [
    (2, PASS), (30, WARN), (72, FAIL), (None, FAIL),
])
def test_cur_freshness_maps(hours, expected):
    from diagnostics.checks import cur_pipeline
    r = cur_pipeline.check_freshness(_freshness_ctx(hours))
    assert r.status == expected


# ── governance: reconciler-last-run + jobs_paused ────────────────────

class _Row:
    def __init__(self, status, started_at, error=""):
        self.status = status
        self.started_at = started_at
        self.error = error
        self.detail = error


def _recon_ctx(row, paused_until=None):
    ctx = FakeCtx()

    class _DB:
        def __enter__(self_):
            return _S(row)

        def __exit__(self_, *a):
            return False

    class _S:
        def __init__(self_, row):
            self_._row = row

        def query(self_, *a):
            return self_

        def filter(self_, *a):
            return self_

        def order_by(self_, *a):
            return self_

        def first(self_):
            return self_._row
    ctx.db = lambda: _DB()
    return ctx, paused_until


def test_reconciler_recent_success_pass(monkeypatch):
    from diagnostics.checks import governance as g
    import db.jobs_pause as jp
    monkeypatch.setattr(jp, "get_jobs_paused_until", lambda db: None)
    row = _Row("succeeded", datetime.now(timezone.utc) - timedelta(minutes=10))
    ctx, _ = _recon_ctx(row)
    assert g.check_reconciler_last_run(ctx).status == PASS


def test_reconciler_failed_fail(monkeypatch):
    from diagnostics.checks import governance as g
    import db.jobs_pause as jp
    monkeypatch.setattr(jp, "get_jobs_paused_until", lambda db: None)
    row = _Row("failed", datetime.now(timezone.utc) - timedelta(minutes=5),
               "boom")
    ctx, _ = _recon_ctx(row)
    r = g.check_reconciler_last_run(ctx)
    assert r.status == FAIL and "boom" in r.detail


def test_reconciler_no_row_but_paused_is_not_fail(monkeypatch):
    from diagnostics.checks import governance as g
    import db.jobs_pause as jp
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(jp, "get_jobs_paused_until", lambda db: future)
    ctx, _ = _recon_ctx(None)
    r = g.check_reconciler_last_run(ctx)
    assert r.status != FAIL       # paused window → warn, not fail


def test_reconciler_no_row_not_paused_is_fail(monkeypatch):
    from diagnostics.checks import governance as g
    import db.jobs_pause as jp
    monkeypatch.setattr(jp, "get_jobs_paused_until", lambda db: None)
    ctx, _ = _recon_ctx(None)
    assert g.check_reconciler_last_run(ctx).status == FAIL


def test_governance_idc_boundary_warns_on_reserved_sso():
    from diagnostics.checks import governance as g
    ctx = FakeCtx(consumer_role_name="AWSReservedSSO_Admin_abc")
    assert g.check_idc_boundary(ctx).status == WARN
    ctx2 = FakeCtx(consumer_role_name="tg-consumer")
    assert g.check_idc_boundary(ctx2).status == PASS


# ── governance: idc-reference ────────────────────────────────────────
#
# A governed IDC user whose deny hasn't reached a role they use →
# WARN "pending". One whose SSO role carries the deny → PASS. Stubs
# ctx.db() (a governed IDC user row) + the iam reads.

class _IdcUser:
    def __init__(self, ident, arn, governed=True, role_type="idc"):
        self.identity_key = ident
        self.email = ident
        self.principal_arn = arn
        self.governed = governed
        self.role_type = role_type


class _CtxWithDb(FakeCtx):
    def __init__(self, users, **kw):
        super().__init__(**kw)
        self._users = users

    def db(self):
        users = self._users

        class _Session:
            def query(self, model):
                return self

            def filter(self, *a, **k):
                return self

            def all(self):
                return users

        import contextlib

        @contextlib.contextmanager
        def _cm():
            yield _Session()
        return _cm()


_IDC_ARN = ("arn:aws:iam::123456789012:role/aws-reserved/"
            "sso.amazonaws.com/AWSReservedSSO_Dev_abc123")


def _iam_for_idc(*, sso_attached, consumer_attached):
    """A MagicMock iam whose ListAttachedRolePolicies (on the SSO role)
    and ListEntitiesForPolicy (deny → tg-consumer) return the given
    attachment states."""
    iam = MagicMock()

    def _paginator(op):
        p = MagicMock()
        if op == "list_attached_role_policies":
            pols = ([{"PolicyName": "tg-BedrockQuotaDeny"}]
                    if sso_attached else [])
            p.paginate.return_value = [{"AttachedPolicies": pols}]
        else:  # list_entities_for_policy
            roles = ([{"RoleName": "tg-consumer"}]
                     if consumer_attached else [])
            p.paginate.return_value = [{"PolicyRoles": roles}]
        return p
    iam.get_paginator.side_effect = _paginator
    return iam


def test_idc_reference_pending_when_deny_not_on_role():
    from diagnostics.checks import governance as g
    ctx = _CtxWithDb(
        [_IdcUser("dev@corp.com", _IDC_ARN)],
        clients={"iam": _iam_for_idc(
            sso_attached=False, consumer_attached=False)})
    r = g.check_idc_reference(ctx)
    assert r.status == WARN
    assert "not yet enforced" in r.detail
    # remediation is plain business language — no internal tg-* names
    assert "tg-BedrockQuotaDeny" not in r.remediation
    assert "identity administrator" in r.remediation


def test_idc_reference_pass_when_deny_on_sso_role():
    from diagnostics.checks import governance as g
    ctx = _CtxWithDb(
        [_IdcUser("dev@corp.com", _IDC_ARN)],
        clients={"iam": _iam_for_idc(
            sso_attached=True, consumer_attached=False)})
    assert g.check_idc_reference(ctx).status == PASS


def test_idc_reference_pass_when_no_governed_idc_users():
    from diagnostics.checks import governance as g
    ctx = _CtxWithDb([], clients={"iam": MagicMock()})
    r = g.check_idc_reference(ctx)
    assert r.status == PASS
    assert "No governed" in r.detail


def test_invocation_logs_pass_when_catalog_empty(monkeypatch):
    from diagnostics.checks import governance as g
    from db import invlogs_config as cfg
    monkeypatch.setattr(cfg, "get_invlogs_regions",
                        lambda db, acct="": [])
    r = g.check_invocation_logs(_CtxWithDb([]))
    assert r.status == PASS
    assert "not enabled" in r.detail


def test_invocation_logs_warn_when_enabled_region_not_live(monkeypatch):
    from diagnostics.checks import governance as g
    from db import invlogs_config as cfg
    monkeypatch.setattr(
        cfg, "get_invlogs_regions",
        lambda db, acct="": [{"region": "us-east-1",
                              "bucket": "tg-bedrock-invlogs-us-east-1-1",
                              "enabled": True, "text_on": True}])
    br = MagicMock()
    br.get_model_invocation_logging_configuration.return_value = {}
    sess = MagicMock()
    sess.client.return_value = br
    ctx = _CtxWithDb([])
    ctx._session_factory = lambda: sess
    r = g.check_invocation_logs(ctx)
    assert r.status == WARN
    assert "not live" in r.detail


def test_invocation_logs_pass_when_live_to_our_bucket(monkeypatch):
    from diagnostics.checks import governance as g
    from db import invlogs_config as cfg
    b = "tg-bedrock-invlogs-us-east-1-1"
    monkeypatch.setattr(
        cfg, "get_invlogs_regions",
        lambda db, acct="": [{"region": "us-east-1", "bucket": b,
                              "enabled": True, "text_on": True}])
    br = MagicMock()
    br.get_model_invocation_logging_configuration.return_value = {
        "loggingConfig": {"s3Config": {"bucketName": b}}}
    sess = MagicMock()
    sess.client.return_value = br
    ctx = _CtxWithDb([])
    ctx._session_factory = lambda: sess
    assert g.check_invocation_logs(ctx).status == PASS


def test_idc_reference_client_build_failure_is_warn_not_raise():
    # ctx.client("iam") raising (ProfileNotFound / NoCredentials) must
    # degrade to a WARN "couldn't verify", never propagate as an error.
    from diagnostics.checks import governance as g

    class _CtxBoom(_CtxWithDb):
        def client(self, service):
            raise RuntimeError("ProfileNotFound")
    ctx = _CtxBoom([_IdcUser("dev@corp.com", _IDC_ARN)])
    r = g.check_idc_reference(ctx)
    assert r.status == WARN
    assert "client unavailable" in r.detail


# ── app_runtime: image-sync ──────────────────────────────────────────

def test_image_sync_fail_on_digest_mismatch(monkeypatch):
    from diagnostics.checks import app_runtime as ar
    ctx = FakeCtx()
    monkeypatch.setattr(ar, "_running_image_ref",
                        lambda c: "acct.dkr.ecr.us-east-1.amazonaws.com/tg-container:latest")
    monkeypatch.setattr(ar, "_ecr_repo_and_ref",
                        lambda img: ("tg-container", "latest"))
    monkeypatch.setattr(ar, "_digest_of_running",
                        lambda c, repo, ref: "sha256:aaaa")
    monkeypatch.setattr(ar, "_src_tag_digest",
                        lambda c, repo: ("sha256:bbbb", "src-de70-4c0f"))
    r = ar.check_image_sync(ctx)
    assert r.status == FAIL
    assert "stale" in r.detail.lower() or "STALE" in r.detail


def test_image_sync_warn_when_label_unreadable(monkeypatch):
    from diagnostics.checks import app_runtime as ar
    ctx = FakeCtx()
    monkeypatch.setattr(ar, "_running_image_ref",
                        lambda c: "acct.dkr.ecr.us-east-1.amazonaws.com/tg-container:latest")
    monkeypatch.setattr(ar, "_ecr_repo_and_ref",
                        lambda img: ("tg-container", "latest"))
    monkeypatch.setattr(ar, "_digest_of_running",
                        lambda c, repo, ref: "sha256:same")
    monkeypatch.setattr(ar, "_src_tag_digest",
                        lambda c, repo: ("sha256:same", "src-de70-4c0f"))
    monkeypatch.setattr(ar, "_image_label_release", lambda c, repo, d: None)
    r = ar.check_image_sync(ctx)
    assert r.status == WARN     # digests match, LABEL can't-tell → warn


def test_image_sync_warn_when_not_on_ecs(monkeypatch):
    from diagnostics.checks import app_runtime as ar
    monkeypatch.setattr(ar, "_running_image_ref", lambda c: None)
    r = ar.check_image_sync(FakeCtx())
    assert r.status == WARN


def test_version_warn_unstamped_nonprod(monkeypatch):
    from diagnostics.checks import app_runtime as ar
    ctx = FakeCtx(tg_version="dev", environment="stage")
    assert ar.check_version(ctx).status == WARN
    ctx2 = FakeCtx(tg_version="v1.1.1-gabc", environment="prod")
    assert ar.check_version(ctx2).status == PASS
