"""
Tests for #649 governance drift detection:
  - the shared verify helper (governance.verify)
  - the governance_drift_check worker job

The reverse-direction (DENY_NO_GOVERNED) rule is
attachment-vs-STATEMENT: an ungoverned principal is drift only when
the deny policy DOCUMENT holds a per-aws:userid Deny naming them, not
merely because the policy is attached to a role they share (the
role-wide-attach / per-userid-enforce model).

IAM is stubbed; SQLAlchemy writes hit the real Postgres
testcontainer (same pattern as test_deny_reconciler).
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from db.session import get_db
from db.models import User, GovernanceDrift

# The fixtures use account 123456789012 in every ARN. verify() now
# constructs the deny-policy ARN deterministically from the account
# (STS GetCallerIdentity, or AWS_ACCOUNT_ID) instead of listing
# policies. Pin the account for the whole module so no test
# makes a live STS call.
_TEST_ACCOUNT = "123456789012"


@pytest.fixture(autouse=True)
def _pin_account(monkeypatch):
    monkeypatch.setenv("AWS_ACCOUNT_ID", _TEST_ACCOUNT)


def _mk_user(**kw):
    """Lightweight User row. principal_arn defaults to a role ARN
    so there's something to attach the deny to."""
    defaults = dict(
        email=kw.get("email"),
        status="active",
        identity_key=kw.get("email"),
        principal_type="assumed_role",
        principal_arn="arn:aws:iam::123456789012:role/tg-consumer",
        role_type="iam",
        governed=False,
    )
    defaults.update(kw)
    return User(**defaults)


def _deny_doc(denied_identity_keys):
    """A tg-BedrockQuotaDeny-shaped policy document: the QuotaDenyNoop
    placeholder plus one per-user QuotaDeny statement per identity_key
    (aws:userid = "*:<identity_key>"). An empty list → Noop-only (the
    attached-but-nobody-enforced case)."""
    stmts = [{
        "Sid": "QuotaDenyNoop",
        "Effect": "Deny",
        "Action": "bedrock:InvokeModel",
        "Resource": "*",
        "Condition": {"StringLike": {"aws:userid": "*:none"}},
    }]
    for k in denied_identity_keys:
        stmts.append({
            "Sid": "QuotaDeny",
            "Effect": "Deny",
            "Action": "bedrock:InvokeModel",
            "Resource": "*",
            "Condition": {"StringLike": {"aws:userid": f"*:{k}"}},
        })
    return {"Version": "2012-10-17", "Statement": stmts}


def _fake_iam(attached_policy_names, denied_identity_keys=None,
              policy_absent=False):
    """Stub IAM: `list_attached_role_policies` yields the given attached
    names for any role; `get_policy` / `get_policy_version` serve a
    document denying `denied_identity_keys` (default: none →
    QuotaDenyNoop-only). When `policy_absent=True`, `get_policy` raises
    NoSuchEntity — the "no deny built yet" case verify() must treat as
    benign (empty enforced set), NOT UNKNOWN.

    Note: verify() no longer calls list_policies (it constructs the ARN
    deterministically), so this stub intentionally does NOT
    serve a `list_policies` paginator; a test asserts it's never called.
    """
    denied_identity_keys = denied_identity_keys or []
    iam = MagicMock()

    attach_pages = [{"AttachedPolicies": [
        {"PolicyName": n} for n in attached_policy_names]}]

    def _get_paginator(op):
        p = MagicMock()
        if op == "list_attached_role_policies":
            p.paginate.return_value = attach_pages
        elif op == "list_policies":
            # Guard: if verify ever regresses to listing, fail loudly
            # rather than silently serving results.
            raise AssertionError(
                "verify() must not call iam:list_policies")
        else:
            p.paginate.return_value = []
        return p

    iam.get_paginator.side_effect = _get_paginator
    if policy_absent:
        iam.get_policy.side_effect = ClientError(
            {"Error": {"Code": "NoSuchEntity"}}, "GetPolicy")
    else:
        iam.get_policy.return_value = {
            "Policy": {"DefaultVersionId": "v1"}}
    iam.get_policy_version.return_value = {
        "PolicyVersion": {"Document": _deny_doc(denied_identity_keys)}}
    return iam


# ── verify helper ──────────────────────────────────────────

def test_verify_managed_when_governed_and_attached():
    from governance import verify, MANAGED
    u = _mk_user(email="a@t.com", governed=True)
    iam = _fake_iam(["tg-BedrockQuotaDeny"])
    assert verify(u, iam=iam) == MANAGED


def test_verify_drift_when_governed_but_not_attached():
    from governance import verify, DRIFT
    u = _mk_user(email="a@t.com", governed=True)
    iam = _fake_iam([])  # deny missing
    assert verify(u, iam=iam) == DRIFT


def test_verify_unmanaged_when_not_governed_and_not_attached():
    from governance import verify, UNMANAGED
    u = _mk_user(email="a@t.com", governed=False)
    iam = _fake_iam([])
    assert verify(u, iam=iam) == UNMANAGED


