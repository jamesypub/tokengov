"""cur_pipeline.* checks — CUR/Athena spend-source health + freshness.

Reuses the CUR route's plain-function core (cur_health_result), the
data-through query, and a live partition-scoped Athena count. Read-only
(Athena/Glue/S3 Get/List + a DB read).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import func

from diagnostics.model import (
    CheckResult, Check, PASS, WARN, FAIL,
    INFO, WARNING, CRITICAL,
)

CATEGORY = "cur_pipeline"

# CUR-health status → diagnostics status. principal_blank heals ≤24h so
# it's a warn; the structural-missing states are fails.
_HEALTH_STATUS_MAP = {
    "healthy": PASS,
    "principal_blank": WARN,
    "absent": FAIL,
    "columns_missing": FAIL,
    "principal_absent_from_export": FAIL,
}


def check_health(ctx) -> CheckResult:
    from api.routes.cur import cur_health_result
    verdict = cur_health_result()
    state = verdict.get("status", "absent")
    status = _HEALTH_STATUS_MAP.get(state, FAIL)
    detail = verdict.get("detail") or "CUR spend source is healthy."
    return CheckResult(
        id="cur_pipeline.health", title="CUR spend source health",
        status=status, category=CATEGORY,
        severity=INFO if status == PASS else (
            WARNING if status == WARN else CRITICAL),
        detail=f"{state}: {detail}",
        # cur_health's own detail is the remediation for non-pass.
        remediation="" if status == PASS else (verdict.get("detail") or ""))


def _latest_usage_hour(ctx):
    """max(CurUserSpend.usage_hour) via a DB read (the data-through
    query), or None if empty."""
    from db.models import CurUserSpend
    with ctx.db() as db:
        return db.query(func.max(CurUserSpend.usage_hour)).scalar()


def check_freshness(ctx) -> CheckResult:
    latest = _latest_usage_hour(ctx)
    if latest is None:
        return CheckResult(
            id="cur_pipeline.freshness", title="CUR spend freshness",
            status=FAIL, category=CATEGORY, severity=CRITICAL,
            detail="No CUR spend rows in cur_user_spend yet.",
            remediation="No CUR spend has landed; AWS delivers with "
                        "≤24h lag on a fresh install. If it's been "
                        ">24h, check the worker's cur_spend_sync job.")
    now = datetime.now(timezone.utc)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    age = now - latest
    age_h = age.total_seconds() / 3600.0
    age_str = f"{age_h:.0f}h"
    if age_h <= 24:
        return CheckResult(
            id="cur_pipeline.freshness", title="CUR spend freshness",
            status=PASS, category=CATEGORY, severity=INFO,
            detail=f"Latest CUR spend is {age_str} old (≤24h).",
            remediation="")
    if age_h <= 48:
        return CheckResult(
            id="cur_pipeline.freshness", title="CUR spend freshness",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail=f"Latest CUR spend is {age_str} old (24–48h).",
            remediation=f"Latest CUR spend is {age_str} old; AWS "
                        "delivers with ≤24h lag, so 24–48h is at the "
                        "edge — watch the worker's cur_spend_sync job.")
    return CheckResult(
        id="cur_pipeline.freshness", title="CUR spend freshness",
        status=FAIL, category=CATEGORY, severity=CRITICAL,
        detail=f"Latest CUR spend is {age_str} old (>48h).",
        remediation=f"Latest CUR spend is {age_str} old; AWS delivers "
                    "with ≤24h lag; >48h means cur_spend_sync isn't "
                    "running — check the worker job.")


def check_athena_query(ctx) -> CheckResult:
    """Smoke the real Athena path: a partition-scoped count. Filters the
    PARTITION key (bill_billing_period_start_date), not the timestamp
    column — a timestamp filter scans and defeats the partition."""
    ath = ctx.client("athena")
    sql = (
        f'SELECT count(*) FROM "{ctx.athena_database}".'
        f'"{ctx.cur_table_name}" '
        "WHERE bill_billing_period_start_date >= "
        "DATE_TRUNC('month', CURRENT_DATE)"
    )
    try:
        exec_resp = ath.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": ctx.athena_database},
            WorkGroup=ctx.athena_workgroup,
        )
        eid = exec_resp["QueryExecutionId"]
        deadline = time.monotonic() + 30
        state = "QUEUED"
        reason = ""
        while time.monotonic() < deadline:
            st = ath.get_query_execution(QueryExecutionId=eid)
            status_obj = st["QueryExecution"]["Status"]
            state = status_obj["State"]
            if state == "SUCCEEDED":
                break
            if state in ("FAILED", "CANCELLED"):
                reason = status_obj.get("StateChangeReason", state)
                break
            time.sleep(1)
    except Exception as e:  # noqa: BLE001 — surface as a fail verdict
        return CheckResult(
            id="cur_pipeline.athena-query", title="Athena query path",
            status=FAIL, category=CATEGORY, severity=WARNING,
            detail=f"Athena query failed to start: {e}",
            remediation="The Athena query path errored — check the "
                        "workgroup / database / task-role Athena grants.")
    if state == "SUCCEEDED":
        return CheckResult(
            id="cur_pipeline.athena-query", title="Athena query path",
            status=PASS, category=CATEGORY, severity=INFO,
            detail="Partition-scoped Athena count succeeded.",
            remediation="")
    if state == "TIMEOUT" or state not in ("FAILED", "CANCELLED"):
        return CheckResult(
            id="cur_pipeline.athena-query", title="Athena query path",
            status=FAIL, category=CATEGORY, severity=WARNING,
            detail="Athena query did not complete within 30s.",
            remediation="The Athena probe timed out — the workgroup may "
                        "be saturated; retry.")
    return CheckResult(
        id="cur_pipeline.athena-query", title="Athena query path",
        status=FAIL, category=CATEGORY, severity=WARNING,
        detail=f"Athena query {state}: {reason}",
        remediation=f"Athena query {state}: {reason} — check the CUR "
                    "table / workgroup / task-role Athena grants.")


CHECKS = [
    Check("cur_pipeline.health", "CUR spend source health", CATEGORY,
          CRITICAL, check_health),
    Check("cur_pipeline.freshness", "CUR spend freshness", CATEGORY,
          CRITICAL, check_freshness),
    Check("cur_pipeline.athena-query", "Athena query path", CATEGORY,
          WARNING, check_athena_query),
]
