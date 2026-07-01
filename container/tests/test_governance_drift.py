"""
Tests for #649 governance drift detection:
  - the shared verify helper (governance.verify)
  - the governance_drift_check worker job (incl. the shared-role
    reverse-direction guard)

IAM is stubbed; SQLAlchemy writes hit the real Postgres
testcontainer (same pattern as test_deny_reconciler).
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from db.session import get_db
from db.models import User, GovernanceDrift


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


def _fake_iam(attached_policy_names):
    """Stub IAM whose paginator yields the given attached-policy
    names for any role."""
    iam = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"AttachedPolicies": [
            {"PolicyName": n} for n in attached_policy_names
        ]},
    ]
    iam.get_paginator.return_value = paginator
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


def test_verify_drift_when_attached_but_not_governed():
    from governance import verify, DRIFT
    u = _mk_user(email="a@t.com", governed=False)
    iam = _fake_iam(["tg-BedrockQuotaDeny"])
    # raw verify reports DRIFT; the job applies the shared-role
    # guard before recording (see job tests below).
    assert verify(u, iam=iam) == DRIFT


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


def test_job_shared_role_guard_suppresses_reverse(clean_db, monkeypatch):
    """A governed principal and an ungoverned co-tenant share the
    deny-bearing role. The ungoverned one must NOT be flagged
    deny_no_governed — that's the shared-tg-consumer norm."""
    with get_db() as db:
        db.add(_mk_user(email="gov@t.com", governed=True))
        db.add(_mk_user(email="cotenant@t.com", governed=False))

    import worker.jobs.governance_drift_check as job
    iam = _fake_iam(["tg-BedrockQuotaDeny"])  # deny attached
    monkeypatch.setattr(job.boto3, "client", lambda *a, **k: iam)

    res = job.run()
    # gov@ is in-sync (managed); cotenant@ is suppressed by the
    # shared-role guard → zero drift.
    assert res["drift"] == 0, res
    assert res["deny_no_governed"] == 0


def test_job_flags_reverse_when_no_governed_cotenant(clean_db, monkeypatch):
    """deny attached, principal not governed, and NO governed
    principal shares the role → genuine deny_no_governed drift."""
    with get_db() as db:
        db.add(_mk_user(
            email="lonely@t.com", governed=False,
            principal_arn="arn:aws:iam::123456789012:role/SoloRole",
        ))

    import worker.jobs.governance_drift_check as job
    iam = _fake_iam(["tg-BedrockQuotaDeny"])
    monkeypatch.setattr(job.boto3, "client", lambda *a, **k: iam)

    res = job.run()
    assert res["drift"] == 1
    assert res["deny_no_governed"] == 1
    with get_db() as db:
        d = db.query(GovernanceDrift).one()
        assert d.direction == "deny_no_governed"


def test_job_idc_never_flagged(clean_db, monkeypatch):
    with get_db() as db:
        db.add(_mk_user(
            email="idc@corp.com", role_type="idc", governed=True))

    import worker.jobs.governance_drift_check as job
    iam = _fake_iam([])  # would be governed_no_deny if not IDC
    monkeypatch.setattr(job.boto3, "client", lambda *a, **k: iam)

    res = job.run()
    assert res["drift"] == 0
