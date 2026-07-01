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
(owner decision). It records drift rows into `governance_drift`
(one sweep = one `sweep_at` group; latest sweep = current drift)
and the API surfaces the count as an org-admin nav badge. The
admin corrects via Manage/Unmanage on the detail page.

Read-only IAM: `iam:ListAttachedRolePolicies` only.
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timezone

import boto3

from db.session import get_db
from db.models import User, GovernanceDrift
from governance import (
    verify, _role_name_from_arn, _is_idc,
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

        # Which roles host at least one governed principal? Used for
        # the reverse-direction guard: in the shared tg-consumer
        # model the deny attaches at the ROLE but enforces per-person
        # via aws:userid, so a deny-present role is EXPECTED whenever
        # any governed principal lives there — flagging every
        # ungoverned co-tenant as drift would be a false positive.
        governed_roles: set[str] = set()
        for u in users:
            if _is_idc(u) or not u.governed:
                continue
            rn = _role_name_from_arn(u.principal_arn)
            if rn:
                governed_roles.add(rn)

        # One timestamp for the whole sweep so all rows share a
        # sweep_at group (latest group = current drift set).
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
                # deny present but not governed: only drift if NO
                # other governed principal shares this role.
                if role_name in governed_roles:
                    continue  # shared-role norm, not drift
                direction = DENY_NO_GOVERNED
                expected, actual = "unmanaged", "deny-attached"
                detail = (
                    "deny attached to role "
                    f"{role_name} but governed=false and no other "
                    "governed principal shares this role"
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

        for f in findings:
            db.add(f)
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
