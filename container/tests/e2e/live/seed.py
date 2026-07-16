"""Idempotent seed of a governable test principal into the LIVE stack.

Mirrors the style of tests/e2e/seed.py, but this seed talks to the
running app's onboard/preregister endpoints over HTTP (not the
in-process DB) — because the live tier drives the DEPLOYED app, not a
TestClient. It seeds ONE reserved @example.com principal the workflow +
enforcement cases can govern, and binds it to the pre-provisioned test
principal ARN (D-1: the ARN is an INPUT, E2E_TEST_PRINCIPAL_ARN — the
seed never writes IAM to create the principal).

Idempotent: preregister returns 409 if the user already exists, which
this treats as success so re-runs are a no-op.
"""
from __future__ import annotations


def seed_principal(client, email: str, principal_arn: str | None = None,
                   team_id: str | None = None) -> dict:
    """Ensure `email` exists as a governable user on the live stack.

    Uses the real onboard endpoint (/api/users/preregister) so the row
    goes through the same path an admin's "Add user" does. A 409 (user
    already exists) is treated as success — the seed is idempotent.
    Returns the user's detail row read back from the API.

    principal_arn (E2E_TEST_PRINCIPAL_ARN) is the pre-provisioned test
    principal the enforcement tests assume; it is bound to the row via
    the reconcile/discovery path, not created here.
    """
    body = {"email": email}
    if team_id is not None:
        body["team_id"] = team_id
    r = client.post("/api/users/preregister", json_body=body)
    # 200/201 = created; 409 = already there (idempotent re-run).
    if r.status not in (200, 201, 409):
        raise AssertionError(
            f"seed preregister({email}) failed {r.status}: {r.text[:300]}")
    # Read back the row so callers get the current governed/principal
    # state (the LIST/detail is the source of truth, as in the in-process
    # seed's one-user-one-team contract).
    detail = client.get(f"/api/users/{email}")
    if detail.status != 200:
        raise AssertionError(
            f"seed read-back({email}) failed {detail.status}: "
            f"{detail.text[:300]}")
    return detail.json()


def unseed_principal(client, email: str) -> None:
    """Best-effort teardown of the seeded principal. A missing user
    (404) is fine — teardown is idempotent and never fails a test."""
    r = client.delete(f"/api/users/{email}")
    if r.status not in (200, 204, 404):
        # Don't raise: teardown must not mask the test's own result.
        pass
