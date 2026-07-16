"""
governance_drift_check — daily (and on-demand) fleet-wide sweep
that flags principals whose governance intent (`users.governed`)
disagrees with IAM truth (the deny policy's actual attachment).
#649.

The deny reconciler does NOT heal this: it only ensures the deny
on the ONE shared tg-consumer role, so a governed principal on any
other role (or one whose deny was self-detached / wiped by IDC
re-provision / removed by a manual console edit) can read
"governed" while enforcing nothing.

Detect + alert ONLY — this job writes NO IAM and flips NO flag
(owner decision). It records drift rows into `governance_drift` and
the API surfaces the count as an org-admin nav badge. The admin
corrects via Manage/Unmanage on the detail page.

Clean-slate model: each sweep REPLACES the whole drift set — it
deletes every existing `governance_drift` row then inserts only this
run's findings, in ONE transaction. So the table always holds exactly
the current sweep's drift, and a clean sweep leaves it empty (the
banner clears on a re-run with no manual DB edit). Drift history is
intentionally not retained (detect-and-alert, not an audit log —
owner decision). Because a clean sweep leaves no row to carry the
"last checked" time, the run stamps its timestamp on the
`last_drift_sweep_at` admin_config key for the banner.

Read-only IAM: `iam:ListAttachedRolePolicies` only.
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timezone

import boto3

from db.session import get_db
from db.models import User, GovernanceDrift
from db.drift_sweep import set_last_drift_sweep_at
from governance import (
    verify, _role_name_from_arn,
    DRIFT, GOVERNED_NO_DENY, DENY_NO_GOVERNED, MANAGED,
)

log = logging.getLogger("worker.governance_drift_check")

REGION = os.environ.get("AWS_REGION", "us-east-1")


def run() -> dict:
    """Sweep all principals; record drift for this sweep. Returns
    a summary dict (drift count + breakdown) for the JobRun log and
    the Jobs-page summary."""
    iam = boto3.client("iam", region_name=REGION)
    deny_cache: dict[str, bool] = {}

    with get_db() as db:
        users = db.query(User).all()

        # NOTE: the reverse-direction (DENY_NO_GOVERNED) false-positive
        # guard now lives in verify() itself — it flags an ungoverned
        # principal ONLY when the deny policy document contains a
        # per-aws:userid Deny naming THEM, not merely because the
        # policy is attached to a role they share. So a verdict of
        # DRIFT with governed=false here already means a real enforcing
        # statement contradicts the flag; no shared-role guard needed.

        # One timestamp for the whole sweep (stamped on every row and
        # on the last_drift_sweep_at marker below).
        sweep = datetime.now(timezone.utc)
        findings: list[GovernanceDrift] = []
        unknown = 0

        for u in users:
            verdict = verify(u, iam=iam, deny_cache=deny_cache)
            if verdict != DRIFT:
                if verdict == "unknown":
                    unknown += 1
                continue

            role_name = _role_name_from_arn(u.principal_arn)
            if u.governed:
                direction = GOVERNED_NO_DENY
                expected, actual = MANAGED, "deny-not-attached"
                detail = (
                    "governed=true but tg-BedrockQuotaDeny is not "
                    f"attached to role {role_name} — enforcing nothing"
                )
            else:
                # verify() only returns DRIFT here when the policy
                # document holds a real per-aws:userid Deny naming this
                # principal — an actual enforcing statement that
                # contradicts governed=false (not mere role attachment).
                direction = DENY_NO_GOVERNED
                expected, actual = "unmanaged", "deny-enforced"
                detail = (
                    "governed=false but tg-BedrockQuotaDeny contains a "
                    f"per-user Deny for this principal on role "
                    f"{role_name} — actively enforcing a deny. Re-Manage "
                    "then Unmanage to reconcile, or investigate the stale "
                    "statement."
                )

            findings.append(GovernanceDrift(
                sweep_at=sweep,
                identity_key=u.identity_key or u.email,
                email=u.email,
                role_arn=u.principal_arn,
                direction=direction,
                expected=expected,
                actual=actual,
                detail=detail,
            ))

        # Clean-slate: replace the whole drift set with THIS sweep's
        # findings. Delete-all then insert happen in the one `get_db()`
        # transaction, so a concurrent reader never sees a half-empty
        # table (torn read). A clean sweep deletes everything and
        # inserts nothing → table empty → banner clears on re-run.
        db.query(GovernanceDrift).delete(synchronize_session=False)
        for f in findings:
            db.add(f)
        # Stamp the completed-sweep time so the banner shows a fresh
        # "last checked" even when this sweep was clean (no rows to
        # carry sweep_at).
        set_last_drift_sweep_at(db, sweep)
        db.flush()

        gnd = sum(
            1 for f in findings if f.direction == GOVERNED_NO_DENY)
        dng = sum(
            1 for f in findings if f.direction == DENY_NO_GOVERNED)
        log.info(
            "governance_drift_check: %d drift (%d governed-no-deny, "
            "%d deny-no-governed), %d unknown (IAM unreadable)",
            len(findings), gnd, dng, unknown,
        )
        return {
            "drift": len(findings),
            "governed_no_deny": gnd,
            "deny_no_governed": dng,
            "unknown": unknown,
            "swept": len(users),
        }
