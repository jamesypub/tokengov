"""e2e-scoped fixtures: self-seed the public fixture into a clean DB
and provide an authenticated API client per persona.

Reuses the shared conftest (pg_url / clean_db) one directory up. The
e2e tier exercises whole workflows across layers (HTTP-shaped requests
via TestClient → real DB → read APIs); it does NOT touch AWS and MUST
NOT run against prod — the test-trust bypass here is only safe on a
test stack, guarded below.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def e2e_env(monkeypatch):
    """Enable the test-trust auth bypass for the API tier + assert we
    are NOT pointed at a prod stack (mirror the test-trust-never-in-prod
    invariant rail). API_BASE is unused by the in-process TestClient
    tier, but if a future live-HTTP tier reads it, refuse a prod host.

    This unconditional prod-guard is scoped to the in-process `e2e`
    tier ONLY — it stays ON, always. The real-stack `live` tier
    (tests/e2e/live/) does not use this fixture; it carries its own
    prod-guard that is an explicit opt-in keyed off E2E_API_BASE /
    E2E_ALLOW_PROD, so the two tiers never share a guard and this
    fixture's behavior is unchanged."""
    api_base = os.environ.get("API_BASE", "")
    assert "prod" not in api_base.lower(), (
        "e2e uses the test-trust bypass — refusing to run against a "
        f"prod-looking API_BASE ({api_base!r})")
    monkeypatch.setenv("TG_AUTH_TEST_TRUST", "1")
    yield


@pytest.fixture
def seeded(e2e_env, clean_db):
    """A clean DB loaded with the public e2e seed. Depends on e2e_env
    so test-trust is on before the app starts."""
    from tests.e2e.seed import seed_db
    seed_db()
    yield


@pytest.fixture
def api(seeded):
    """An authenticated API client (TestClient) factory: api(email)
    returns a client that sends test-trust headers for that persona.
    Default persona is the seed's org admin."""
    from fastapi.testclient import TestClient
    from api.main import app
    from tests.e2e.api.client import PersonaClient

    with TestClient(app) as raw:
        def _as(email: str = "org-admin@example.com") -> PersonaClient:
            return PersonaClient(raw, email)
        yield _as


@pytest.fixture
def no_aws(monkeypatch):
    """Stub boto3.client so endpoints that make an IAM call (e.g.
    Manage/Unmanage's AttachRolePolicy) succeed AWS-free — the e2e tier
    asserts the app-contract round-trip, not the real attach (the deep
    reconciler+IAM path is unit-covered elsewhere). A no-op client whose
    every method returns {} is enough for the flag round-trip."""
    class _NoopAws:
        def __getattr__(self, _name):
            def _call(*a, **k):
                return {}
            return _call

    noop = _NoopAws()
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: noop)
    # Manage/Unmanage build the IAM client via api.routes.users._iam_client
    # (an aws_session wrapper, not boto3.client directly); stub it too so
    # the attach is a no-op. _deny_policy_arn is tolerant of the noop.
    import api.routes.users as users_mod
    monkeypatch.setattr(users_mod, "_iam_client", lambda: noop, raising=False)
    monkeypatch.setattr(
        users_mod, "_deny_policy_arn",
        lambda iam: "arn:aws:iam::123456789012:policy/tg-BedrockQuotaDeny",
        raising=False)
    yield
