"""
service_account_monitor (#346) — periodically sums spend
per service-role identity_key over each cap's period,
fires threshold alerts at the configured pct, and
optionally writes an inline deny on the role at budget
exhaustion.

Trust boundary: the worker only writes IAM perms when the
target role carries `tg-budget-enforced=true`. Untagged
roles surface as `tag_missing` in the API and never get an
IAM call. Mirrors #344's pattern.

Alert delivery itself happens in
service_alert_dispatcher (separate job) — this monitor only
queues alert rows.
"""
from __future__ import annotations
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import func

from db.session import get_db
from db.models import (
    CurUserSpend, ServiceAccountAlert, ServiceAccountCap, User,
)

log = logging.getLogger("worker.service_account_monitor")

REGION = os.environ.get("AWS_REGION", "us-east-1")
DENY_POLICY_NAME = os.environ.get(
    "TG_SERVICE_BUDGET_DENY_POLICY",
    "tg-service-budget-deny",
)
TAG_KEY = "tg-budget-enforced"
TAG_VALUE = "true"

DENY_POLICY_DOCUMENT = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "ServiceBudgetExhausted",
        "Effect": "Deny",
        "Action": [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
            "bedrock:Converse",
            "bedrock:ConverseStream",
        ],
        "Resource": "*",
    }],
}, separators=(",", ":"))


def _period_window(period: str, now: datetime) -> tuple[
    datetime, datetime, str
]:
    """Returns (window_start, window_end, period_key) for
    the given period anchored at `now` (UTC). period_key
    is the dedup key for alerts (`YYYY-MM-DD` for day,
    `YYYY-Wnn` for ISO week, `YYYY-MM` for month)."""
    if period == "day":
        start = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=1)
        return start, end, start.strftime("%Y-%m-%d")
    if period == "week":
        # ISO week: Monday is the start.
        d = now.date()
        weekday = d.weekday()
        monday = d - timedelta(days=weekday)
        start = datetime.combine(
            monday, datetime.min.time(),
            tzinfo=timezone.utc,
        )
        end = start + timedelta(days=7)
        iso_year, iso_week, _ = d.isocalendar()
        return start, end, f"{iso_year}-W{iso_week:02d}"
    # month
    start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end, start.strftime("%Y-%m")


def _spend_in_window(
    db, identity_key: str, start: datetime, end: datetime,
) -> float:
    """Sum quota_metrics.spend_usd for an identity_key
    across the window. quota_metrics is keyed by `email`
    today; for service rows the synthetic email equals
    identity_key (set on JIT create in metrics_aggregator).

    #643: with the per-day grain we can filter usage_date to the
    EXACT [start, end) window instead of over-fetching whole
    months — the budget window (day/week/month) maps directly onto
    a usage_date range.
    """
    total = (
        db.query(func.coalesce(func.sum(CurUserSpend.spend_usd), 0))
        .filter(CurUserSpend.email == identity_key)
        .filter(CurUserSpend.usage_hour >= start.date())
        .filter(CurUserSpend.usage_hour < end.date())
        .scalar()
    )
    return float(total or 0.0)


def _alert_already_fired(
    db, identity_key: str, kind: str, period_key: str,
) -> bool:
    return db.query(ServiceAccountAlert).filter(
        ServiceAccountAlert.identity_key == identity_key,
        ServiceAccountAlert.kind == kind,
        ServiceAccountAlert.period_key == period_key,
    ).first() is not None


def _enqueue_alert(
    db, identity_key: str, kind: str,
    pct: float, period_key: str,
) -> None:
    db.add(ServiceAccountAlert(
        identity_key=identity_key,
        kind=kind,
        pct_of_budget=pct,
        period_key=period_key,
    ))


def _role_name_from_identity_key(identity_key: str) -> str | None:
    """`role:MyEcsTaskRole` → `MyEcsTaskRole`. Returns None
    for non-role identity_keys so the caller can skip the
    IAM write."""
    if identity_key.startswith("role:"):
        return identity_key[len("role:"):]
    return None


