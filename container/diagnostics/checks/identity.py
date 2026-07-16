"""identity.* checks — the caller's AWS creds, account, and region.

All read-only: sts:GetCallerIdentity (needs no permission) + env reads.
"""
from __future__ import annotations

from diagnostics.model import (
    CheckResult, Check, PASS, FAIL, INFO, WARNING, CRITICAL, ERROR,
)
from api.aws_errors import is_expired_cred_error, EXPIRED_CRED_DETAIL

CATEGORY = "identity"


def _caller_identity(ctx):
    """(account, arn) from sts:GetCallerIdentity, or raises."""
    sts = ctx.client("sts")
    resp = sts.get_caller_identity()
    return resp.get("Account"), resp.get("Arn")


def check_caller(ctx) -> CheckResult:
    try:
        account, arn = _caller_identity(ctx)
    except Exception as e:  # noqa: BLE001 — classify creds vs other
        remediation = (
            EXPIRED_CRED_DETAIL if is_expired_cred_error(e)
            else "sts:GetCallerIdentity failed — the container's AWS "
                 "credentials are invalid or unavailable. Check the "
                 "task-role / instance-role / IRSA cred source.")
        return CheckResult(
            id="identity.caller", title="AWS credentials resolve",
            status=FAIL, category=CATEGORY, severity=CRITICAL,
            detail=f"sts:GetCallerIdentity failed: {e}",
            remediation=remediation,
            docs_url="docs/creds-refresh.md")
    # Cache the resolved account on ctx so account-match can compare
    # without a second STS call.
    ctx.account_id = account or ctx.account_id
    return CheckResult(
        id="identity.caller", title="AWS credentials resolve",
        status=PASS, category=CATEGORY, severity=INFO,
        detail=f"Caller {arn} in account {account}.", remediation="")


def check_account_match(ctx) -> CheckResult:
    configured = ctx.configured_account_id
    if not configured:
        # No AWS_ACCOUNT_ID configured — nothing to compare against.
        return CheckResult(
            id="identity.account-match", title="Account matches config",
            status=PASS, category=CATEGORY, severity=INFO,
            detail="AWS_ACCOUNT_ID is not set — skipping account match.",
            remediation="")
    try:
        resolved, _ = _caller_identity(ctx)
    except Exception as e:  # noqa: BLE001 — caller check owns cred detail
        return CheckResult(
            id="identity.account-match", title="Account matches config",
            status=ERROR, category=CATEGORY, severity=WARNING,
            detail=f"Could not resolve caller account: {e}",
            remediation="Fix identity.caller first (creds unavailable).")
    if resolved == configured:
        return CheckResult(
            id="identity.account-match", title="Account matches config",
            status=PASS, category=CATEGORY, severity=INFO,
            detail=f"Caller resolves to the configured account "
                   f"{configured}.", remediation="")
    return CheckResult(
        id="identity.account-match", title="Account matches config",
        status=FAIL, category=CATEGORY, severity=CRITICAL,
        detail=f"Creds resolve to account {resolved} but "
               f"AWS_ACCOUNT_ID={configured}.",
        remediation=f"Creds resolve to account {resolved} but "
                    f"AWS_ACCOUNT_ID={configured} — wrong account; "
                    f"check your AWS_PROFILE / SSO session.")


def check_region(ctx) -> CheckResult:
    if ctx.region == "us-east-1":
        return CheckResult(
            id="identity.region", title="Region is us-east-1",
            status=PASS, category=CATEGORY, severity=INFO,
            detail="AWS_REGION=us-east-1.", remediation="")
    return CheckResult(
        id="identity.region", title="Region is us-east-1",
        status=FAIL, category=CATEGORY, severity=CRITICAL,
        detail=f"AWS_REGION={ctx.region}.",
        remediation="This deployment is us-east-1 only (CUR 2.0 "
                    "operates there). Set AWS_REGION=us-east-1.")


CHECKS = [
    Check("identity.caller", "AWS credentials resolve", CATEGORY,
          CRITICAL, check_caller, docs_url="docs/creds-refresh.md"),
    Check("identity.account-match", "Account matches config", CATEGORY,
          CRITICAL, check_account_match),
    Check("identity.region", "Region is us-east-1", CATEGORY,
          CRITICAL, check_region),
]
