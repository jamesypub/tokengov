"""Credential-free CONTRACT guard for the live-tier's API assumptions.

The `live` tier (tests/e2e/live/, -m live) drives a real stack + real
AWS, so it only runs at operator time — a rename/regression in the
endpoints or response fields it depends on would otherwise slip through
the credential-free CI gate and only surface when someone runs
`-m live` against a real target. This module pins those assumptions in
the fast `no_aws` tier (in-process TestClient, boto3 stubbed), so a
breaking change is caught on every commit.

It asserts the endpoints EXIST and the fields the live tier reads are
SHAPE-present — NOT the enforcement behavior (that is what the live
tier proves against real AWS). This is the contract half of the
smoke-the-real-artifact split: shape here, real bite there.

The live-tier call sites this guards (tests/e2e/live/*):
  - GET/PUT /api/settings/blocked-models  (blocked_models list)
  - POST /api/jobs/run  (+ optional {"job": <name>})
  - GET /api/users/<email>  → governed / role_type fields
  - POST /api/users/<email>/manage  → apply{state} (pending path)
"""
from __future__ import annotations

import pytest

from tests.e2e.api import workflows as wf

pytestmark = pytest.mark.e2e


def test_blocked_models_endpoint_shape(api, no_aws):
    """GET/PUT /api/settings/blocked-models exist and round-trip a
    `blocked_models` list. This is the path the live tier's
    blocked-model workflow drives — guarding the exact route
    (/api/settings/blocked-models, NOT /api/blocked-models) so a
    live-tier path typo can't hide until an operator run."""
    c = api()  # org admin
    r = c.get("/api/settings/blocked-models")
    assert r.status_code == 200, r.text
    assert "blocked_models" in r.json()
    assert isinstance(r.json()["blocked_models"], list)

    # PUT accepts + echoes the list (the live tier adds/restores here).
    r = c.put(
        "/api/settings/blocked-models",
        json={"blocked_models": []})
    assert r.status_code == 200, r.text
    assert "blocked_models" in r.json()


def test_jobs_run_endpoint_exists(api, no_aws):
    """POST /api/jobs/run exists (the live tier runs the real
    reconcile pipeline through it) and accepts the optional
    {"job": <name>} single-job body the drift-sweep case uses."""
    c = api()
    # Bare trigger (cur_spend_sync → deny_reconciler pipeline).
    r = c.post("/api/jobs/run")
    assert r.status_code == 200, r.text
    # Named single job — the live drift-sweep case passes this.
    r = c.post(
        "/api/jobs/run",
        json={"job": "governance_drift_check"})
    assert r.status_code == 200, r.text


def test_user_detail_exposes_governed_and_role_type(api, no_aws):
    """GET /api/users/<email> returns the `governed` and `role_type`
    fields the live tier reads (govern round-trip + the IDC
    honest-pending rail keys off role_type)."""
    email = "member-1@example.com"  # seeded, governable
    detail = wf.user(api(), email)
    assert "governed" in detail
    assert isinstance(detail["governed"], bool)
    # role_type distinguishes idc (AWSReservedSSO_*) from iam — the
    # live tier's IDC honest-pending case skips unless role_type==idc.
    assert "role_type" in detail


def test_manage_returns_apply_state(api, no_aws):
    """POST /api/users/<email>/manage returns an `apply` object with a
    `state` — the live tier's IDC rail asserts a 'pending' state (tg
    can't attach to an AWSReservedSSO_* role). no_aws stubs boto3 so
    the attach is a no-op; we assert the CONTRACT (apply.state present),
    not the real enforcement."""
    email = "member-1@example.com"
    r = api().post(f"/api/users/{email}/manage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "apply" in body, body
    assert "state" in body["apply"], body["apply"]
    # Revert so the case is idempotent against the shared seed.
    api().post(f"/api/users/{email}/unmanage")
