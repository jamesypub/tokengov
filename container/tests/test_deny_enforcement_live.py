"""
#705: LIVE deny-enforcement regression test — proves the deny-only
governance loop actually blocks Bedrock end-to-end against a real
assumed-role session, not just that the policy *document* is built
correctly.

`test_deny_reconciler.py` stubs IAM and asserts the `aws:userid`
statement is written — it can't prove enforcement. This test closes
that gap: it assumes the consumer role as the RESERVED
tg-uat-admin@example.com persona,
invokes Bedrock (baseline OK), adds that session's `aws:userid` to the
live `tg-BedrockQuotaDeny` policy (exactly what the reconciler writes
when spend > cap), and asserts the SAME session now gets
`AccessDeniedException` citing an explicit deny from
`tg-BedrockQuotaDeny` — then ALWAYS reverts.

SKIPPED BY DEFAULT — needs real stage AWS creds. Opt in with
`TG_LIVE_DENY_TEST=1` (mirrors the smoke-the-real-artifact rule). This
is the automation of the manual repro proven on stage acct
123456789012 / stage-dd0a84d (2026-06-07), inlined in #705.

Rails (non-negotiable — see #705 / SKILL.md):
- Reserved persona ONLY: role-session-name tg-uat-admin@example.com.
  Never cap/deny a real seeded user.
- Always revert in `finally`: a failed assertion must NOT leave the
  deny policy mutated — snapshot the baseline version, restore it
  unconditionally.
- Stage/dev only: never run against a prod-configured target.
- IAM 5-version ceiling: prune the oldest non-default version before
  each create-policy-version.
"""
from __future__ import annotations

import json
import os
import time

import pytest

_LIVE = os.environ.get("TG_LIVE_DENY_TEST") == "1"
pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="live deny-enforcement test — set TG_LIVE_DENY_TEST=1 with "
           "stage AWS creds to run (needs real Bedrock + IAM).",
)

# Reserved UAT persona — NEVER a real seeded user (rail 2). The
# session name becomes the `aws:userid` suffix the deny matches.
UAT_SESSION = "tg-uat-admin@example.com"
MODEL_ID = os.environ.get(
    "TG_LIVE_MODEL_ID",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0")
CONSUMER_ROLE = os.environ.get("TG_CONSUMER_ROLE_NAME", "tg-consumer")
DENY_POLICY = os.environ.get("DENY_POLICY_NAME", "tg-BedrockQuotaDeny")
REGION = os.environ.get("AWS_REGION", "us-east-1")
# IAM propagation after a policy-version change is eventually
# consistent; the manual repro needed ~15s.
PROPAGATION_SLEEP = int(os.environ.get("TG_LIVE_DENY_SLEEP", "20"))


@pytest.fixture
def _boto():
    boto3 = pytest.importorskip("boto3")
    return boto3


def _assert_stage(boto3) -> str:
    """Prod-guard (rail 3): refuse to run unless the caller is a
    known non-prod (dev/stage) account. Returns the account id."""
    acct = boto3.client("sts").get_caller_identity()["Account"]
    allowed = {
        a for a in (
            os.environ.get("TG_TARGET_ACCOUNT_ID", ""),
            "123456789012",  # stage
            "123456789012",  # dev
        ) if a
    }
    if acct not in allowed:
        pytest.skip(
            f"refusing to run live deny test against account {acct} "
            f"(not a known dev/stage account {allowed}) — prod-guard.")
    return acct


def _assume(boto3, account_id: str):
    """Assume the consumer role with the RESERVED UAT session name."""
    creds = boto3.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{CONSUMER_ROLE}",
        RoleSessionName=UAT_SESSION,
        DurationSeconds=900,
    )["Credentials"]
    return boto3.client(
        "bedrock-runtime", region_name=REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _converse(brt) -> str:
    """Invoke the model via Converse. Returns 'OK' on success;
    raises the botocore ClientError on AccessDenied."""
    brt.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user",
                   "content": [{"text": "say hi briefly"}]}],
        inferenceConfig={"maxTokens": 20},
    )
    return "OK"


