"""
Tests for the org-level default quota (#269).

Acceptance:
  - admin_config['org_default_quota_usd'] backs an org-wide
    default cap; built-in fallback is $1000/month.
  - GET/PUT /api/admin/config returns/persists it.
  - Quota reconciler falls back to it when a user has no
    explicit cap_usd / no QuotaPolicy override.
  - GET /api/users + GET /api/users/<id> include
    effective_quota_usd (explicit cap if set, else org
    default).
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest


# ────────────────────────────────────────────────
# org_config helpers
# ────────────────────────────────────────────────

def test_org_default_falls_back_to_built_in(clean_db):
    """No admin_config row, no legacy QuotaPolicy DEFAULT row →
    helper returns the built-in 1000.00 floor."""
    from db.session import get_db
    from db.org_config import (
        get_org_default_quota_usd,
        ORG_DEFAULT_QUOTA_USD,
    )
    assert ORG_DEFAULT_QUOTA_USD == 1000.00
    with get_db() as db:
        assert get_org_default_quota_usd(db) == 1000.00


def test_org_default_set_then_get(clean_db):
    from db.session import get_db
    from db.org_config import (
        get_org_default_quota_usd,
        set_org_default_quota_usd,
    )
    with get_db() as db:
        set_org_default_quota_usd(db, 250.0)
    with get_db() as db:
        assert get_org_default_quota_usd(db) == 250.0


def test_org_default_legacy_policy_used_when_no_admin_config(
    clean_db,
):
    """Existing installs may already have a QuotaPolicy
    scope='DEFAULT' row from the legacy /api/policy/default
    surface. With no admin_config row, the helper falls back
    to that legacy row instead of the hard 1000 floor — so
    upgraded installs don't silently raise their cap."""
    from db.session import get_db
    from db.models import QuotaPolicy
    from db.org_config import get_org_default_quota_usd
    with get_db() as db:
        db.add(QuotaPolicy(scope="DEFAULT", monthly_cap_usd=42.0))
    with get_db() as db:
        assert get_org_default_quota_usd(db) == 42.0


def test_org_default_admin_config_wins_over_legacy(clean_db):
    """If both rows exist, admin_config is the source of
    truth — the legacy QuotaPolicy DEFAULT is only a backstop."""
    from db.session import get_db
    from db.models import QuotaPolicy
    from db.org_config import (
        get_org_default_quota_usd,
        set_org_default_quota_usd,
    )
    with get_db() as db:
        db.add(QuotaPolicy(scope="DEFAULT", monthly_cap_usd=42.0))
        set_org_default_quota_usd(db, 750.0)
    with get_db() as db:
        assert get_org_default_quota_usd(db) == 750.0


# ────────────────────────────────────────────────
# deny_reconciler fallback
# ────────────────────────────────────────────────

@pytest.fixture
def fake_iam(monkeypatch):
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


def _seed_user(email, cap):
    # #836: cap enforcement is gated on `governed` — tg only denies a
    # principal it governs. These tests assert the org-default cap
    # path, so the seeded users must be governed (else, correctly, no
    # deny).
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(
            email=email, status="active", cap_usd=cap,
            governed=True))


def _seed_metric(email, spend):
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import CurUserSpend
    usage_hour = datetime.now(timezone.utc).date()  # #643
    with get_db() as db:
        db.add(CurUserSpend(
            email=email, usage_hour=usage_hour, model_id="m1",
            input_tokens=0, output_tokens=0,
            total_tokens=0,
            spend_usd=spend,
        ))


def test_unpolicied_user_uses_org_default_cap(
    clean_db, fake_iam,
):
    """User with cap_usd=None and no explicit QuotaPolicy
    DEFAULT row falls back to the admin_config org default.
    Spend above org default → deny."""
    from db.session import get_db
    from db.org_config import set_org_default_quota_usd
    with get_db() as db:
        set_org_default_quota_usd(db, 100.0)
    _seed_user("nopolicy@test.com", cap=None)
    _seed_metric("nopolicy@test.com", spend=150.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert out["blocked"] == ["nopolicy@test.com"], out


def test_unpolicied_user_under_org_default_not_denied(
    clean_db, fake_iam,
):
    from db.session import get_db
    from db.org_config import set_org_default_quota_usd
    with get_db() as db:
        set_org_default_quota_usd(db, 100.0)
    _seed_user("ok@test.com", cap=None)
    _seed_metric("ok@test.com", spend=50.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert "0 denied" in out["detail"]


def test_explicit_cap_overrides_org_default(
    clean_db, fake_iam,
):
    """A user with their own cap_usd is unaffected by the
    org default — both directions."""
    from db.session import get_db
    from db.org_config import set_org_default_quota_usd
    with get_db() as db:
        set_org_default_quota_usd(db, 100.0)
    # Higher per-user cap → spend above org default but
    # under personal cap → not denied.
    _seed_user("rich@test.com", cap=500.0)
    _seed_metric("rich@test.com", spend=200.0)
    # Lower per-user cap → spend below org default but
    # over personal cap → denied.
    _seed_user("tight@test.com", cap=10.0)
    _seed_metric("tight@test.com", spend=20.0)

    import worker.jobs.deny_reconciler as dr
    out = dr.run()
    assert out["blocked"] == ["tight@test.com"], out
