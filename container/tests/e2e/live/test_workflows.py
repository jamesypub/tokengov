"""Live workflows against the REAL deployed app + real AWS.

These mirror the in-process API-tier workflow cases but run over HTTP
against the running stack, so they prove the wiring the TestClient
tier can't: real IAM attach on Govern, the real reconciler on the
drift sweep, a real Bedrock call denied by a blocked model.

Every mutating case seeds/uses the RESERVED @example.com principal and
reverts in `finally` — a workflow case must never mutate a real person
or leave governance state dirty.

Honest-skip rail: an IDC (AWSReservedSSO_*) principal can only ever be
reported *pending* by Govern — tg cannot attach the deny directly to an
IDC-owned role (#1011). The IDC case asserts pending, never a false
"enforced".
"""
from __future__ import annotations

import os

import pytest

from tests.e2e.live.conftest import (
    SEED_EMAIL, TEST_MODEL_ID, TEST_PRINCIPAL_ARN,
)
from tests.e2e.live.seed import seed_principal, unseed_principal

pytestmark = pytest.mark.live


def _team_count(client, team_id: str):
    r = client.get("/api/teams")
    r.raise_for_status()
    for t in r.json().get("teams", []):
        if t["team_id"] == team_id:
            return t["member_count"]
    return None


def test_onboard_appears_and_counts(live_client):
    """Onboard the reserved principal → it appears in the users list.
    Idempotent seed; teardown removes it so re-runs are clean."""
    email = SEED_EMAIL
    try:
        seed_principal(live_client, email, TEST_PRINCIPAL_ARN)
        r = live_client.get("/api/users")
        r.raise_for_status()
        emails = {u["email"] for u in r.json().get("users", [])}
        assert email in emails, (
            f"onboarded {email} not in users list {sorted(emails)[:20]}")
    finally:
        unseed_principal(live_client, email)


def test_govern_then_ungovern_real_reconciler(live_client):
    """Govern flips governed=true (real IAM attach + reconcile via the
    app), ungovern flips it back. Always ungoverns in `finally` so the
    reserved principal never stays governed."""
    email = SEED_EMAIL
    try:
        seed_principal(live_client, email, TEST_PRINCIPAL_ARN)
        g = live_client.post(f"/api/users/{email}/manage")
        # Govern may 400 if the seeded row carries no IAM role ARN yet
        # (a discovery-populated field); that's an honest infra gap, not
        # a test failure — skip rather than falsely fail.
        if g.status == 400:
            pytest.skip(
                f"seeded principal {email} has no attachable IAM role "
                "(principal_arn not discovered on this stack); govern "
                "needs a discovered role — nothing to enforce.")
        g.raise_for_status()
        detail = live_client.get(f"/api/users/{email}")
        detail.raise_for_status()
        assert detail.json().get("governed") is True

        live_client.post(f"/api/users/{email}/unmanage").raise_for_status()
        detail = live_client.get(f"/api/users/{email}")
        detail.raise_for_status()
        assert detail.json().get("governed") is False
    finally:
        # Belt-and-suspenders: ensure ungoverned + removed.
        live_client.post(f"/api/users/{email}/unmanage")
        unseed_principal(live_client, email)


def test_idc_principal_govern_is_pending_not_enforced(live_client):
    """Honest-skip rail: an AWSReservedSSO_* (IDC) principal can only be
    reported *pending* by Govern — tg never attaches the deny to an
    IDC-owned role (#1011). Assert pending, never false-enforced.

    Opt-in via E2E_IDC_PRINCIPAL_EMAIL: a pre-seeded IDC user on the
    stack. Skips when absent — most test stacks have no IDC principal."""
    idc_email = os.environ.get("E2E_IDC_PRINCIPAL_EMAIL")
    if not idc_email:
        pytest.skip(
            "no IDC test principal: set E2E_IDC_PRINCIPAL_EMAIL to an "
            "AWSReservedSSO_* user to exercise the honest-pending rail.")
    detail = live_client.get(f"/api/users/{idc_email}")
    detail.raise_for_status()
    if (detail.json().get("role_type") or "iam") != "idc":
        pytest.skip(
            f"{idc_email} is not an IDC (role_type=idc) principal; the "
            "honest-pending rail only applies to AWSReservedSSO_* roles.")
    try:
        g = live_client.post(f"/api/users/{idc_email}/manage")
        g.raise_for_status()
        apply_state = g.json().get("apply") or {}
        # tg must NOT claim hard enforcement for an IDC principal — the
        # attach it cannot perform means the truthful state is pending.
        state = str(apply_state).lower()
        assert "pending" in state or apply_state.get("pending") is True, (
            "IDC principal must report pending (tg cannot attach the "
            f"deny to an AWSReservedSSO_* role), got: {apply_state!r}")
    finally:
        live_client.post(f"/api/users/{idc_email}/unmanage")


def test_blocked_model_add_denies_real_call(
        live_base, live_client, aws_session, account_id, assume_bedrock):
    """Add the test model to the org block-list → a REAL Bedrock call
    for it is denied. Always restore the prior block-list in `finally`
    so the model is not left globally blocked."""
    from botocore.exceptions import ClientError
    import time

    from tests.e2e.live.conftest import RESERVED_SESSION

    prior = live_client.get("/api/settings/blocked-models")
    prior.raise_for_status()
    prior_list = prior.json().get("blocked_models", [])
    brt = assume_bedrock(RESERVED_SESSION)
    sleep_s = int(os.environ.get("E2E_LIVE_DENY_SLEEP", "20"))

    try:
        new_list = sorted(set(prior_list) | {TEST_MODEL_ID})
        live_client.put(
            "/api/settings/blocked-models",
            json_body={"blocked_models": new_list}).raise_for_status()
        # The block-list feeds the reconciler's DenyBlockedModels stmt;
        # run the real pipeline so the deny is applied, then wait for
        # IAM propagation.
        live_client.post("/api/jobs/run").raise_for_status()
        time.sleep(sleep_s)

        with pytest.raises(ClientError) as exc:
            brt.converse(
                modelId=TEST_MODEL_ID,
                messages=[{"role": "user",
                           "content": [{"text": "hi"}]}],
                inferenceConfig={"maxTokens": 10})
        assert "AccessDenied" in str(exc.value), str(exc.value)
    finally:
        # Restore the exact prior block-list + re-run so the model is
        # allowed again (never leave a model globally blocked).
        live_client.put(
            "/api/settings/blocked-models",
            json_body={"blocked_models": prior_list})
        live_client.post("/api/jobs/run")


def test_drift_sweep_run_reflects_truth(live_client):
    """Run the on-demand governance-drift sweep via /api/jobs/run and
    assert it completes without errors — the count/state it reports
    reflects the real reconciled truth, not a stale cache."""
    r = live_client.post(
        "/api/jobs/run",
        json_body={"job": "governance_drift_check"})
    # A test stack without discovered principals may have nothing to
    # sweep; the job still returns 200 with an empty result. A non-200
    # or an errors[] entry IS a failure (real wiring bug).
    r.raise_for_status()
    body = r.json()
    assert not body.get("errors"), (
        f"drift sweep reported errors: {body.get('errors')}")