def _policy_arn(iam, account_id: str) -> str:
    return f"arn:aws:iam::{account_id}:policy/{DENY_POLICY}"


def _default_version_doc(iam, arn: str) -> tuple[str, dict]:
    vid = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
    doc = iam.get_policy_version(
        PolicyArn=arn, VersionId=vid)["PolicyVersion"]["Document"]
    return vid, doc


def _prune_oldest_nondefault(iam, arn: str) -> None:
    """IAM caps managed policies at 5 versions (rail 4) — drop the
    oldest non-default before creating a new one."""
    versions = iam.list_policy_versions(PolicyArn=arn)["Versions"]
    nondefault = sorted(
        (v for v in versions if not v["IsDefaultVersion"]),
        key=lambda v: v["CreateDate"])
    while len(versions) >= 5 and nondefault:
        victim = nondefault.pop(0)
        iam.delete_policy_version(
            PolicyArn=arn, VersionId=victim["VersionId"])
        versions = iam.list_policy_versions(PolicyArn=arn)["Versions"]


def _set_doc(iam, arn: str, doc: dict) -> None:
    _prune_oldest_nondefault(iam, arn)
    iam.create_policy_version(
        PolicyArn=arn, PolicyDocument=json.dumps(doc),
        SetAsDefault=True)


def _with_uat_deny(doc: dict) -> dict:
    """Return a copy of the policy doc with a QuotaDeny statement
    that denies the reserved UAT session — exactly the shape the
    reconciler writes (StringLike aws:userid *:<email>)."""
    doc = json.loads(json.dumps(doc))  # deep copy
    doc.setdefault("Statement", [])
    # Drop any prior test statement so re-runs are idempotent.
    doc["Statement"] = [
        s for s in doc["Statement"]
        if s.get("Sid") != "QuotaDenyUatLiveTest"
    ]
    doc["Statement"].append({
        "Sid": "QuotaDenyUatLiveTest",
        "Effect": "Deny",
        "Action": [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
            "bedrock:Converse",
            "bedrock:ConverseStream",
        ],
        "Resource": "*",
        "Condition": {"StringLike": {"aws:userid": [f"*:{UAT_SESSION}"]}},
    })
    return doc


def test_deny_blocks_live_invoke(_boto):
    boto3 = _boto
    account_id = _assert_stage(boto3)
    iam = boto3.client("iam")
    arn = _policy_arn(iam, account_id)
    brt = _assume(boto3, account_id)

    # Snapshot the baseline policy doc BEFORE any mutation (rail 2).
    _base_vid, baseline_doc = _default_version_doc(iam, arn)

    try:
        # 1. BASELINE — the session can invoke.
        assert _converse(brt) == "OK"

        # 2. APPLY the per-person deny (what the reconciler writes
        #    when spend > cap). tg-consumer already carries
        #    tg-BedrockQuotaDeny, so this condition-list edit IS the
        #    enforcement — no per-user AttachRolePolicy needed.
        _set_doc(iam, arn, _with_uat_deny(baseline_doc))
        time.sleep(PROPAGATION_SLEEP)  # IAM eventual consistency

        # 3. ENFORCE — the SAME session is now denied, explicitly by
        #    tg-BedrockQuotaDeny.
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError) as exc:
            _converse(brt)
        msg = str(exc.value)
        assert "AccessDenied" in msg or "explicit deny" in msg, msg
        assert DENY_POLICY in msg, (
            f"expected the deny to cite {DENY_POLICY}: {msg}")
    finally:
        # 4. REVERT unconditionally — restore the exact baseline doc
        #    (removes ONLY the test session; real users untouched).
        _set_doc(iam, arn, baseline_doc)

    # 5. Post-revert: the session works again (propagation wait).
    time.sleep(PROPAGATION_SLEEP)
    assert _converse(brt) == "OK"