def test_verify_unmanaged_when_attached_but_no_userid_deny():
    # Mere attachment of the deny to the user's role is NOT
    # enforcement. With governed=false and NO per-aws:userid Deny
    # naming this user (QuotaDenyNoop-only doc), verify is UNMANAGED,
    # not DRIFT — the false positive this fix clears.
    from governance import verify, UNMANAGED
    u = _mk_user(email="a@t.com", governed=False)
    iam = _fake_iam(["tg-BedrockQuotaDeny"], denied_identity_keys=[])
    assert verify(u, iam=iam) == UNMANAGED


def test_verify_drift_when_attached_and_real_userid_deny():
    # True reverse drift: governed=false but the policy document holds
    # a per-aws:userid Deny naming THIS principal (identity_key).
    from governance import verify, DRIFT
    u = _mk_user(email="a@t.com", governed=False)
    iam = _fake_iam(
        ["tg-BedrockQuotaDeny"], denied_identity_keys=["a@t.com"])
    assert verify(u, iam=iam) == DRIFT


def test_verify_unmanaged_when_deny_names_only_other_user():
    # A per-user Deny exists, but for someone else — this ungoverned
    # co-tenant is denied nothing → UNMANAGED, not drift.
    from governance import verify, UNMANAGED
    u = _mk_user(email="a@t.com", governed=False)
    iam = _fake_iam(
        ["tg-BedrockQuotaDeny"], denied_identity_keys=["other@t.com"])
    assert verify(u, iam=iam) == UNMANAGED


def test_verify_idc_never_drift():
    from governance import verify, IDC
    u = _mk_user(email="d@corp.com", role_type="idc", governed=True)
    iam = _fake_iam([])  # would be DRIFT if not for IDC short-circuit
    assert verify(u, iam=iam) == IDC


def test_verify_no_role_is_unmanaged():
    from governance import verify, UNMANAGED
    u = _mk_user(
        email="b@t.com", principal_type="iam_user",
        principal_arn="arn:aws:iam::123456789012:user/b", governed=True,
    )
    iam = _fake_iam([])
    # No attachable role → can't be drift; in-sync unmanaged.
    assert verify(u, iam=iam) == UNMANAGED


def test_verify_unknown_on_iam_error():
    from botocore.exceptions import ClientError
    from governance import verify, UNKNOWN
    u = _mk_user(email="a@t.com", governed=True)
    iam = MagicMock()
    iam.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}},
        "ListAttachedRolePolicies",
    )
    # No grant yet → UNKNOWN, never a crash.
    assert verify(u, iam=iam) == UNKNOWN


# ── construct-ARN path ─────────────────────────────────────
# verify() locates the deny policy by constructing its ARN from the
# account (no iam:ListPolicies → no Resource:"*" grant needed).

def test_verify_never_calls_list_policies_on_reverse_path(monkeypatch):
    # The reverse-direction check reads the policy DOCUMENT. It must get
    # there via a constructed ARN + get_policy, NEVER list_policies. The
    # stub raises if list_policies is used; assert we still resolve.
    from governance import verify, DRIFT
    u = _mk_user(email="a@t.com", governed=False)
    iam = _fake_iam(
        ["tg-BedrockQuotaDeny"], denied_identity_keys=["a@t.com"])
    # get_policy is called with the deterministically-built ARN.
    assert verify(u, iam=iam) == DRIFT
    iam.get_policy.assert_called_once_with(
        PolicyArn=f"arn:aws:iam::{_TEST_ACCOUNT}:policy/tg-BedrockQuotaDeny")


def test_verify_policy_absent_is_unmanaged_not_unknown():
    # No deny policy has ever been built → get_policy raises NoSuchEntity.
    # This is a benign absent state: an ungoverned/attached principal is
    # enforced against nothing → UNMANAGED. It must NOT surface as UNKNOWN
    # (which would mean "couldn't read") and must NOT crash (AC-3 / AC-1).
    from governance import verify, UNMANAGED
    u = _mk_user(email="a@t.com", governed=False)
    iam = _fake_iam(["tg-BedrockQuotaDeny"], policy_absent=True)
    assert verify(u, iam=iam) == UNMANAGED


def test_verify_unknown_on_get_policy_access_denied():
    # A genuine read failure on the document (AccessDenied, not
    # NoSuchEntity) still resolves to UNKNOWN — verify stays honest when
    # it truly can't read the policy (AC-1). The log hint now correctly
    # names iam:GetPolicy* (list_policies is gone).
    from governance import verify, UNKNOWN
    u = _mk_user(email="a@t.com", governed=False)
    iam = _fake_iam(["tg-BedrockQuotaDeny"], denied_identity_keys=["a@t.com"])
    iam.get_policy.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "GetPolicy")
    assert verify(u, iam=iam) == UNKNOWN


