"""
Tests for worker/jobs/deny_reconciler.py — builds the
tg-BedrockQuotaDeny IAM policy from Postgres state.

Per memory: must use aws:userid (not aws:PrincipalArn) so
the deny matches assumed-role sessions whose name we set to
the email at sts:AssumeRole time.

IAM is stubbed; the SQLAlchemy writes hit the real Postgres
testcontainer.
"""
from __future__ import annotations
import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_iam(monkeypatch):
    """Stub boto3.client('iam'). The policy ARN itself is
    expected to already exist (created by tg-bedrock-role
    CFN); reconciler only edits its document. Default:
    return an existing "no-op" policy doc, an empty version
    list, and let create_policy_version succeed.
    """
    iam = MagicMock()
    iam.get_policy.return_value = {
        "Policy": {"DefaultVersionId": "v1"},
    }
    iam.get_policy_version.return_value = {
        "PolicyVersion": {"Document": {
            "Version": "2012-10-17",
            "Statement": [{
                "Sid":       "QuotaDenyNoop",
                "Effect":    "Deny",
                "Action":    "bedrock:InvokeModel",
                "Resource":  "*",
                "Condition": {
                    "StringEquals": {"aws:userid": "none"},
                },
            }],
        }},
    }
    iam.list_policy_versions.return_value = {"Versions": []}
    import worker.jobs.deny_reconciler as dr
    monkeypatch.setattr(
        dr.boto3, "client", lambda *a, **kw: iam
    )
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    return iam


def _seed_user(email, cap, status="active", governed=True):
    # #836: enforcement is now gated on `governed`. This file tests
    # the enforcement paths, so seeds default to governed=True (the
    # managed principal whose deny the reconciler maintains). The
    # #836 tests pass governed=False explicitly to prove an
    # unmanaged principal is never denied/blocked.
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(
            email=email, status=status, cap_usd=cap,
            governed=governed))


def _seed_metric(email, model, spend, usage_hour=None):
    # #643: per-day grain. Default to today (within the current
    # month → counts toward the MTD cap sum the reconciler reads).
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import CurUserSpend
    if usage_hour is None:
        usage_hour = datetime.now(timezone.utc).date()
    with get_db() as db:
        db.add(CurUserSpend(
            email=email, usage_hour=usage_hour, model_id=model,
            input_tokens=0, output_tokens=0,
            total_tokens=0,
            spend_usd=spend,
        ))


def _captured_doc(iam):
    """Pull the PolicyDocument from the most recent
    create_policy_version call."""
    call = iam.create_policy_version.call_args
    return json.loads(call.kwargs["PolicyDocument"])


