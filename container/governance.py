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

Read-only: this module calls `iam:ListAttachedRolePolicies` (is the
deny attached to the role?) and, for the reverse direction,
`iam:GetPolicy` / `iam:GetPolicyVersion` (does the deny document hold
a per-aws:userid Deny naming this principal?). It writes no IAM and
flips no flag — detect+alert only (owner decision). The deny-policy
ARN is CONSTRUCTED deterministically from the account (STS
GetCallerIdentity, or AWS_ACCOUNT_ID) — we deliberately avoid
`iam:ListPolicies`, which can't be resource-scoped and would force a
`Resource:"*"` grant on the task role; the resource-scoped
GetPolicy* grants suffice. When a grant is missing / IAM errors,
verify() reports `unknown` rather than crashing the sweep.
"""
from __future__ import annotations
import logging
import os

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("governance")

REGION      = os.environ.get("AWS_REGION", "us-east-1")
POLICY_NAME = os.environ.get("DENY_POLICY_NAME", "tg-BedrockQuotaDeny")
ACCOUNT_ID  = os.environ.get("AWS_ACCOUNT_ID")

# IAM ClientError codes that mean "the policy simply isn't there yet"
# (no deny has ever been built) — a benign absent state, NOT an
# unreadable one. We map these to "no policy" (empty enforced set),
# distinct from AccessDenied / throttling which stay UNKNOWN.
_POLICY_ABSENT_CODES = {"NoSuchEntity", "NoSuchEntityException"}

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


# Sentinel key for the deny_cache slot that holds the policy's
# per-user Deny set (distinct from the per-role-name attachment
# bools that share the same cache dict).
_DENIED_USERIDS_KEY = "\0denied_userids"


def _denied_identity_keys_from_doc(doc: dict) -> set[str]:
    """The set of identity_keys (role-session-names) that the deny
    policy document ACTUALLY enforces a per-principal Deny against.
    Deny statements condition on aws:userid = "*:<identity_key>"; we
    return the "<identity_key>" tails. The QuotaDenyNoop placeholder
    (aws:userid = "*:none") and any non-per-user statement
    (DenyBlockedModels — no aws:userid) contribute nothing, so an
    attached-but-nobody-enforced policy yields an EMPTY set."""
    out: set[str] = set()
    for stmt in doc.get("Statement") or []:
        cond = stmt.get("Condition") or {}
        like = cond.get("StringLike") or {}
        ids = like.get("aws:userid") or []
        if isinstance(ids, str):
            ids = [ids]
        for v in ids:
            if ":" in v:
                tail = v.split(":", 1)[1]
                if tail and tail != "none":
                    out.add(tail)
    return out


def denied_identity_keys(iam, deny_cache: dict | None = None) -> set[str]:
    """Read tg-BedrockQuotaDeny's live default-version document and
    return the set of identity_keys it enforces a per-principal Deny
    against (empty if the policy is absent / only QuotaDenyNoop).
    Cached in `deny_cache` under a sentinel key so a fleet sweep does
    ONE GetPolicyVersion regardless of how many principals it checks.
    Raises ClientError on an IAM failure (caller maps to UNKNOWN)."""
    if deny_cache is not None and _DENIED_USERIDS_KEY in deny_cache:
        return deny_cache[_DENIED_USERIDS_KEY]
    # Construct the policy ARN deterministically (account-scoped managed
    # policy, fixed name) and read its default version document. We do
    # NOT list_policies to discover the ARN: iam:ListPolicies can't be
    # resource-scoped, so listing would force a Resource:"*" grant on the
    # task role. Constructing the ARN needs only the resource-scoped
    # iam:GetPolicy / iam:GetPolicyVersion the bedrock-role stack already
    # grants. A genuinely-absent policy surfaces as NoSuchEntity on
    # get_policy → an empty enforced set (no deny built yet), NOT UNKNOWN.
    arn = _deny_policy_arn(iam)
    keys: set[str] = set()
    try:
        pol = iam.get_policy(PolicyArn=arn)
        ver = (pol.get("Policy") or {}).get("DefaultVersionId")
        if ver:
            pv = iam.get_policy_version(PolicyArn=arn, VersionId=ver)
            doc = (pv.get("PolicyVersion") or {}).get("Document") or {}
            keys = _denied_identity_keys_from_doc(doc)
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code")
        if code not in _POLICY_ABSENT_CODES:
            raise  # AccessDenied / throttle / etc → caller maps to UNKNOWN
        # Policy not created yet → no principal is enforced. Benign.
    if deny_cache is not None:
        deny_cache[_DENIED_USERIDS_KEY] = keys
    return keys


def _deny_policy_arn(iam) -> str:
    """ARN of the customer-managed tg-BedrockQuotaDeny policy.

    Constructed deterministically as
    arn:aws:iam::<account>:policy/<POLICY_NAME> — the policy is an
    account-scoped managed policy with a fixed name, so the ARN is a
    pure function of the account id. This deliberately avoids
    iam:ListPolicies (which can't be resource-scoped → would need a
    Resource:"*" grant); the existing resource-scoped iam:GetPolicy*
    grants then suffice. The account comes from AWS_ACCOUNT_ID when set,
    else STS GetCallerIdentity (the same source deny_reconciler uses).
    Whether the policy actually EXISTS is discovered by the caller's
    get_policy (NoSuchEntity → absent). `iam` is accepted for signature
    stability / test injection; the account comes from STS, not it."""
    return f"arn:aws:iam::{_account_id()}:policy/{POLICY_NAME}"


def _account_id() -> str:
    """The AWS account id: AWS_ACCOUNT_ID env if set, else STS
    GetCallerIdentity (the same resolution deny_reconciler uses; the
    api/worker run on boto3's native cred chain). Read live from
    the environment (not the import-time constant) so a caller/test can
    pin it without a live STS round-trip."""
    acct = os.environ.get("AWS_ACCOUNT_ID") or ACCOUNT_ID
    if acct:
        return acct
    sts = boto3.client("sts", region_name=REGION)
    return sts.get_caller_identity()["Account"]


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

    # Reverse direction (DENY_NO_GOVERNED). Mere attachment of the
    # policy to the user's role is NOT enforcement: under the role-wide
    # deny model the deny attaches at the ROLE but enforces
    # PER-aws:userid, so an ungoverned principal merely sharing the
    # role is denied nothing and is EXPECTED, not drift. A user is
    # actually enforced iff the policy document contains a per-user
    # Deny naming THEIR aws:userid (identity_key). Flag drift only
    # then; a QuotaDenyNoop-only / other-principals-only policy is
    # UNMANAGED for this user. (This subsumes the old shared-role
    # guard — a co-tenant with no statement is no longer flagged.)
    if attached and not governed:
        identity_key = getattr(user, "identity_key", None)
        try:
            enforced = denied_identity_keys(iam, deny_cache)
        except ClientError as e:
            code = (e.response or {}).get("Error", {}).get("Code")
            log.warning(
                "verify: reading %s document failed (%s) — reporting "
                "UNKNOWN; is the iam:GetPolicy* grant deployed?",
                POLICY_NAME, code,
            )
            return UNKNOWN
        if identity_key and identity_key in enforced:
            return DRIFT      # real per-user Deny contradicts governed=false
        return UNMANAGED      # attachment-only / noop → not drift for this user

    return MANAGED if governed else UNMANAGED
