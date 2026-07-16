"""
Tests for #346 service_account_monitor — covers period
sums, threshold dedup, block/unblock cycle, the
tag-enforcement gate, and the IAM call shape.
"""
from __future__ import annotations
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest


def _seed_cap(**kw):
    from db.session import get_db
    from db.models import ServiceAccountCap
    defaults = {
        "identity_key": "role:MyBatchJobRole",
        "budget_usd": 10.0,
        "period": "month",
        "mode": "alert_and_block",
        "alert_threshold_pct": 80,
        "owner_emails": "owner@test.com",
        "grace_pct": 0,
        "auto_unblock": True,
        "created_by": "admin@test.com",
    }
    defaults.update(kw)
    with get_db() as db:
        db.add(ServiceAccountCap(**defaults))


def _seed_metric_for(identity_key, spend, model_id="m1"):
    # #643: per-day grain. Seed today's row — the budget windows
    # under test (day/week/month) all include today.
    from db.session import get_db
    from db.models import CurUserSpend
    usage_hour = datetime.now(timezone.utc).date()
    with get_db() as db:
        existing = (
            db.query(CurUserSpend)
            .filter(
                CurUserSpend.email == identity_key,
                CurUserSpend.usage_hour == usage_hour,
                CurUserSpend.model_id == model_id,
            )
            .first()
        )
        if existing:
            existing.spend_usd = spend
        else:
            db.add(CurUserSpend(
                email=identity_key,
                usage_hour=usage_hour,
                model_id=model_id,
                input_tokens=0, output_tokens=0,
                total_tokens=0,
                spend_usd=spend,
            ))


@pytest.fixture
def fake_iam(monkeypatch):
    """Stub boto3.client('iam'); test populates
    list_role_tags / put_role_policy / delete_role_policy
    behavior."""
    iam = MagicMock()
    iam.list_role_tags.return_value = {"Tags": [
        {"Key": "tg-budget-enforced", "Value": "true"},
    ]}
    import worker.jobs.service_account_monitor as sam
    monkeypatch.setattr(
        sam.boto3, "client", lambda *a, **kw: iam,
    )
    return iam


def test_threshold_alert_fires_once_per_period(
    clean_db, fake_iam,
):
    """At 80% of budget, monitor enqueues a threshold
    alert. Re-running in the same period must not
    duplicate (the dedup key is identity_key + kind +
    period_key)."""
    _seed_cap(budget_usd=10.0, alert_threshold_pct=80)
    _seed_metric_for("role:MyBatchJobRole", spend=8.5)

    import worker.jobs.service_account_monitor as sam
    sam.run()
    sam.run()

    from db.session import get_db
    from db.models import ServiceAccountAlert
    with get_db() as db:
        alerts = (
            db.query(ServiceAccountAlert)
            .filter(
                ServiceAccountAlert.identity_key
                == "role:MyBatchJobRole",
                ServiceAccountAlert.kind == "threshold",
            )
            .all()
        )
        assert len(alerts) == 1
        assert alerts[0].pct_of_budget >= 80


def test_budget_exhausted_writes_inline_deny(
    clean_db, fake_iam,
):
    """When spend ≥ 100% (no grace), monitor enqueues a
    budget_exhausted alert AND writes the inline deny via
    iam:PutRolePolicy on the tagged role."""
    _seed_cap(
        budget_usd=10.0, mode="alert_and_block", grace_pct=0,
    )
    _seed_metric_for("role:MyBatchJobRole", spend=12.0)

    import worker.jobs.service_account_monitor as sam
    sam.run()

    fake_iam.put_role_policy.assert_called_once()
    call = fake_iam.put_role_policy.call_args
    assert call.kwargs["RoleName"] == "MyBatchJobRole"
    assert (
        call.kwargs["PolicyName"]
        == "tg-service-budget-deny"
    )
    assert "Deny" in call.kwargs["PolicyDocument"]

    from db.session import get_db
    from db.models import (
        ServiceAccountAlert, ServiceAccountCap,
    )
    with get_db() as db:
        cap = (
            db.query(ServiceAccountCap)
            .filter(
                ServiceAccountCap.identity_key
                == "role:MyBatchJobRole"
            )
            .first()
        )
        assert cap.blocked_at is not None
        kinds = [
            a.kind for a in db.query(ServiceAccountAlert).all()
        ]
        assert "budget_exhausted" in kinds


def test_alert_only_mode_never_writes_deny(
    clean_db, fake_iam,
):
    """mode='alert_only' should fire alerts but never call
    PutRolePolicy. Customer's choice for tracking-without-
    enforcement."""
    _seed_cap(budget_usd=10.0, mode="alert_only")
    _seed_metric_for("role:MyBatchJobRole", spend=12.0)

    import worker.jobs.service_account_monitor as sam
    sam.run()

    fake_iam.put_role_policy.assert_not_called()


def test_disabled_mode_is_a_noop(clean_db, fake_iam):
    """mode='disabled' records intent-not-to-enforce and
    is filtered out at query time."""
    _seed_cap(budget_usd=10.0, mode="disabled")
    _seed_metric_for("role:MyBatchJobRole", spend=12.0)

    import worker.jobs.service_account_monitor as sam
    sam.run()

    from db.session import get_db
    from db.models import ServiceAccountAlert
    with get_db() as db:
        assert (
            db.query(ServiceAccountAlert).count() == 0
        )
    fake_iam.put_role_policy.assert_not_called()


def test_tag_missing_skips_iam_write(
    clean_db, fake_iam,
):
    """A role without `tg-budget-enforced=true` must NOT
    receive a PutRolePolicy call. Mirrors the customer
    opt-in pattern from #344."""
    fake_iam.list_role_tags.return_value = {"Tags": []}
    _seed_cap(budget_usd=10.0, mode="alert_and_block")
    _seed_metric_for("role:MyBatchJobRole", spend=12.0)

    import worker.jobs.service_account_monitor as sam
    sam.run()

    fake_iam.put_role_policy.assert_not_called()


def test_unblock_when_spend_drops_below_cap(
    clean_db, fake_iam,
):
    """If spend dips below the budget (e.g. month rolled
    over or admin lowered the rates), monitor calls
    DeleteRolePolicy and emits an unblocked alert."""
    from db.session import get_db
    from db.models import ServiceAccountCap

    _seed_cap(budget_usd=10.0, mode="alert_and_block")
    # Seed pre-blocked state.
    with get_db() as db:
        cap = (
            db.query(ServiceAccountCap)
            .filter(
                ServiceAccountCap.identity_key
                == "role:MyBatchJobRole"
            )
            .first()
        )
        cap.blocked_at = datetime.now(timezone.utc)
    _seed_metric_for("role:MyBatchJobRole", spend=2.0)

    import worker.jobs.service_account_monitor as sam
    sam.run()

    fake_iam.delete_role_policy.assert_called_once()
    with get_db() as db:
        cap = (
            db.query(ServiceAccountCap)
            .filter(
                ServiceAccountCap.identity_key
                == "role:MyBatchJobRole"
            )
            .first()
        )
        assert cap.blocked_at is None
