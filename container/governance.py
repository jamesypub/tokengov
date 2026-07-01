"""
governance.py — shared deny-only governance verify helper (#649).

The single definition of "is this principal's governance state in
sync with IAM?", used by the daily `governance_drift_check` worker
job (and available to the API). It compares tg's *intent*
(`users.governed`) against IAM *truth*
(`iam:ListAttachedRolePolicies` on the principal's role).

Why this is net-new: #642 shipped as a UI-only fix (the Manage/
Unmanage confirm dialog), so no server-side IAM-verify helper
existed — this is the first one (#649 re-scope, tg-lead 2026-06-07).

Read-only: this module calls ONLY `iam:ListAttachedRolePolicies`.
It writes no IAM and flips no flag — detect+alert only (owner
decision). The grant for that read is a one-shot ops CFN deploy on
the task role; until it lands, verify() reports `unknown` rather
than crashing the sweep.
"""
from __future__ import annotations
import logging
import os

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("governance")

REGION      = os.environ.get("AWS_REGION", "us-east-1")
POLICY_NAME = os.environ.get("DENY_POLICY_NAME", "tg-BedrockQuotaDeny")

# Verify verdicts.
MANAGED   = "managed"     # governed AND deny attached — in sync
UNMANAGED = "unmanaged"   # not governed AND deny absent — in sync
DRIFT     = "drift"       # governed flag disagrees with IAM truth
IDC       = "idc"         # surface-only; tg never attaches → never drift
UNKNOWN   = "unknown"     # couldn't read IAM (no grant / API error)

# Drift directions (recorded on GovernanceDrift.direction).
GOVERNED_NO_DENY = "governed_no_deny"
DENY_NO_GOVERNED = "deny_no_governed"


def _role_name_from_arn(role_arn: str | None) -> str | None:
    """IAM role NAME from a role ARN
    (arn:aws:iam::<acct>:role/<name>). None if absent/not a role
    ARN. Mirrors users.py / deny_reconciler conventions."""
    if not role_arn:
        return None
    marker = ":role/"
    i = role_arn.find(marker)
    if i == -1:
        return None
    return role_arn[i + len(marker):]


def _is_idc(user) -> bool:
    return (getattr(user, "role_type", None) or "iam") == "idc"


def deny_attached(iam, role_name: str) -> bool:
    """True if tg-BedrockQuotaDeny is attached to `role_name`.
    Raises ClientError on an IAM failure (caller decides whether
    to treat as UNKNOWN)."""
    paginator = iam.get_paginator("list_attached_role_policies")
    for page in paginator.paginate(RoleName=role_name):
        for pol in page.get("AttachedPolicies", []):
            if pol.get("PolicyName") == POLICY_NAME:
                return True
    return False


def verify(user, iam=None, deny_cache: dict | None = None) -> str:
    """Return the verified governance verdict for `user`:
    MANAGED / UNMANAGED / DRIFT / IDC / UNKNOWN.

    `iam` — a boto3 IAM client (one is created if omitted; pass one
    when sweeping many principals). `deny_cache` — optional
    {role_name: bool} memo so a sweep makes ONE
    ListAttachedRolePolicies call per role even when many
    principals share it (the shared tg-consumer model)."""
    if _is_idc(user):
        return IDC

    role_name = _role_name_from_arn(getattr(user, "principal_arn", None))
    governed = bool(getattr(user, "governed", False))

    # No attachable role (iam_user / root / unobserved): there is
    # nowhere for the deny to live, so "governed" can only be the
    # in-sync unmanaged state. Not drift.
    if not role_name:
        return UNMANAGED

    if iam is None:
        iam = boto3.client("iam", region_name=REGION)

    try:
        if deny_cache is not None and role_name in deny_cache:
            attached = deny_cache[role_name]
        else:
            attached = deny_attached(iam, role_name)
            if deny_cache is not None:
                deny_cache[role_name] = attached
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code")
        # The iam:ListAttachedRolePolicies grant is a one-shot ops
        # CFN deploy (#649). Until it lands, report UNKNOWN loudly
        # rather than crash the whole sweep.
        log.warning(
            "verify: ListAttachedRolePolicies failed for role %s "
            "(%s) — reporting UNKNOWN; is the task-role grant "
            "deployed?", role_name, code,
        )
        return UNKNOWN

    if governed and not attached:
        return DRIFT          # GOVERNED_NO_DENY
    if attached and not governed:
        return DRIFT          # DENY_NO_GOVERNED (caller applies the
                              # shared-role guard before recording)
    return MANAGED if governed else UNMANAGED