def test_verify_falls_back_to_sts_when_account_env_unset(monkeypatch):
    # AWS_ACCOUNT_ID unset → the account resolves via STS
    # GetCallerIdentity (the deny_reconciler path). Stub STS so no live
    # call; assert the ARN is built from the STS-returned account.
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    import governance
    monkeypatch.setattr(governance, "ACCOUNT_ID", None)
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    monkeypatch.setattr(
        governance.boto3, "client",
        lambda *a, **k: sts if a and a[0] == "sts" else MagicMock())

    from governance import verify, DRIFT
    u = _mk_user(
        email="a@t.com", governed=False,
        principal_arn="arn:aws:iam::123456789012:role/tg-consumer")
    iam = _fake_iam(
        ["tg-BedrockQuotaDeny"], denied_identity_keys=["a@t.com"])
    assert verify(u, iam=iam) == DRIFT
    iam.get_policy.assert_called_once_with(
        PolicyArn="arn:aws:iam::123456789012:policy/tg-BedrockQuotaDeny")


# ── the job ────────────────────────────────────────────────
# These use the shared `clean_db` fixture (conftest) which loads
# the schema via pg_url and truncates between tests.

def test_job_records_governed_no_deny(clean_db, monkeypatch):
    with get_db() as db:
        db.add(_mk_user(email="gov@t.com", governed=True))

    import worker.jobs.governance_drift_check as job
    iam = _fake_iam([])  # deny NOT attached anywhere
    monkeypatch.setattr(job.boto3, "client", lambda *a, **k: iam)

    res = job.run()
    assert res["drift"] == 1
    assert res["governed_no_deny"] == 1
    with get_db() as db:
        rows = db.query(GovernanceDrift).all()
        assert len(rows) == 1
        assert rows[0].direction == "governed_no_deny"
        assert rows[0].email == "gov@t.com"


def test_job_cotenant_not_flagged_when_no_userid_deny(clean_db, monkeypatch):
    """A governed principal and an ungoverned co-tenant share the
    deny-bearing role, and the policy holds a per-user Deny only for
    the governed one. The ungoverned co-tenant must NOT be flagged —
    it's denied nothing (the false-positive class this fix clears)."""
    with get_db() as db:
        db.add(_mk_user(email="gov@t.com", governed=True))
        db.add(_mk_user(email="cotenant@t.com", governed=False))

    import worker.jobs.governance_drift_check as job
    # deny attached + a per-user Deny for gov@ only (governed users
    # aren't statement-checked; cotenant@ has no statement).
    iam = _fake_iam(
        ["tg-BedrockQuotaDeny"], denied_identity_keys=["gov@t.com"])
    monkeypatch.setattr(job.boto3, "client", lambda *a, **k: iam)

    res = job.run()
    # gov@ managed; cotenant@ has no aws:userid Deny → not drift.
    assert res["drift"] == 0, res
    assert res["deny_no_governed"] == 0


def test_job_noop_only_ungoverned_not_flagged(clean_db, monkeypatch):
    """Core case: an ungoverned user shares tg-consumer, the policy is
    attached but contains ONLY QuotaDenyNoop (no per-user Deny). The
    old code flagged this as deny_no_governed; now it must clear."""
    with get_db() as db:
        db.add(_mk_user(email="innocent@t.com", governed=False))

    import worker.jobs.governance_drift_check as job
    iam = _fake_iam(["tg-BedrockQuotaDeny"], denied_identity_keys=[])
    monkeypatch.setattr(job.boto3, "client", lambda *a, **k: iam)

    res = job.run()
    assert res["drift"] == 0, res
    assert res["deny_no_governed"] == 0


def test_job_flags_reverse_when_real_userid_deny(clean_db, monkeypatch):
    """deny attached AND the policy holds a per-aws:userid Deny naming
    this ungoverned principal → genuine deny_no_governed drift (the
    true-positive that must be retained)."""
    with get_db() as db:
        db.add(_mk_user(
            email="stale@t.com", governed=False,
            principal_arn="arn:aws:iam::123456789012:role/SoloRole",
        ))

    import worker.jobs.governance_drift_check as job
    iam = _fake_iam(
        ["tg-BedrockQuotaDeny"], denied_identity_keys=["stale@t.com"])
    monkeypatch.setattr(job.boto3, "client", lambda *a, **k: iam)

    res = job.run()
    assert res["drift"] == 1
    assert res["deny_no_governed"] == 1
    with get_db() as db:
        d = db.query(GovernanceDrift).one()
        assert d.direction == "deny_no_governed"
        # Detail must not tell the admin to "Ungovern" an already-
        # ungoverned user (AC-4); it describes the real enforcing stmt.
        assert "per-user Deny" in d.detail


def test_job_idc_never_flagged(clean_db, monkeypatch):
    with get_db() as db:
        db.add(_mk_user(
            email="idc@corp.com", role_type="idc", governed=True))

    import worker.jobs.governance_drift_check as job
    iam = _fake_iam([])  # would be governed_no_deny if not IDC
    monkeypatch.setattr(job.boto3, "client", lambda *a, **k: iam)

    res = job.run()
    assert res["drift"] == 0
