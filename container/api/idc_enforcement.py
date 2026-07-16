"""idc_enforcement — classify whether a *governed* IDC permission-set
user's deny is ACTUALLY enforced, using only reads tg can make in the
member account it runs in.

Why this exists: Govern on an IDC (`AWSReservedSSO_*`) user records the
deny *intent* (sets `governed=true`, emits the per-principal QuotaDeny,
and wires the tg-consumer trust). But the deny only *bites* once it
reaches a role the user actually assumes. That happens two ways:

  1. the IDC admin references the deny policy on the user's permission
     set, which IDC then PROVISIONS as an attached policy on the
     member-account `AWSReservedSSO_*` role (tg can SEE that attached
     policy — `iam:ListAttachedRolePolicies`), or
  2. the user assumes the tg-consumer chokepoint role, which carries
     the deny (tg can SEE that via `iam:ListEntitiesForPolicy` on the
     deny policy).

tg CANNOT read the permission-set *definition* in the IDC management
account (`sso:ListPermissionSets` is denied to a member-account
caller). But the *provisioned outcome* — the attached policy on the
member-account SSO role — IS readable, and that is what actually
governs enforcement. So the strongest truthful states tg can assert
are:

  ENFORCED_HERE          — the deny is attached to the user's own
                           `AWSReservedSSO_*` role (the permission-set
                           reference was provisioned).
  ENFORCED_VIA_CONSUMER  — the deny is attached to tg-consumer AND the
                           user's trust into tg-consumer is wired, so
                           assuming it is enforced.
  PENDING                — governed intent set, but neither of the
                           above holds. This is the honest default —
                           "not enforced in this account yet."
  UNKNOWN                — IAM was unreadable (missing grant / API
                           error) — can't confirm either way.

Read-only throughout: `iam:ListAttachedRolePolicies` (already granted,
Sid GovernanceDriftRead) + `iam:ListEntitiesForPolicy` on the deny
policy (already granted, Sid ListQuotaDenyAttachments). No mutating
call, no new IAM surface.

The IAM plumbing (client + policy-attachment reads) lives in the thin
route in routes/users.py; the pure classification is here so it's
unit-testable without AWS.
"""
from __future__ import annotations

import re

# Enforcement states for a governed IDC user (see module docstring).
ENFORCED_HERE = "enforced_here"
ENFORCED_VIA_CONSUMER = "enforced_via_consumer"
PENDING = "pending"
UNKNOWN = "unknown"

# The role NAME out of an `arn:...:role/[path/]<name>` ARN — the
# member-account role whose attached policies decide enforcement.
_ROLE_NAME_FROM_ARN_RE = re.compile(r":role/(?:.*/)?(?P<name>[^/]+)$")


def idc_role_name(principal_arn: str | None) -> str | None:
    """The `AWSReservedSSO_*` role NAME from an IDC principal ARN, or
    None if the ARN isn't an IDC permission-set role. Accepts both the
    path-form (aws-reserved/sso.amazonaws.com/AWSReservedSSO_...) and
    the collapsed role/AWSReservedSSO_... form."""
    if not principal_arn:
        return None
    m = _ROLE_NAME_FROM_ARN_RE.search(principal_arn.strip())
    if not m:
        return None
    name = m.group("name")
    return name if name.startswith("AWSReservedSSO_") else None


def classify(
    *,
    sso_role_attached: bool | None,
    consumer_attached: bool | None,
    consumer_trust_wired: bool,
) -> str:
    """Pure classification of a governed IDC user's enforcement state.

    Inputs (all derived from read-only IAM in the route):
      sso_role_attached    — is the deny attached to the user's OWN
                             `AWSReservedSSO_*` role? None = unreadable.
      consumer_attached    — is the deny attached to tg-consumer?
                             None = unreadable.
      consumer_trust_wired — is this user's SSO role trusted to assume
                             tg-consumer? (tg wired it on Govern.)

    Precedence: an attached policy on the user's OWN role is the
    strongest, most direct proof, so it wins. Then enforcement via the
    tg-consumer chokepoint (deny attached there AND the trust wired).
    Otherwise PENDING — unless IAM was unreadable for the deciding
    read, in which case UNKNOWN (never claim enforced on a failed
    read, never falsely claim pending when we simply couldn't look)."""
    if sso_role_attached:
        return ENFORCED_HERE
    if consumer_attached and consumer_trust_wired:
        return ENFORCED_VIA_CONSUMER
    # Neither enforced path confirmed. Distinguish "confirmed not
    # enforced" (PENDING) from "couldn't read IAM" (UNKNOWN): if the
    # reads that would have flipped us to enforced were unreadable,
    # we don't actually know.
    if sso_role_attached is None or consumer_attached is None:
        return UNKNOWN
    return PENDING


def is_enforced(state: str) -> bool:
    """True only for a tg-VERIFIED enforced state — the UI shows the
    success (green) badge only for these."""
    return state in (ENFORCED_HERE, ENFORCED_VIA_CONSUMER)
