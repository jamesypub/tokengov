"""governance.* checks — is the deny actually enforcing?

Reuses governance.verify() (the same verdict the drift sweep uses),
JobRun for the reconciler's last run, GovernanceDrift for the latest
sweep, and read-only IAM (ListPolicyVersions). Read-only throughout.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from diagnostics.model import (
    CheckResult, Check, PASS, WARN, FAIL,
    INFO, WARNING, CRITICAL,
)

CATEGORY = "governance"


class _SyntheticUser:
    """A minimal user-like object so governance.verify() can classify
    the consumer role directly (governed=True → expect the deny
    attached). Mirrors the attributes verify() reads."""

    def __init__(self, principal_arn, governed=True, role_type="iam"):
        self.principal_arn = principal_arn
        self.governed = governed
        self.role_type = role_type


def _consumer_role_arn(ctx):
    acct = ctx.account_id or ctx.configured_account_id
    if not acct:
        return None
    return f"arn:aws:iam::{acct}:role/{ctx.consumer_role_name}"


def check_reconciler_last_run(ctx) -> CheckResult:
    from db.models import JobRun
    from db.jobs_pause import get_jobs_paused_until
    with ctx.db() as db:
        paused_until = get_jobs_paused_until(db)
        row = (
            db.query(JobRun)
            .filter(JobRun.job_name == "deny_reconciler")
            .order_by(JobRun.started_at.desc())
            .first()
        )
        # Snapshot fields inside the session (avoid detached access).
        status = row.status if row else None
        started_at = row.started_at if row else None
        error = (row.error or row.detail or "") if row else ""

    if row is None:
        # A pause window with no row is NOT a failure (scheduled runs
        # skipped by jobs_paused_until write no JobRun row).
        if paused_until is not None:
            return CheckResult(
                id="governance.reconciler-last-run",
                title="Deny reconciler last run",
                status=WARN, category=CATEGORY, severity=WARNING,
                detail=f"No reconciler run recorded; jobs are paused "
                       f"until {paused_until.isoformat()}.",
                remediation="Jobs are paused — the reconciler is "
                            "intentionally not running. Resume jobs to "
                            "re-enable enforcement.")
        return CheckResult(
            id="governance.reconciler-last-run",
            title="Deny reconciler last run",
            status=FAIL, category=CATEGORY, severity=CRITICAL,
            detail="The deny_reconciler has never run.",
            remediation="Reconciler has never run: governance is not "
                        "being enforced — check worker scheduling + "
                        "task-role IAM grants.")

    now = datetime.now(timezone.utc)
    if started_at is not None and started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    ts = started_at.isoformat() if started_at else "unknown"

    if status == "failed":
        return CheckResult(
            id="governance.reconciler-last-run",
            title="Deny reconciler last run",
            status=FAIL, category=CATEGORY, severity=CRITICAL,
            detail=f"Last reconciler run failed at {ts}: {error}",
            remediation=f"Reconciler last run failed at {ts}: {error}. "
                        "Governance not being enforced — check worker "
                        "scheduling + task-role IAM grants.")
    if status == "succeeded":
        stale = (started_at is None
                 or (now - started_at) > timedelta(hours=2))
        if stale:
            return CheckResult(
                id="governance.reconciler-last-run",
                title="Deny reconciler last run",
                status=WARN, category=CATEGORY, severity=WARNING,
                detail=f"Last successful reconciler run was at {ts} "
                       "(>2h ago).",
                remediation="Reconciler last succeeded >2h ago — it "
                            "should run on a shorter cadence; check the "
                            "worker scheduler.")
        return CheckResult(
            id="governance.reconciler-last-run",
            title="Deny reconciler last run",
            status=PASS, category=CATEGORY, severity=INFO,
            detail=f"Reconciler succeeded at {ts}.", remediation="")
    # running / unknown status
    return CheckResult(
        id="governance.reconciler-last-run",
        title="Deny reconciler last run",
        status=WARN, category=CATEGORY, severity=WARNING,
        detail=f"Reconciler last status '{status}' at {ts}.",
        remediation="Reconciler is mid-run or in an unexpected state — "
                    "re-check shortly.")


def check_deny_attached(ctx) -> CheckResult:
    import governance
    arn = _consumer_role_arn(ctx)
    if not arn:
        return CheckResult(
            id="governance.deny-attached", title="Deny attached to role",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail="Account id unknown — cannot build the consumer-role "
                   "ARN to verify.",
            remediation="Set AWS_ACCOUNT_ID (or fix identity.caller) so "
                        "the deny-attachment can be verified.")
    iam = ctx.client("iam")
    verdict = governance.verify(
        _SyntheticUser(arn, governed=True), iam=iam)

    # Latest drift sweep count (informational; a non-zero sweep warns).
    drift_n, sweep_at = _latest_sweep(ctx)

    if verdict == governance.MANAGED:
        if drift_n:
            return CheckResult(
                id="governance.deny-attached",
                title="Deny attached to role",
                status=WARN, category=CATEGORY, severity=WARNING,
                detail=f"Deny attached to {ctx.consumer_role_name}, but "
                       f"{drift_n} drifted principal(s) in the last "
                       f"sweep ({sweep_at}).",
                remediation=f"{drift_n} principals drifted in the last "
                            f"sweep ({sweep_at}) — review the "
                            "governance-drift report.")
        return CheckResult(
            id="governance.deny-attached", title="Deny attached to role",
            status=PASS, category=CATEGORY, severity=INFO,
            detail=f"{ctx.deny_policy_name} attached to "
                   f"{ctx.consumer_role_name}.", remediation="")
    if verdict == governance.UNKNOWN:
        return CheckResult(
            id="governance.deny-attached", title="Deny attached to role",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail="IAM unreadable — cannot confirm the deny attachment.",
            remediation="Could not read the role's attached policies — "
                        "the iam:ListAttachedRolePolicies task-role "
                        "grant may be missing.")
    # DRIFT (governed-no-deny) — the deny isn't attached.
    return CheckResult(
        id="governance.deny-attached", title="Deny attached to role",
        status=FAIL, category=CATEGORY, severity=CRITICAL,
        detail=f"{ctx.deny_policy_name} NOT attached to "
               f"{ctx.consumer_role_name} — enforcing nothing.",
        remediation=f"{ctx.deny_policy_name} not attached to "
                    f"{ctx.consumer_role_name} — enforcing nothing. "
                    f"{drift_n} drifted principals in the last sweep "
                    f"({sweep_at}). Check the reconciler.")


def _latest_sweep(ctx):
    """(count, sweep_at_iso) of the latest governance_drift sweep, or
    (0, 'never')."""
    from db.models import GovernanceDrift
    from sqlalchemy import func
    with ctx.db() as db:
        latest = db.query(
            func.max(GovernanceDrift.sweep_at)).scalar()
        if latest is None:
            return 0, "never"
        n = (
            db.query(func.count(GovernanceDrift.id))
            .filter(GovernanceDrift.sweep_at == latest)
            .scalar()
        )
        return int(n or 0), latest.isoformat()


def check_policy_version_count(ctx) -> CheckResult:
    """The managed deny policy caps at 5 versions; the reconciler prunes
    the oldest non-default before the 6th. A wedged prune fails the next
    reconcile. iam:ListPolicyVersions (read-only)."""
    iam = ctx.client("iam")
    acct = ctx.account_id or ctx.configured_account_id
    if not acct:
        return CheckResult(
            id="governance.policy-version-count",
            title="Deny policy version count",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail="Account id unknown — cannot build the policy ARN.",
            remediation="Set AWS_ACCOUNT_ID (or fix identity.caller).")
    policy_arn = f"arn:aws:iam::{acct}:policy/{ctx.deny_policy_name}"
    try:
        resp = iam.list_policy_versions(PolicyArn=policy_arn)
        n = len(resp.get("Versions", []))
    except Exception as e:  # noqa: BLE001 — soft-warn, never crash
        return CheckResult(
            id="governance.policy-version-count",
            title="Deny policy version count",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail=f"Could not list policy versions: {e}",
            remediation="Could not read the deny policy's versions — the "
                        "iam:ListPolicyVersions grant may be missing, or "
                        "the policy doesn't exist yet.")
    if n <= 3:
        return CheckResult(
            id="governance.policy-version-count",
            title="Deny policy version count",
            status=PASS, category=CATEGORY, severity=INFO,
            detail=f"{ctx.deny_policy_name} at {n}/5 versions.",
            remediation="")
    if n == 4:
        return CheckResult(
            id="governance.policy-version-count",
            title="Deny policy version count",
            status=WARN, category=CATEGORY, severity=CRITICAL,
            detail=f"{ctx.deny_policy_name} at {n}/5 versions.",
            remediation=f"{ctx.deny_policy_name} at {n}/5 versions; the "
                        "reconciler prunes the oldest non-default before "
                        "the 6th — watch that the prune is working.")
    return CheckResult(
        id="governance.policy-version-count",
        title="Deny policy version count",
        status=FAIL, category=CATEGORY, severity=CRITICAL,
        detail=f"{ctx.deny_policy_name} at {n}/5 versions.",
        remediation=f"{ctx.deny_policy_name} at {n}/5 versions; the "
                    "reconciler prunes the oldest non-default before the "
                    "6th — a wedged prune fails the next reconcile. "
                    "Delete an old non-default version.")


def check_idc_boundary(ctx) -> CheckResult:
    """tg must never Attach/Detach an IDC-owned (AWSReservedSSO_*) role
    — IDC owns that attachment via the permission-set reference."""
    name = ctx.consumer_role_name or ""
    if name.startswith("AWSReservedSSO_"):
        return CheckResult(
            id="governance.idc-boundary", title="Consumer role is not IDC-owned",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail=f"Consumer role {name} is an AWSReservedSSO_* "
                   "(IDC-owned) role.",
            remediation="tg must never Attach/Detach IDC-owned roles — "
                        "IDC owns that attachment via the permission "
                        "set. Govern via the permission-set policy.")
    return CheckResult(
        id="governance.idc-boundary", title="Consumer role is not IDC-owned",
        status=PASS, category=CATEGORY, severity=INFO,
        detail=f"Consumer role {name} is not IDC-owned.", remediation="")


def _consumer_deny_attached(ctx, iam) -> bool | None:
    """True/False whether the deny is attached to tg-consumer, via
    iam:ListEntitiesForPolicy on the deny policy. None on read failure.
    Mirrors the route helper (routes/users.py) but reads from ctx."""
    acct = ctx.account_id or ctx.configured_account_id
    if not acct:
        return None
    policy_arn = f"arn:aws:iam::{acct}:policy/{ctx.deny_policy_name}"
    try:
        paginator = iam.get_paginator("list_entities_for_policy")
        for page in paginator.paginate(
                PolicyArn=policy_arn, EntityFilter="Role"):
            for r in page.get("PolicyRoles", []):
                if r.get("RoleName") == ctx.consumer_role_name:
                    return True
        return False
    except Exception:  # noqa: BLE001 — unreadable → None (UNKNOWN)
        return None


def check_idc_reference(ctx) -> CheckResult:
    """For each GOVERNED IDC (AWSReservedSSO_*) user, is the deny
    actually enforced from a role they use — the permission-set
    reference provisioned onto their SSO role, or tg-consumer they
    assume? tg can verify this in its own member account (read-only IAM)
    even though it can't read the IDC management account. A governed IDC
    user with neither is 'pending' — governed intent set, not enforced.
    """
    import governance
    from api import idc_enforcement as ie
    from db.models import User

    with ctx.db() as db:
        idc_governed = (
            db.query(User)
            .filter(User.governed.is_(True), User.role_type == "idc")
            .all()
        )
        # Snapshot inside the session (avoid detached access).
        rows = [(u.identity_key or u.email, u.principal_arn)
                for u in idc_governed]

    if not rows:
        return CheckResult(
            id="governance.idc-reference",
            title="IDC users enforced (permission-set reference)",
            status=PASS, category=CATEGORY, severity=INFO,
            detail="No governed IAM Identity Center users.",
            remediation="")

    # Building the client/session can itself fail (ProfileNotFound,
    # NoCredentials) — degrade to the UNKNOWN/WARN branch rather than
    # raising (the engine would isolate it, but a clean "couldn't
    # verify" WARN is the honest result, not an error card).
    try:
        iam = ctx.client("iam")
    except Exception:  # noqa: BLE001 — client build failed → all unknown
        return CheckResult(
            id="governance.idc-reference",
            title="IDC users enforced (permission-set reference)",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail=f"Could not verify enforcement for {len(rows)} "
                   "governed IDC user(s) — IAM client unavailable.",
            remediation="Could not build an IAM client (credentials / "
                        "profile) — enforcement for IDC users can't be "
                        "verified.")
    consumer_attached = _consumer_deny_attached(ctx, iam)

    pending, enforced, unknown = [], [], []
    for ident, arn in rows:
        sso_role = ie.idc_role_name(arn)
        try:
            sso_attached = (
                governance.deny_attached(iam, sso_role)
                if sso_role else False)
        except Exception:  # noqa: BLE001 — unreadable → UNKNOWN
            sso_attached = None
        # Trust wiring is a positive signal for the consumer path; if we
        # can't read it, treat as wired=True (conservative for the check
        # — the reconciler wires it on Govern, so a deny on tg-consumer
        # means enforced-via-consumer; the per-user route reads the
        # trust precisely).
        state = ie.classify(
            sso_role_attached=sso_attached,
            consumer_attached=consumer_attached,
            consumer_trust_wired=True,
        )
        if state in (ie.ENFORCED_HERE, ie.ENFORCED_VIA_CONSUMER):
            enforced.append(ident)
        elif state == ie.UNKNOWN:
            unknown.append(ident)
        else:
            pending.append(ident)

    n = len(rows)
    if pending:
        return CheckResult(
            id="governance.idc-reference",
            title="IDC users enforced (permission-set reference)",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail=f"{len(pending)}/{n} governed IDC user(s) not yet "
                   f"enforced: {', '.join(pending[:5])}"
                   f"{' …' if len(pending) > 5 else ''}.",
            remediation="Governance intent is set but not yet active for "
                        "these IDC users. An identity administrator must "
                        "apply the governance policy to their access "
                        "(reference it on the permission set), or the "
                        "users must reach Bedrock through the governed "
                        "consumer role, before limits take effect.")
    if unknown:
        return CheckResult(
            id="governance.idc-reference",
            title="IDC users enforced (permission-set reference)",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail=f"Could not verify enforcement for {len(unknown)}/{n} "
                   f"governed IDC user(s) — IAM read failed.",
            remediation="Could not read the IDC users' roles / deny "
                        "attachments — the iam:ListAttachedRolePolicies "
                        "/ iam:ListEntitiesForPolicy task-role grants may "
                        "be missing.")
    return CheckResult(
        id="governance.idc-reference",
        title="IDC users enforced (permission-set reference)",
        status=PASS, category=CATEGORY, severity=INFO,
        detail=f"All {n} governed IDC user(s) enforced.",
        remediation="")


def check_invocation_logs(ctx) -> CheckResult:
    """For each region in the invocation-logging CATALOG marked
    enabled, is Bedrock invocation logging ACTUALLY live to that
    region's bucket? The catalog (admin_config) records intent; the
    Bedrock GetModelInvocationLoggingConfiguration singleton is the
    truth. A catalog-enabled region whose live config is off (or points
    elsewhere) → WARN. No catalog entries → PASS (feature not in use).
    Read-only (bedrock:Get…). This is the analytics capture stream, not
    spend/deny — informational."""
    from db.invlogs_config import get_invlogs_regions
    acct = ctx.account_id or ctx.configured_account_id or ""
    with ctx.db() as db:
        catalog = get_invlogs_regions(db, acct)

    enabled = [e for e in catalog if e.get("enabled")]
    if not enabled:
        return CheckResult(
            id="governance.invocation-logs",
            title="Invocation logging live where enabled",
            status=PASS, category=CATEGORY, severity=INFO,
            detail="Invocation-logging analytics capture not enabled "
                   "in any region.",
            remediation="")

    from api import invlogs_apply as ia
    not_live, unreadable = [], []
    for e in enabled:
        region, bucket = e["region"], e["bucket"]
        try:
            # Region-bound client — the config is a same-region
            # singleton. DiagContext.client memoizes by service, so
            # build a region-bound one from the same session.
            sf = getattr(ctx, "_session_factory", None)
            sess = sf() if sf else __import__(
                "api.aws_session", fromlist=["get_aws_session"]
            ).get_aws_session()
            br = sess.client("bedrock", region_name=region)
            live = ia._live_config(br)
        except Exception:  # noqa: BLE001 — unreadable → flag, don't crash
            unreadable.append(region)
            continue
        if live is None or ia._live_s3_bucket(live) != bucket:
            not_live.append(region)

    n = len(enabled)
    if not_live:
        return CheckResult(
            id="governance.invocation-logs",
            title="Invocation logging live where enabled",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail=f"{len(not_live)}/{n} region(s) marked for "
                   f"invocation logging are not live: "
                   f"{', '.join(not_live[:5])}.",
            remediation="Re-save the invocation-logging settings to "
                        "re-apply, or check that another config isn't "
                        "already occupying the per-region logging slot.")
    if unreadable:
        return CheckResult(
            id="governance.invocation-logs",
            title="Invocation logging live where enabled",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail=f"Could not verify {len(unreadable)}/{n} region(s) "
                   "— Bedrock logging config unreadable.",
            remediation="Could not read the invocation-logging config "
                        "— the bedrock:GetModelInvocationLoggingConfiguration "
                        "task-role grant may be missing.")
    return CheckResult(
        id="governance.invocation-logs",
        title="Invocation logging live where enabled",
        status=PASS, category=CATEGORY, severity=INFO,
        detail=f"All {n} enabled region(s) logging live.",
        remediation="")


CHECKS = [
    Check("governance.reconciler-last-run", "Deny reconciler last run",
          CATEGORY, CRITICAL, check_reconciler_last_run),
    Check("governance.deny-attached", "Deny attached to role",
          CATEGORY, CRITICAL, check_deny_attached),
    Check("governance.policy-version-count", "Deny policy version count",
          CATEGORY, CRITICAL, check_policy_version_count),
    Check("governance.idc-boundary", "Consumer role is not IDC-owned",
          CATEGORY, WARNING, check_idc_boundary),
    Check("governance.idc-reference",
          "IDC users enforced (permission-set reference)",
          CATEGORY, WARNING, check_idc_reference),
    Check("governance.invocation-logs",
          "Invocation logging live where enabled",
          CATEGORY, WARNING, check_invocation_logs),
]
