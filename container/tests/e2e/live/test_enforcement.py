"""CROWN-JEWEL live enforcement: prove the deny-only loop actually
blocks Bedrock end-to-end against a REAL assumed-role session, driven
through the deployed app where possible.

The flow:
  1. Assume the consumer role as the RESERVED test session → baseline
     Converse/InvokeModel succeeds.
  2. FORCE-BLOCK that session's aws:userid on the live
     tg-BedrockQuotaDeny policy — exactly the statement the reconciler
     writes when spend > cap.
  3. Assert the SAME session now gets AccessDenied citing the deny
     policy.
  4. UNBLOCK (restore the exact baseline doc) and assert success
     returns.

Rails (non-negotiable):
  - RESERVED session only (E2E_RESERVED_SESSION) — never a real user.
  - ALWAYS revert in `finally` — a failed assertion must never leave a
    principal blocked (snapshot the baseline version, restore it
    unconditionally).
  - Prod-guard: the aws_session / live_base fixtures refuse a
    prod-looking API_BASE or unknown account.
  - IAM 5-version ceiling: prune the oldest non-default version before
    each create-policy-version.

The force-block primitive uses direct policy mutation (the reconciler's
exact statement shape) so the test is self-contained and reversible;
the workflow-level "govern via the app + real reconciler" path is
covered in test_workflows.py. The over-cap spend variant (drive block
purely through billed spend) is behind E2E_LIVE_OVERCAP=1, off by
default — it depends on CUR-lagged spend data and is operator-run.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from tests.e2e.live.conftest import (
    DENY_POLICY, RESERVED_SESSION, TEST_MODEL_ID,
)

pytestmark = pytest.mark.live

# IAM policy-version propagation is eventually consistent; the manual
# repro (test_deny_enforcement_live.py) needed ~15-20s.
PROPAGATION_SLEEP = int(os.environ.get("E2E_LIVE_DENY_SLEEP", "20"))
_TEST_SID = "QuotaDenyE2eLiveTest"


def _converse(brt) -> str:
    """Invoke via Converse. Returns 'OK'; raises ClientError on deny."""
    brt.converse(
        modelId=TEST_MODEL_ID,
        messages=[{"role": "user",
                   "content": [{"text": "say hi briefly"}]}],
        inferenceConfig={"maxTokens": 20},
    )
    return "OK"


def _policy_arn(account_id: str) -> str:
    return os.environ.get(
        "E2E_DENY_POLICY_ARN",
        f"arn:aws:iam::{account_id}:policy/{DENY_POLICY}")


def _default_version_doc(iam, arn: str):
    vid = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
    doc = iam.get_policy_version(
        PolicyArn=arn, VersionId=vid)["PolicyVersion"]["Document"]
    return vid, doc


def _prune_oldest_nondefault(iam, arn: str) -> None:
    """IAM caps managed policies at 5 versions — drop the oldest
    non-default before creating a new one."""
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
        PolicyArn=arn, PolicyDocument=json.dumps(doc), SetAsDefault=True)


def _with_reserved_deny(doc: dict) -> dict:
    """A copy of the policy doc with a QuotaDeny statement denying the
    RESERVED session — the reconciler's shape (StringLike aws:userid
    *:<session>). Re-runs are idempotent (drops any prior test stmt)."""
    doc = json.loads(json.dumps(doc))  # deep copy
    doc.setdefault("Statement", [])
    doc["Statement"] = [
        s for s in doc["Statement"] if s.get("Sid") != _TEST_SID]
    doc["Statement"].append({
        "Sid": _TEST_SID,
        "Effect": "Deny",
        "Action": [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
            "bedrock:Converse",
            "bedrock:ConverseStream",
        ],
        "Resource": "*",
        "Condition": {
            "StringLike": {"aws:userid": [f"*:{RESERVED_SESSION}"]}},
    })
    return doc


def test_force_block_denies_then_unblock_restores(
        live_base, aws_session, account_id, assume_bedrock):
    """Baseline OK → force-block → AccessDenied → unblock → OK again.
    The whole thing reverts in `finally` so a failure never strands the
    reserved session blocked."""
    from botocore.exceptions import ClientError

    iam = aws_session.client("iam")
    arn = _policy_arn(account_id)
    brt = assume_bedrock(RESERVED_SESSION)

    # Snapshot baseline BEFORE any mutation (revert rail).
    _base_vid, baseline_doc = _default_version_doc(iam, arn)

    try:
        # 1. BASELINE — the reserved session can invoke.
        assert _converse(brt) == "OK"

        # 2. FORCE-BLOCK — write the per-person deny (what the
        #    reconciler writes when spend > cap). tg-consumer already
        #    carries tg-BedrockQuotaDeny, so this condition edit IS the
        #    enforcement — no per-user AttachRolePolicy needed.
        _set_doc(iam, arn, _with_reserved_deny(baseline_doc))
        time.sleep(PROPAGATION_SLEEP)  # IAM eventual consistency

        # 3. ENFORCE — the SAME session is now denied by the policy.
        with pytest.raises(ClientError) as exc:
            _converse(brt)
        msg = str(exc.value)
        assert "AccessDenied" in msg or "explicit deny" in msg, msg
        assert DENY_POLICY in msg, (
            f"expected the deny to cite {DENY_POLICY}: {msg}")
    finally:
        # 4. UNBLOCK unconditionally — restore the exact baseline doc
        #    (removes ONLY the test session; real users untouched).
        _set_doc(iam, arn, baseline_doc)

    # 5. Post-unblock: the session works again (propagation wait).
    time.sleep(PROPAGATION_SLEEP)
    assert _converse(brt) == "OK"


@pytest.mark.skipif(
    os.environ.get("E2E_LIVE_OVERCAP") != "1",
    reason="over-cap spend variant is opt-in (E2E_LIVE_OVERCAP=1): it "
           "depends on CUR-lagged billed spend crossing the cap and is "
           "operator-run, not part of the automated crown-jewel loop.")
def test_overcap_spend_blocks_via_reconciler(
        live_base, live_client, aws_session, account_id, assume_bedrock):
    """Drive the block purely through billed over-cap spend + the REAL
    reconciler (no direct policy edit). Off by default because CUR spend
    lands ≤24h lagged; the operator runs this after spend has crossed
    the cap for the reserved principal. Always reverts the cap in
    `finally`."""
    from botocore.exceptions import ClientError
    from tests.e2e.live.conftest import SEED_EMAIL

    brt = assume_bedrock(RESERVED_SESSION)
    # Set a $0 cap so any recorded spend is over-cap, then run the real
    # pipeline (cur_spend_sync → deny_reconciler) via /api/jobs/run.
    detail = live_client.get(f"/api/users/{SEED_EMAIL}")
    detail.raise_for_status()
    prior_cap = detail.json().get("cap_usd")

    try:
        live_client.put(
            f"/api/users/{SEED_EMAIL}",
            json_body={"cap_usd": 0}).raise_for_status()
        live_client.post("/api/users/{0}/manage".format(SEED_EMAIL))
        live_client.post("/api/jobs/run").raise_for_status()
        time.sleep(PROPAGATION_SLEEP)
        with pytest.raises(ClientError) as exc:
            _converse(brt)
        assert "AccessDenied" in str(exc.value)
    finally:
        # Restore the prior cap + re-run the reconciler so the reserved
        # principal is unblocked (never leave it capped at $0).
        live_client.put(
            f"/api/users/{SEED_EMAIL}",
            json_body={"cap_usd": prior_cap})
        live_client.post("/api/jobs/run")