def _is_tagged_for_enforcement(iam, role_name: str) -> bool:
    """Customer-controlled opt-in: only roles tagged
    `tg-budget-enforced=true` are eligible for the inline
    deny. Mirrors #344's pattern. Untagged roles surface
    as `tag_missing` and never get an IAM call."""
    try:
        resp = iam.list_role_tags(RoleName=role_name)
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code")
        if code in ("NoSuchEntity", "AccessDenied"):
            return False
        raise
    for tag in resp.get("Tags", []):
        if (
            tag.get("Key") == TAG_KEY
            and tag.get("Value") == TAG_VALUE
        ):
            return True
    return False


def _put_deny(iam, role_name: str) -> None:
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=DENY_POLICY_NAME,
        PolicyDocument=DENY_POLICY_DOCUMENT,
    )


def _delete_deny(iam, role_name: str) -> None:
    try:
        iam.delete_role_policy(
            RoleName=role_name,
            PolicyName=DENY_POLICY_NAME,
        )
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code")
        if code == "NoSuchEntity":
            return
        raise


def run() -> str:
    """One pass: for each cap, compute spend in current
    period, fire threshold/exhausted/unblocked alerts as
    needed, optionally write inline deny."""
    iam = boto3.client("iam", region_name=REGION)
    now = datetime.now(timezone.utc)
    threshold_fired = 0
    blocked = 0
    unblocked = 0

    with get_db() as db:
        caps = (
            db.query(ServiceAccountCap)
            .filter(ServiceAccountCap.mode != "disabled")
            .all()
        )
        for cap in caps:
            start, end, period_key = _period_window(
                cap.period, now,
            )
            spend = _spend_in_window(
                db, cap.identity_key, start, end,
            )
            pct = (
                (spend / cap.budget_usd) * 100.0
                if cap.budget_usd > 0 else 0.0
            )

            # Threshold alert.
            if (
                pct >= (cap.alert_threshold_pct or 80)
                and pct < 100 + (cap.grace_pct or 0)
                and not _alert_already_fired(
                    db, cap.identity_key,
                    "threshold", period_key,
                )
            ):
                _enqueue_alert(
                    db, cap.identity_key,
                    "threshold", pct, period_key,
                )
                threshold_fired += 1

            # Budget exhausted: alert + optional deny.
            over_cap = pct >= 100 + (cap.grace_pct or 0)
            role_name = _role_name_from_identity_key(
                cap.identity_key,
            )
            if over_cap:
                if not _alert_already_fired(
                    db, cap.identity_key,
                    "budget_exhausted", period_key,
                ):
                    _enqueue_alert(
                        db, cap.identity_key,
                        "budget_exhausted", pct, period_key,
                    )
                if (
                    cap.mode == "alert_and_block"
                    and role_name
                    and _is_tagged_for_enforcement(iam, role_name)
                    and cap.blocked_at is None
                ):
                    try:
                        _put_deny(iam, role_name)
                        cap.blocked_at = now
                        blocked += 1
                    except ClientError as e:
                        log.warning(
                            "service_account_monitor: "
                            "PutRolePolicy on %s failed: %s",
                            role_name, e,
                        )

            # Auto-unblock at period boundary or when spend
            # dropped below cap (manual rate change can do
            # this). Only meaningful when this cap actually
            # wrote a deny.
            if (
                cap.blocked_at is not None
                and cap.auto_unblock
                and not over_cap
                and role_name
            ):
                try:
                    _delete_deny(iam, role_name)
                    cap.blocked_at = None
                    if not _alert_already_fired(
                        db, cap.identity_key,
                        "unblocked", period_key,
                    ):
                        _enqueue_alert(
                            db, cap.identity_key,
                            "unblocked", pct, period_key,
                        )
                    unblocked += 1
                except ClientError as e:
                    log.warning(
                        "service_account_monitor: "
                        "DeleteRolePolicy on %s failed: %s",
                        role_name, e,
                    )

    return (
        f"thresholds={threshold_fired} "
        f"blocked={blocked} "
        f"unblocked={unblocked}"
    )