def test_no_over_cap_users_writes_noop(clean_db, fake_iam):
    """If no user is over cap and the existing policy is
    already the no-op shape, reconciler reports 'no change'.
    Otherwise it would create a new version."""
    _seed_user("alice@test.com", cap=10.0)
    _seed_metric("alice@test.com", "m1", spend=1.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()

    assert "0 denied" in out["detail"]
    # Existing doc already matches desired no-op → no version
    # created.
    fake_iam.create_policy_version.assert_not_called()


def test_over_cap_user_lands_in_deny(clean_db, fake_iam):
    """User over cap → Deny statement uses StringLike
    aws:userid with '*:<email>' principal."""
    _seed_user("over@test.com", cap=5.0)
    _seed_user("under@test.com", cap=10.0)
    _seed_metric("over@test.com", "m1", spend=6.0)
    _seed_metric("under@test.com", "m1", spend=2.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()

    assert "1 denied" in out["detail"]
    assert out["blocked"] == ["over@test.com"]

    doc = _captured_doc(fake_iam)
    stmt = doc["Statement"][0]
    assert stmt["Sid"] == "QuotaDeny"
    # CRITICAL: must use aws:userid, NOT aws:PrincipalArn —
    # see memory feedback_aws_principalarn_vs_userid.
    assert "aws:userid" in stmt["Condition"]["StringLike"]
    assert "aws:PrincipalArn" not in stmt["Condition"].get(
        "StringLike", {})
    ids = stmt["Condition"]["StringLike"]["aws:userid"]
    assert ids == ["*:over@test.com"]


def test_force_blocked_user_always_denied(clean_db, fake_iam):
    """#750: User.status=force_blocked → always in Deny (manual
    admin override), even UNDER cap with near-zero spend."""
    _seed_user("dead@test.com", cap=10.0, status="force_blocked")
    _seed_metric("dead@test.com", "m1", spend=0.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()

    assert out["blocked"] == ["dead@test.com"]


def test_should_deny_is_clean_2way(clean_db):
    """#750: _should_deny is force_blocked OR over-cap — no
    temp-unblock reprieve. Unit-level so the behavior model the
    ticket specifies is pinned."""
    from datetime import datetime, timezone
    import worker.jobs.deny_reconciler as dr
    from db.models import User
    # force_blocked → denied regardless of spend (under cap).
    assert dr._should_deny(
        User(email="a", status="force_blocked"), 0.0, 10.0) is True
    # active + over cap → denied.
    assert dr._should_deny(
        User(email="b", status="active"), 11.0, 10.0) is True
    # active + under cap → allowed.
    assert dr._should_deny(
        User(email="c", status="active"), 1.0, 10.0) is False
    # blocked (auto over-cap label) + STILL over cap → denied;
    # there is no unblock_expires_at reprieve any more.
    assert dr._should_deny(
        User(email="d", status="blocked"), 11.0, 10.0) is True
    # the now-removed value 'disabled' is not special — it's just
    # not force_blocked, so the cap governs it.
    assert dr._should_deny(
        User(email="e", status="disabled"), 1.0, 10.0) is False
    _ = datetime  # silence unused import in case of edits


def test_unblock_respects_cap_over_cap_user_redenied(
    clean_db, fake_iam
):
    """#750 ACCEPTANCE: unblocking an over-cap user (status reset
    to active by the /unblock endpoint) does NOT let them through
    — the reconcile tick re-blocks them because spend >= cap, and
    flips status back to blocked. No cap-free window."""
    _seed_user("over@test.com", cap=5.0, status="active")
    _seed_metric("over@test.com", "m1", spend=9.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert out["blocked"] == ["over@test.com"]
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        u = db.query(User).filter(
            User.email == "over@test.com").first()
        assert u.status == "blocked"  # auto-flipped back


def test_unblock_under_cap_user_stays_allowed(clean_db, fake_iam):
    """#750: an under-cap user left active is NOT denied."""
    _seed_user("ok@test.com", cap=100.0, status="active")
    _seed_metric("ok@test.com", "m1", spend=1.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert "0 denied" in out["detail"]


def test_default_cap_used_when_user_cap_unset(
    clean_db, fake_iam
):
    """User with cap_usd=None falls back to the DEFAULT
    QuotaPolicy row's monthly_cap_usd."""
    from db.session import get_db
    from db.models import QuotaPolicy
    with get_db() as db:
        db.add(QuotaPolicy(
            scope="DEFAULT", monthly_cap_usd=3.0,
        ))
    _seed_user("d@test.com", cap=None)
    _seed_metric("d@test.com", "m1", spend=4.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert out["blocked"] == ["d@test.com"]


def test_create_policy_failure_surfaces_real_code(
    clean_db, monkeypatch
):
    """When the policy is missing AND iam:CreatePolicy fails
    (e.g. AccessDenied because the task role lacks the perm),
    the resulting RuntimeError must mention CreatePolicy /
    AccessDenied — NOT the misleading GetPolicy code from the
    outer wrapper. Reproduces #229."""
    from botocore.exceptions import ClientError as Boto3ClientError

    # NOTE: do NOT put "Operation" inside the Error dict —
    # boto3 itself does not populate that key on real
    # ClientErrors. Operation lives on
    # ClientError.operation_name (the second positional
    # arg). Putting it on the dict would mistest-positive
    # any code that looked it up the wrong way.
    iam = MagicMock()
    iam.get_policy.side_effect = Boto3ClientError(
        {"Error": {"Code": "NoSuchEntity",
                   "Message": "policy not found"}},
        "GetPolicy",
    )
    iam.create_policy.side_effect = Boto3ClientError(
        {"Error": {"Code": "AccessDenied",
                   "Message": "iam:CreatePolicy denied"}},
        "CreatePolicy",
    )
    import worker.jobs.deny_reconciler as dr
    monkeypatch.setattr(
        dr.boto3, "client", lambda *a, **kw: iam
    )
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")

    _seed_user("a@test.com", cap=10.0)
    _seed_metric("a@test.com", "m1", spend=1.0)

    with pytest.raises(RuntimeError) as exc_info:
        dr.run()
    msg = str(exc_info.value)
    # The wrapped except must name CreatePolicy as the failing
    # operation so an operator reading the Jobs error column
    # knows the self-heal step blew up (vs. a steady-state
    # GetPolicy / CreatePolicyVersion failure). Without the
    # wrap, the outer handler reports "IAM error (...) on <arn>"
    # — operation context lives only in the boto3 str() suffix.
    # With the wrap, the operation name is hoisted into the
    # primary "on <op>" slot.
    assert "AccessDenied" in msg, (
        f"expected the real CreatePolicy code in error, got: {msg}"
    )
    assert "on CreatePolicy" in msg, (
        "expected the failing operation (CreatePolicy) to be "
        "named explicitly in the error so an operator can tell "
        f"self-heal failed without parsing the suffix, got: {msg}"
    )


def test_active_user_over_cap_promoted_to_blocked(
    clean_db, fake_iam
):
    """When the policy adds a user to Deny because they're
    over cap AND status=active, the User row's status is
    flipped to 'blocked' so the UI shows the right state."""
    _seed_user("flip@test.com", cap=5.0, status="active")
    _seed_metric("flip@test.com", "m1", spend=6.0)

    import worker.jobs.deny_reconciler as dr
    dr.run()

    from db.session import get_db
    from db.models import User
    with get_db() as db:
        u = db.query(User).filter(
            User.email == "flip@test.com").first()
        assert u.status == "blocked"


# ── #746 model-DENYLIST deny (reverses #626 allow-list) ──────

def _seed_blocked(ids):
    from db.session import get_db
    from db.org_config import set_blocked_models
    with get_db() as db:
        set_blocked_models(db, ids)


def _stmt_by_sid(doc, sid):
    for s in doc.get("Statement", []):
        if s.get("Sid") == sid:
            return s
    return None


# Catalog model_ids (not ARNs) — what the block-list now stores.
_MODEL_A = "us.anthropic.claude-sonnet-4-6"
_MODEL_B = "global.anthropic.claude-opus-4-8"


def test_no_blocklist_configured_emits_no_model_statement(
    clean_db, fake_iam
):
    """#746: with no block-list configured, the policy carries
    ONLY the per-person quota statement — no DenyBlockedModels.
    Empty block-list = allow every model (fail-open)."""
    _seed_user("over@test.com", cap=5.0)
    _seed_metric("over@test.com", "m1", spend=6.0)

    import worker.jobs.deny_reconciler as dr
    dr.run()

    doc = _captured_doc(fake_iam)
    assert _stmt_by_sid(doc, "DenyBlockedModels") is None
    assert _stmt_by_sid(doc, "QuotaDeny") is not None


def test_blocklist_emits_deny_blocked_models(clean_db, fake_iam):
    """#746: when a block-list is set, the policy gains a
    DenyBlockedModels statement: Deny on the four invoke actions
    with Resource = region/profile-agnostic *model* wildcards
    (both inference-profile and foundation-model)."""
    _seed_blocked([_MODEL_A, _MODEL_B])
    # No one over cap — proves the model statement is emitted
    # independent of the per-person quota class.
    _seed_user("ok@test.com", cap=100.0)
    _seed_metric("ok@test.com", "m1", spend=1.0)

    import worker.jobs.deny_reconciler as dr
    dr.run()

    doc = _captured_doc(fake_iam)
    stmt = _stmt_by_sid(doc, "DenyBlockedModels")
    assert stmt is not None
    assert stmt["Effect"] == "Deny"
    # Resource (denylist), NOT NotResource (allowlist).
    assert "NotResource" not in stmt
    res = set(stmt["Resource"])
    # Geo prefix stripped → agnostic token; both resource types.
    assert (
        "arn:aws:bedrock:*:*:inference-profile/"
        "*anthropic.claude-sonnet-4-6*" in res)
    assert (
        "arn:aws:bedrock:*::foundation-model/"
        "*anthropic.claude-sonnet-4-6*" in res)
    # global.* opus reduces to the SAME token as us.* would.
    assert (
        "arn:aws:bedrock:*:*:inference-profile/"
        "*anthropic.claude-opus-4-8*" in res)
    # No identity condition — the model deny is role-wide.
    assert "Condition" not in stmt


def test_blocklist_token_is_region_and_profile_agnostic(
    clean_db, fake_iam
):
    """#746 CRITICAL (the stage-freeze root cause): a us.* entry
    and a global.* entry of the same model must reduce to the
    same agnostic token so the deny matches under ANY profile /
    region — the old allow-list pinned us-east-1/us.* and missed
    the real us-west-2/global.* traffic."""
    import worker.jobs.deny_reconciler as dr
    assert (dr._agnostic_token("us.anthropic.claude-opus-4-8")
            == "anthropic.claude-opus-4-8")
    assert (dr._agnostic_token("global.anthropic.claude-opus-4-8")
            == "anthropic.claude-opus-4-8")
    assert (dr._agnostic_token("apac.anthropic.claude-x")
            == "anthropic.claude-x")
    # A bare foundation-model id (no geo prefix) is unchanged.
    assert (dr._agnostic_token("anthropic.claude-v2")
            == "anthropic.claude-v2")


def test_blocklist_deny_lists_converse_explicitly(
    clean_db, fake_iam
):
    """#746 CRITICAL (AWS gotcha): Converse / ConverseStream are
    NOT auto-blocked by an InvokeModel deny — they're distinct
    IAM actions. The model-denylist Deny must list all four
    invoke actions or a blocked model stays reachable via
    Converse."""
    _seed_blocked([_MODEL_A])
    _seed_user("ok@test.com", cap=100.0)
    _seed_metric("ok@test.com", "m1", spend=1.0)

    import worker.jobs.deny_reconciler as dr
    dr.run()

    doc = _captured_doc(fake_iam)
    actions = set(
        _stmt_by_sid(doc, "DenyBlockedModels")["Action"])
    assert actions == {
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
    }


def test_blocklist_and_quota_coexist(clean_db, fake_iam):
    """#746: the model-denylist statement and the per-person
    quota statement coexist in the same policy document — both
    statement classes present, model first."""
    _seed_blocked([_MODEL_A])
    _seed_user("over@test.com", cap=5.0)
    _seed_metric("over@test.com", "m1", spend=6.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert out["blocked"] == ["over@test.com"]

    doc = _captured_doc(fake_iam)
    sids = [s.get("Sid") for s in doc["Statement"]]
    assert "DenyBlockedModels" in sids
    assert "QuotaDeny" in sids
    # Per-person quota statement still keys on aws:userid.
    q = _stmt_by_sid(doc, "QuotaDeny")
    assert q["Condition"]["StringLike"]["aws:userid"] == \
        ["*:over@test.com"]


# ── #627 per-principal quota keying ──────────────────────

def _seed_principal_user(
    email, identity_key, principal_type, principal_arn,
    cap, status="active", governed=True, role_type="iam",
):
    # #836: default governed=True (enforcement tests); #836 cases
    # pass governed=False to prove the no-enforcement gate.
    # #1011: role_type="idc" seeds an AWSReservedSSO_* principal.
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(
            email=email, identity_key=identity_key,
            principal_type=principal_type,
            principal_arn=principal_arn,
            status=status, cap_usd=cap, governed=governed,
            role_type=role_type,
        ))


def test_email_pinned_over_cap_keys_on_userid(
    clean_db, fake_iam
):
    """#627: a human (email-pinned) over cap is denied via
    aws:userid '*:<email>' — per-person, cross-role."""
    _seed_principal_user(
        "human@test.com", "human@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer", cap=5.0)
    _seed_metric("human@test.com", "m1", spend=6.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert out["blocked"] == ["human@test.com"]

    doc = _captured_doc(fake_iam)
    q = next(s for s in doc["Statement"]
             if s["Sid"] == "QuotaDeny")
    assert q["Condition"]["StringLike"]["aws:userid"] == \
        ["*:human@test.com"]


def test_machine_session_over_cap_keys_on_userid(
    clean_db, fake_iam
):
    """#810 (reverses #627): a machine principal over cap is now
    denied via aws:userid on its last-segment session name — the
    SAME uniform keying as a human. The per-role aws:PrincipalArn
    QuotaDenyByRole statement is GONE."""
    # Machine rows carry email == identity_key (cur_spend_sync's
    # `email = email or identity_key`), and spend is keyed on email.
    _seed_principal_user(
        "i-0819dd4c", "i-0819dd4c", "service",
        "arn:aws:iam::123:role/MyBatchRole", cap=5.0)
    _seed_metric("i-0819dd4c", "m1", spend=6.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert out["blocked"] == ["i-0819dd4c"]

    doc = _captured_doc(fake_iam)
    sids = {s["Sid"] for s in doc["Statement"]}
    # Uniform aws:userid keying — NO per-role PrincipalArn statement.
    assert "QuotaDenyByRole" not in sids
    q = next(s for s in doc["Statement"] if s["Sid"] == "QuotaDeny")
    assert q["Condition"]["StringLike"]["aws:userid"] == \
        ["*:i-0819dd4c"]
    # Never key a machine deny on the role ARN.
    assert "ArnLike" not in q.get("Condition", {})


def test_mixed_principals_share_one_userid_statement(
    clean_db, fake_iam
):
    """#810: humans AND machine sessions over cap in the same cycle
    land in ONE QuotaDeny statement keyed aws:userid — no separate
    QuotaDenyByRole. Both session names appear in the same list."""
    _seed_principal_user(
        "human@test.com", "human@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer", cap=5.0)
    _seed_metric("human@test.com", "m1", spend=6.0)
    _seed_principal_user(
        "i-0batch", "i-0batch", "service",
        "arn:aws:iam::123:role/Batch", cap=5.0)
    _seed_metric("i-0batch", "m1", spend=6.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert set(out["blocked"]) == {"human@test.com", "i-0batch"}

    doc = _captured_doc(fake_iam)
    sids = {s["Sid"] for s in doc["Statement"]}
    assert "QuotaDenyByRole" not in sids
    q = next(s for s in doc["Statement"] if s["Sid"] == "QuotaDeny")
    ids = set(q["Condition"]["StringLike"]["aws:userid"])
    assert ids == {"*:human@test.com", "*:i-0batch"}


def test_quota_deny_lists_converse_explicitly(
    clean_db, fake_iam
):
    """#627: the per-person quota deny must list Converse /
    ConverseStream explicitly (same AWS gotcha as #626)."""
    _seed_principal_user(
        "human@test.com", "human@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer", cap=5.0)
    _seed_metric("human@test.com", "m1", spend=6.0)

    import worker.jobs.deny_reconciler as dr
    dr.run()
    doc = _captured_doc(fake_iam)
    q = next(s for s in doc["Statement"]
             if s["Sid"] == "QuotaDeny")
    assert set(q["Action"]) == {
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
    }


# ── #643 cap enforcement preserved across the grain change ──

def test_monthly_cap_sums_per_day_rows(clean_db, fake_iam):
    """#643 HARD constraint: with the per-day grain, the
    reconciler's monthly spend total must be the SUM of this
    month's per-day rows — and still block an over-cap user. Seed
    $3 + $3 on two different days this month (= $6) against a $5
    cap → denied."""
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone.utc).date()
    # two days this month; if today is the 1st, use today + a
    # same-month neighbour so both land in the MTD window.
    d1 = today
    d2 = today - timedelta(days=1)
    if d2.month != today.month:
        d2 = today + timedelta(days=1)
    _seed_user("split@test.com", cap=5.0)
    _seed_metric("split@test.com", "m1", spend=3.0, usage_hour=d1)
    _seed_metric("split@test.com", "m1", spend=3.0, usage_hour=d2)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    # $6 monthly > $5 cap → denied.
    assert out["blocked"] == ["split@test.com"]


def test_prior_month_spend_excluded_from_cap(clean_db, fake_iam):
    """#643: a big spend dated LAST month must NOT count toward
    this month's cap (the month-start filter excludes it) — so an
    otherwise-quiet user is not falsely blocked."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date()
    first_of_month = today.replace(day=1)
    # one day before this month started = last month.
    last_month = first_of_month - __import__("datetime").timedelta(days=1)
    _seed_user("quiet@test.com", cap=5.0)
    _seed_metric(
        "quiet@test.com", "m1", spend=999.0, usage_hour=last_month)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    # last month's $999 is excluded → under cap → not blocked.
    assert "0 denied" in out["detail"]


# ── #349 role-rename env-var precedence ──────────────────

def _resolve_role_name():
    """Re-import deny_reconciler with the current env so the
    module-level ROLE_NAME is recomputed from os.environ.
    Returns the resolved value."""
    import importlib, sys
    sys.modules.pop("worker.jobs.deny_reconciler", None)
    import worker.jobs.deny_reconciler as dr
    importlib.reload(dr)
    return dr.ROLE_NAME


def test_role_name_prefers_new_env_var(monkeypatch):
    """#349: TG_TOKEN_CONSUMER_ROLE_NAME beats the legacy
    BEDROCK_ROLE_NAME if both are set."""
    monkeypatch.setenv(
        "TG_TOKEN_CONSUMER_ROLE_NAME", "tg-Custom-New")
    monkeypatch.setenv("BEDROCK_ROLE_NAME", "tg-Custom-Old")
    assert _resolve_role_name() == "tg-Custom-New"


def test_role_name_legacy_env_var_still_honored(
    monkeypatch, caplog
):
    """#349: BEDROCK_ROLE_NAME alone is still honored for
    one release; emits a deprecation warning."""
    import logging
    monkeypatch.delenv(
        "TG_TOKEN_CONSUMER_ROLE_NAME", raising=False)
    monkeypatch.setenv(
        "BEDROCK_ROLE_NAME", "tg-Custom-Legacy")
    with caplog.at_level(logging.WARNING):
        assert _resolve_role_name() == "tg-Custom-Legacy"
    assert any(
        "BEDROCK_ROLE_NAME is deprecated" in r.getMessage()
        for r in caplog.records
    ), "expected deprecation warning when only the legacy " \
       "env var is set"


def test_role_name_default_is_token_consumer(monkeypatch):
    """#349: with neither env var set, falls back to the
    new default name."""
    monkeypatch.delenv(
        "TG_TOKEN_CONSUMER_ROLE_NAME", raising=False)
    monkeypatch.delenv("BEDROCK_ROLE_NAME", raising=False)
    assert _resolve_role_name() == "tg-consumer"


# ── #746 _ensure_policy: admin-role guard + loud attach-fail ─

def _mk_iam(attached_arns=(), attach_error=None):
    """A MagicMock iam whose list_attached_role_policies paginator
    yields `attached_arns`, and whose attach_role_policy raises
    `attach_error` (a ClientError) if given."""
    from unittest.mock import MagicMock
    iam = MagicMock()
    page = {"AttachedPolicies": [
        {"PolicyArn": a} for a in attached_arns]}
    paginator = MagicMock()
    paginator.paginate.return_value = [page]
    iam.get_paginator.return_value = paginator
    # create_policy: pretend it already exists (steady state).
    from botocore.exceptions import ClientError
    iam.create_policy.side_effect = ClientError(
        {"Error": {"Code": "EntityAlreadyExists"}}, "CreatePolicy")
    if attach_error is not None:
        iam.attach_role_policy.side_effect = attach_error
    return iam


def test_ensure_policy_attaches_to_any_role_no_admin_guard(monkeypatch):
    """#809 (reverses #746 defect-1 / #804): _ensure_policy attaches
    to the configured consumer role with NO AdministratorAccess
    refusal — even when the role carries AdministratorAccess the
    attach proceeds (denylist semantics make a role-wide attach
    safe, not a freeze)."""
    import worker.jobs.deny_reconciler as dr
    iam = _mk_iam(attached_arns=[
        "arn:aws:iam::aws:policy/AdministratorAccess"])
    dr._ensure_policy(iam, "123456789012", "{}")
    iam.attach_role_policy.assert_called_once()


def test_ensure_policy_raises_on_attach_failure(monkeypatch):
    """#746 defect 3: a failed attach (e.g. NoSuchEntity from a
    misconfigured TG_TOKEN_CONSUMER_ROLE_NAME) is raised LOUDLY,
    not swallowed as a warning — else governance is silently off."""
    from botocore.exceptions import ClientError
    import worker.jobs.deny_reconciler as dr
    err = ClientError(
        {"Error": {"Code": "NoSuchEntity"}}, "AttachRolePolicy")
    iam = _mk_iam(attached_arns=[], attach_error=err)
    with pytest.raises(RuntimeError, match="governance is OFF"):
        dr._ensure_policy(iam, "123456789012", "{}")


def test_ensure_policy_attach_already_exists_is_ok(monkeypatch):
    """#746: EntityAlreadyExists on attach = already attached
    (idempotent) — must NOT raise."""
    from botocore.exceptions import ClientError
    import worker.jobs.deny_reconciler as dr
    err = ClientError(
        {"Error": {"Code": "EntityAlreadyExists"}},
        "AttachRolePolicy")
    iam = _mk_iam(attached_arns=[], attach_error=err)
    dr._ensure_policy(iam, "123456789012", "{}")  # no raise


# ───────── #809: attach to any role a governed user assumes ─────────

def test_role_name_from_assumed_role_arn():
    """#809: the role-name parser handles role + assumed-role ARNs."""
    import worker.jobs.deny_reconciler as dr
    assert dr._role_name_from_arn(
        "arn:aws:iam::123:role/tg-install") == "tg-install"
    assert dr._role_name_from_arn(
        "arn:aws:sts::123:assumed-role/tg-install/sess") == "tg-install"
    assert dr._role_name_from_arn(None) is None
    assert dr._role_name_from_arn("not-an-arn") is None


class _AttachIam:
    """IAM fake for the #809 attach/detach reconcile. Steady-state
    bundled policy; records attach/detach; reports a configurable
    current attachment set via list_entities_for_policy."""
    def __init__(self, currently_attached=None):
        self.attached = []
        self.detached = []
        self._cur = set(currently_attached or [])

    def get_policy(self, PolicyArn):
        return {"Policy": {"DefaultVersionId": "v1"}}

    def get_policy_version(self, PolicyArn, VersionId):
        return {"PolicyVersion": {"Document": {
            "Version": "2012-10-17", "Statement": [{
                "Sid": "QuotaDenyNoop", "Effect": "Deny",
                "Action": "bedrock:InvokeModel", "Resource": "*",
                "Condition": {"StringEquals": {"aws:userid": "none"}},
            }]}}}

    def list_policy_versions(self, PolicyArn):
        return {"Versions": []}

    def create_policy_version(self, **kw):
        pass

    def attach_role_policy(self, RoleName, PolicyArn):
        self.attached.append((RoleName, PolicyArn))

    def detach_role_policy(self, RoleName, PolicyArn):
        self.detached.append((RoleName, PolicyArn))

    def get_caller_identity(self):
        return {"Account": "123456789012"}

    def get_paginator(self, name):
        outer = self

        class _P:
            def paginate(self, PolicyArn, EntityFilter):
                return [{"PolicyRoles": [
                    {"RoleName": r} for r in outer._cur]}]
        return _P()


def _run_with_attach_iam(monkeypatch, iam):
    import worker.jobs.deny_reconciler as dr
    monkeypatch.setattr(dr.boto3, "client", lambda *a, **kw: iam)
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    return dr.run()


def test_deny_attached_to_users_actual_role(clean_db, monkeypatch):
    """#809: a force-blocked user who assumes tg-install gets
    tg-BedrockQuotaDeny attached to tg-install (the role they
    actually use), not just the configured consumer role."""
    _seed_principal_user(
        "tg-org-admin@test.com", "tg-org-admin@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-install", cap=5.0,
        status="force_blocked")
    iam = _AttachIam(currently_attached=set())
    _run_with_attach_iam(monkeypatch, iam)
    arn = ("arn:aws:iam::123456789012:policy/tg-BedrockQuotaDeny")
    roles_attached = {r for r, a in iam.attached if a == arn}
    assert "tg-install" in roles_attached       # her actual role
    assert "tg-consumer" in roles_attached       # baseline kept


def test_admin_role_attaches_without_refusal(clean_db, monkeypatch):
    """#809: even when the role would be an admin role, the attach
    proceeds — no AdministratorAccess refusal anywhere."""
    _seed_principal_user(
        "boss@test.com", "boss@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-install", cap=5.0,
        status="force_blocked")
    iam = _AttachIam(currently_attached=set())
    _run_with_attach_iam(monkeypatch, iam)
    assert any(r == "tg-install" for r, _ in iam.attached)


def test_deny_detached_from_unreferenced_role(clean_db, monkeypatch):
    """#809: a role the deny is attached to, but that no governed/
    blocked user assumes anymore, is detached — but the configured
    consumer role is NEVER detached."""
    _seed_principal_user(
        "ok@test.com", "ok@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer", cap=100.0)
    _seed_metric("ok@test.com", "m1", spend=1.0)
    iam = _AttachIam(currently_attached={"stale-role", "tg-consumer"})
    _run_with_attach_iam(monkeypatch, iam)
    arn = "arn:aws:iam::123456789012:policy/tg-BedrockQuotaDeny"
    assert ("stale-role", arn) in iam.detached
    # consumer role is never detached (baseline + self-heal target)
    assert ("tg-consumer", arn) not in iam.detached


# ── #1011: governed IDC users — QuotaDeny yes, role attach no ────

def test_governed_idc_user_emits_quota_deny_but_no_attach(
    clean_db, monkeypatch
):
    """#1011: a governed IDC (AWSReservedSSO_*) user over cap gets its
    per-person QuotaDeny statement (keyed on aws:userid), but the
    reconciler does NOT attach tg-BedrockQuotaDeny to the
    AWSReservedSSO_* role (a direct attach is wiped on re-provision,
    #618). Enforcement lands via tg-consumer / the #1010 permission-
    set reference instead."""
    _seed_principal_user(
        "idc@test.com", "idc@test.com", "assumed_role",
        "arn:aws:iam::123:role/AWSReservedSSO_Dev_abc",
        cap=5.0, status="force_blocked", role_type="idc")
    iam = _AttachIam(currently_attached=set())
    _run_with_attach_iam(monkeypatch, iam)
    # the per-person deny statement IS emitted (keyed on the identity)
    doc = iam.create_policy_version  # composed doc applied via this
    # the AWSReservedSSO_* role is NEVER attached
    assert not any(
        r.startswith("AWSReservedSSO_") for r, _ in iam.attached)
    # baseline consumer role still attached (governance stays on)
    arn = "arn:aws:iam::123456789012:policy/tg-BedrockQuotaDeny"
    assert ("tg-consumer", arn) in iam.attached
    assert doc is not None  # version-apply path exercised


def test_idc_role_never_detached_even_if_currently_attached(
    clean_db, monkeypatch
):
    """#1011: the counterintuitive trap — dropping IDC roles from the
    keep-set means any AWSReservedSSO_* role that happens to be
    attached falls into `attached - keep`, so the DETACH pass would
    race IDC's re-provision (the mirror of the attach problem). The
    detach pass must skip AWSReservedSSO_* roles in BOTH directions."""
    _seed_principal_user(
        "ok@test.com", "ok@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer", cap=100.0)
    _seed_metric("ok@test.com", "m1", spend=1.0)
    # an AWSReservedSSO_* role is currently attached (e.g. left by a
    # prior direct attach, or the IDC permission-set reference).
    iam = _AttachIam(currently_attached={
        "AWSReservedSSO_Dev_abc", "tg-consumer"})
    _run_with_attach_iam(monkeypatch, iam)
    # tg must NOT detach the IDC role (IDC owns that attachment).
    assert not any(
        r.startswith("AWSReservedSSO_") for r, _ in iam.detached)


# ── #827: unmanage-cleared force_block → no residual deny ────

def test_unmanaged_under_cap_principal_has_no_deny(
    clean_db, fake_iam
):
    """#827 ACCEPTANCE: a principal that was force_blocked then
    unmanaged (status reset to active, governed=False, under cap)
    must NOT appear in the rebuilt policy — neither a QuotaDeny
    aws:userid entry nor (machine) a QuotaDenyByRole entry. This is
    the source-level proof of the tg-org-admin+dev still-denied bug:
    once unmanage clears force_block, _should_deny is False, so the
    reconciler drops the principal's statement on its next tick."""
    # State as the #827 unmanage_principal leaves it: not over cap,
    # active, not governed.
    _seed_principal_user(
        "freed@test.com", "freed@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-install", cap=100.0,
        status="active")
    _seed_metric("freed@test.com", "m1", spend=1.0)
    # A SECOND, genuinely over-cap user so the policy IS rebuilt
    # (a 0-denied run leaves the existing noop doc untouched, so
    # create_policy_version wouldn't fire and there'd be no doc to
    # inspect). We assert the freed principal is absent from the
    # rebuilt doc while the over-cap one is present.
    _seed_principal_user(
        "other@test.com", "other@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer", cap=5.0,
        status="active")
    _seed_metric("other@test.com", "m1", spend=6.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert out["blocked"] == ["other@test.com"]

    doc = _captured_doc(fake_iam)
    # sanity: the over-cap user IS denied (proves the doc is real)
    q = next(s for s in doc["Statement"] if s["Sid"] == "QuotaDeny")
    assert "*:other@test.com" in \
        q["Condition"]["StringLike"]["aws:userid"]
    # No userid entry for the freed principal anywhere in the doc.
    for stmt in doc["Statement"]:
        like = (stmt.get("Condition") or {}).get("StringLike") or {}
        ids = like.get("aws:userid") or []
        if isinstance(ids, str):
            ids = [ids]
        assert "*:freed@test.com" not in ids
        # and no per-role ArnLike entry for its role
        arnlike = (stmt.get("Condition") or {}).get("ArnLike") or {}
        parns = arnlike.get("aws:PrincipalArn") or []
        if isinstance(parns, str):
            parns = [parns]
        assert "arn:aws:iam::123:role/tg-install" not in parns


def test_still_force_blocked_principal_is_denied(clean_db, fake_iam):
    """#827 guard: the fix does NOT weaken force_block — a principal
    that is STILL force_blocked (i.e. NOT unmanaged) stays denied,
    even under cap. Only unmanage clears it; force_block alone must
    keep denying (#750 unchanged)."""
    _seed_principal_user(
        "blocked@test.com", "blocked@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-install", cap=100.0,
        status="force_blocked")
    _seed_metric("blocked@test.com", "m1", spend=1.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert out["blocked"] == ["blocked@test.com"]
    doc = _captured_doc(fake_iam)
    q = next(s for s in doc["Statement"] if s["Sid"] == "QuotaDeny")
    assert q["Condition"]["StringLike"]["aws:userid"] == \
        ["*:blocked@test.com"]


# ── #836: `governed` gates ALL enforcement ──────────────────

def test_unmanaged_over_cap_not_denied_not_blocked(clean_db, fake_iam):
    """#836 ACCEPTANCE: an UNMANAGED (governed=False) over-cap
    principal is NOT denied and NOT auto-blocked — tg enforces
    nothing on a principal it doesn't govern. No aws:userid entry;
    status stays active."""
    _seed_user("ungov@test.com", cap=5.0, status="active",
               governed=False)
    _seed_metric("ungov@test.com", "m1", spend=9.0)   # over cap

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    # not counted as denied
    assert "ungov@test.com" not in out["blocked"]
    assert "0 denied" in out["detail"]
    # status NOT flipped to blocked
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        u = db.query(User).filter(
            User.email == "ungov@test.com").first()
        assert u.status == "active"
    # 0 denied → the policy stays the no-op shape (no
    # create_policy_version call), so there is no userid entry for
    # the ungoverned principal. (A doc-level assertion needs a
    # genuinely-denied co-tenant to force a rebuild — covered by
    # test_unmanaged_coexists_with_a_governed_deny below.)


def test_unmanaged_coexists_with_a_governed_deny(clean_db, fake_iam):
    """#836 doc-level: with a genuinely-denied GOVERNED co-tenant
    forcing a policy rebuild, the rebuilt doc denies the governed
    principal but contains NO entry for the ungoverned over-cap one."""
    _seed_user("ungov@test.com", cap=5.0, status="active",
               governed=False)
    _seed_metric("ungov@test.com", "m1", spend=9.0)     # over cap
    _seed_user("gov@test.com", cap=5.0, status="active",
               governed=True)
    _seed_metric("gov@test.com", "m1", spend=9.0)        # over cap

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert out["blocked"] == ["gov@test.com"]            # only governed

    doc = _captured_doc(fake_iam)
    q = next(s for s in doc["Statement"] if s["Sid"] == "QuotaDeny")
    ids = q["Condition"]["StringLike"]["aws:userid"]
    assert "*:gov@test.com" in ids
    assert "*:ungov@test.com" not in ids


def test_unmanaged_blocked_row_self_heals_to_active(clean_db, fake_iam):
    """#836 ACCEPTANCE: a pre-existing contradictory row — unmanaged
    (governed=False) but status=blocked — is reconciled back to
    active on the next tick (and carries no deny), clearing the
    screenshot bug with no manual action."""
    _seed_user("stale@test.com", cap=5.0, status="blocked",
               governed=False)
    _seed_metric("stale@test.com", "m1", spend=9.0)   # still over cap

    import worker.jobs.deny_reconciler as dr
    dr.run()

    from db.session import get_db
    from db.models import User
    with get_db() as db:
        u = db.query(User).filter(
            User.email == "stale@test.com").first()
        assert u.status == "active"     # self-healed


def test_unmanaged_force_blocked_row_self_heals(clean_db, fake_iam):
    """#836: an ungoverned principal stuck at force_blocked is also
    reconciled to active + force_blocked_at cleared (an unmanaged
    principal can't be enforced, so the manual block can't stand)."""
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(
            email="fbstale@test.com", status="force_blocked",
            cap_usd=5.0, governed=False,
            force_blocked_at=datetime.now(timezone.utc)))
    _seed_metric("fbstale@test.com", "m1", spend=1.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert "fbstale@test.com" not in out["blocked"]
    with get_db() as db:
        u = db.query(User).filter(
            User.email == "fbstale@test.com").first()
        assert u.status == "active"
        assert u.force_blocked_at is None


def test_managed_over_cap_still_blocked_and_denied(clean_db, fake_iam):
    """#836 no-regression: a MANAGED (governed=True) over-cap
    principal is still flipped to blocked AND denied."""
    _seed_user("gov@test.com", cap=5.0, status="active",
               governed=True)
    _seed_metric("gov@test.com", "m1", spend=9.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert out["blocked"] == ["gov@test.com"]
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        u = db.query(User).filter(
            User.email == "gov@test.com").first()
        assert u.status == "blocked"
    doc = _captured_doc(fake_iam)
    q = next(s for s in doc["Statement"] if s["Sid"] == "QuotaDeny")
    assert "*:gov@test.com" in \
        q["Condition"]["StringLike"]["aws:userid"]


def test_managed_force_blocked_still_denied(clean_db, fake_iam):
    """#836 no-regression (#750/#827): a MANAGED force_blocked
    principal is still denied even under cap."""
    _seed_user("govfb@test.com", cap=100.0, status="force_blocked",
               governed=True)
    _seed_metric("govfb@test.com", "m1", spend=1.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert out["blocked"] == ["govfb@test.com"]


# ── spend-estimate enforcement ──────────────────────────────────────


def _seed_hours(email, model, per_hour, n_hours, end=None):
    """Seed n_hours of billed CurUserSpend at per_hour $, one row per
    hour ending `end` (default now). Used to give a principal a stable
    trailing-window rate for the projection."""
    from datetime import datetime, timezone, timedelta
    from db.session import get_db
    from db.models import CurUserSpend
    if end is None:
        end = datetime.now(timezone.utc)
    with get_db() as db:
        for i in range(n_hours):
            db.add(CurUserSpend(
                email=email, model_id=model,
                usage_hour=end - timedelta(hours=i + 1),
                input_tokens=0, output_tokens=0, total_tokens=0,
                spend_usd=per_hour))


def _set_estimate_cfg(strategy=None, enforcement=None):
    from db.session import get_db
    from db.org_config import (
        set_spend_estimate_strategy, set_spend_estimate_enforcement)
    with get_db() as db:
        if strategy is not None:
            set_spend_estimate_strategy(db, strategy)
        if enforcement is not None:
            set_spend_estimate_enforcement(db, enforcement)


def test_off_mode_never_denies_on_estimate(clean_db, fake_iam):
    """Default off: a user UNDER cap on billed but whose projection
    would exceed it is NOT denied (estimate is display-only)."""
    from datetime import datetime, timezone, timedelta
    _set_estimate_cfg(strategy="average", enforcement="off")
    # billed = 12 hrs × $5 = $60 (under cap 100). History ends 8h ago,
    # so there's an unbilled gap whose projection ($5/hr × ~8h = ~$40)
    # would push 60+40=100 to the edge — but off mode ignores it.
    _seed_user("offuser@test.com", cap=100.0)
    _seed_hours("offuser@test.com", "m1", per_hour=5.0, n_hours=12,
                end=datetime.now(timezone.utc) - timedelta(hours=10))

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert "offuser@test.com" not in out["blocked"]


def test_warn_mode_never_denies_on_estimate(clean_db, fake_iam):
    """warn: alert/UI only — still no estimate-driven IAM deny."""
    from datetime import datetime, timezone, timedelta
    _set_estimate_cfg(strategy="average", enforcement="warn")
    # billed $60 < cap 100; projection over the 8h gap would exceed it
    # but warn must not produce an IAM deny.
    _seed_user("warnuser@test.com", cap=100.0)
    _seed_hours("warnuser@test.com", "m1", per_hour=5.0, n_hours=12,
                end=datetime.now(timezone.utc) - timedelta(hours=10))

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert "warnuser@test.com" not in out["blocked"]


def test_enforce_mode_denies_on_projected(clean_db, fake_iam):
    """enforce: billed under cap but billed+estimated >= cap → denied.
    History ends ~10h ago, so the unbilled gap (~10h, capped at 36h)
    times the $20/hr rate pushes billed $240 over a cap set at $300."""
    from datetime import datetime, timezone, timedelta
    _set_estimate_cfg(strategy="average", enforcement="enforce")
    # billed 12×$20 = $240 < cap 300; gap ~10h × $20 = ~$200 → ~$440
    # projected >= 300 → denied on the estimate.
    _seed_user("enf@test.com", cap=300.0)
    _seed_hours("enf@test.com", "m1", per_hour=20.0, n_hours=12,
                end=datetime.now(timezone.utc) - timedelta(hours=10))

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert "enf@test.com" in out["blocked"]


def test_enforce_mode_under_cap_on_projection_not_denied(
    clean_db, fake_iam
):
    """enforce but the projection still stays under cap → not denied
    (the estimate widens the number, it doesn't auto-block)."""
    from datetime import datetime, timezone, timedelta
    _set_estimate_cfg(strategy="average", enforcement="enforce")
    _seed_user("enfok@test.com", cap=100000.0)
    _seed_hours("enfok@test.com", "m1", per_hour=1.0, n_hours=12,
                end=datetime.now(timezone.utc) - timedelta(hours=10))

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert "enfok@test.com" not in out["blocked"]


# ── spend-cap alert notifications ───────────────────────────────────
# The event-decision is a pure helper (_spend_alert_events); the
# integration tests below run the reconciler with notify.send_alert
# stubbed to record (to, subject, body) and assert the
# warn-once-latch / blocked / unblocked / recipient behavior.


def test_spend_alert_events_warn_on_cross():
    """warn fires when ratio >= warn% and the latch isn't set."""
    import worker.jobs.deny_reconciler as dr
    ev = dr._spend_alert_events(
        "active", "active", effective_spend=8.0, cap=10.0,
        warn_pct=80, exceeded_on=True, warn_latched=False)
    assert ev == ["warn"]


def test_spend_alert_events_warn_latched_no_repeat():
    """warn does NOT re-fire while the latch is set."""
    import worker.jobs.deny_reconciler as dr
    ev = dr._spend_alert_events(
        "active", "active", effective_spend=9.0, cap=10.0,
        warn_pct=80, exceeded_on=True, warn_latched=True)
    assert ev == []


def test_spend_alert_events_blocked_supersedes_warn():
    """active→blocked emits blocked (not warn) when exceeded on."""
    import worker.jobs.deny_reconciler as dr
    ev = dr._spend_alert_events(
        "active", "blocked", effective_spend=12.0, cap=10.0,
        warn_pct=80, exceeded_on=True, warn_latched=False)
    assert ev == ["blocked"]


def test_spend_alert_events_blocked_suppressed_when_off():
    """exceeded-email off → no blocked event on active→blocked."""
    import worker.jobs.deny_reconciler as dr
    ev = dr._spend_alert_events(
        "active", "blocked", effective_spend=12.0, cap=10.0,
        warn_pct=80, exceeded_on=False, warn_latched=False)
    assert ev == []


def test_spend_alert_events_unblocked():
    """blocked→active emits unblocked regardless of exceeded flag."""
    import worker.jobs.deny_reconciler as dr
    ev = dr._spend_alert_events(
        "blocked", "active", effective_spend=1.0, cap=10.0,
        warn_pct=80, exceeded_on=False, warn_latched=False)
    assert ev == ["unblocked"]


def test_spend_alert_events_uncapped_never_warns():
    """cap=0 guards the division and never warns."""
    import worker.jobs.deny_reconciler as dr
    ev = dr._spend_alert_events(
        "active", "active", effective_spend=5.0, cap=0.0,
        warn_pct=80, exceeded_on=True, warn_latched=False)
    assert ev == []


@pytest.fixture
def record_alerts(monkeypatch):
    """Stub notify.send_alert to record (to, subject, body) and
    report sent:True so the latch/status writes proceed."""
    calls = []

    def _fake(to, subject, body):
        calls.append({"to": to, "subject": subject, "body": body})
        return {"sent": True, "to": to}

    import worker.notify as notify
    monkeypatch.setattr(notify, "send_alert", _fake)
    return calls


def _seed_team(team_id, parent=None):
    from db.session import get_db
    from db.models import Team
    with get_db() as db:
        db.add(Team(team_id=team_id, name=team_id,
                    parent_team_id=parent))


def _seed_user_team(email, cap, team_id, status="active"):
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(
            email=email, status=status, cap_usd=cap,
            governed=True, team_id=team_id))


def _seed_admin(email, role, team_id=None):
    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        db.add(AdminRole(email=email, role=role, team_id=team_id))


def _user_status(email):
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        u = db.query(User).filter(User.email == email).first()
        return (u.status, u.last_warn_sent_at, u.last_status_notified)


def test_warn_fires_once_then_latches(clean_db, fake_iam, record_alerts):
    """A governed user crossing the warn threshold (80% of cap)
    gets a warn email this tick, and NOT again on a repeat tick."""
    _seed_user("warn@test.com", cap=10.0)
    _seed_metric("warn@test.com", "m1", spend=8.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    warns = [c for c in record_alerts
             if "approaching" in c["subject"].lower()]
    assert len(warns) == 1  # user only (no admin seeded → org fallback)
    assert out["alerts_sent"] == 1
    _, latch, _ = _user_status("warn@test.com")
    assert latch is not None

    # Repeat tick: still over warn%, latch set → no new warn.
    record_alerts.clear()
    dr.run()
    assert record_alerts == []


def test_warn_recipients_include_user_and_team_admin(
    clean_db, fake_iam, record_alerts
):
    """A warn notifies the user AND their team admin."""
    _seed_team("t1")
    _seed_user_team("dev@test.com", cap=10.0, team_id="t1")
    _seed_admin("lead@test.com", "team_admin", team_id="t1")
    _seed_metric("dev@test.com", "m1", spend=9.0)

    import worker.jobs.deny_reconciler as dr
    dr.run()
    tos = {c["to"] for c in record_alerts}
    assert "dev@test.com" in tos
    assert "lead@test.com" in tos


def test_blocked_event_on_active_to_blocked(
    clean_db, fake_iam, record_alerts
):
    """A user going over cap (active→blocked) gets a blocked email
    once; a repeat tick (still blocked) does not re-email."""
    _seed_user("over@test.com", cap=5.0)
    _seed_metric("over@test.com", "m1", spend=6.0)

    import worker.jobs.deny_reconciler as dr
    dr.run()
    blocked = [c for c in record_alerts
               if "paused" in c["subject"].lower()
               or "cap" in c["subject"].lower()]
    assert len(blocked) >= 1
    status, _, notified = _user_status("over@test.com")
    assert status == "blocked"
    assert notified == "blocked"

    record_alerts.clear()
    dr.run()
    assert record_alerts == []  # already-notified → no repeat


def test_unblocked_event_on_blocked_to_active(
    clean_db, fake_iam, record_alerts
):
    """A previously-blocked user dropping under cap (blocked→active)
    gets an unblocked email once."""
    _seed_user("back@test.com", cap=10.0, status="blocked")
    _seed_metric("back@test.com", "m1", spend=1.0)

    import worker.jobs.deny_reconciler as dr
    dr.run()
    restored = [c for c in record_alerts
                if "restored" in c["subject"].lower()
                or "back under" in c["subject"].lower()]
    assert len(restored) >= 1
    status, _, notified = _user_status("back@test.com")
    assert status == "active"
    assert notified == "active"


def test_warn_keyed_off_projected_in_enforce_mode(
    clean_db, fake_iam, record_alerts
):
    """In enforce mode the warn threshold is measured against the
    PROJECTED spend: billed alone is under warn%, but billed +
    estimated crosses it → a warn fires."""
    from datetime import datetime, timezone, timedelta
    _set_estimate_cfg(strategy="average", enforcement="enforce")
    # cap 300, warn 80% → warn line at $240. billed 12×$11 = $132 is
    # UNDER the warn line, but the ~10h unbilled gap × $11/hr ≈ +$110
    # → ~$242 projected, which crosses the warn line yet stays under
    # the $300 cap (so a warn fires but no block).
    _seed_user("proj@test.com", cap=300.0)
    _seed_hours("proj@test.com", "m1", per_hour=11.0, n_hours=12,
                end=datetime.now(timezone.utc) - timedelta(hours=10))

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert "proj@test.com" not in out["blocked"]  # under cap
    warns = [c for c in record_alerts
             if "approaching" in c["subject"].lower()]
    assert len(warns) >= 1  # warned on the projection, not billed
