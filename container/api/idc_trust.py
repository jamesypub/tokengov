"""idc_trust — wire an IDC developer's SSO role into tg-consumer's
trust policy when an admin Governs them (#1065).

This wires the CHAINED governance model, which is the **secondary**
(optional) path. The **primary** model is DIRECT: the dev's IDC
permission set carries `bedrock:InvokeModel` itself, the dev invokes
Bedrock AS their SSO role, and the reconciler attaches the deny to
that role — no role-chain, nothing in this module. The chained model
here is the fallback for locked-down IDC where the permission set
can't carry Bedrock (or would be wiped on re-provision).

The chained model: a dev assumes `tg-consumer`, then calls Bedrock;
the per-principal QuotaDeny tg emits lives on `tg-consumer` (tg-owned,
durable — an IDC re-provision can't wipe it, #618). For the deny to
evaluate for that dev, the dev's SSO role must be ALLOWED to
sts:AssumeRole tg-consumer. tg adds exactly that — who-may-assume, no
Bedrock grant.

An AWSReservedSSO_<permset>_<suffix> role's suffix changes on every IDC
re-provision, so the trust references it churn-safe: Principal=root +
an ArnLike condition on aws:PrincipalArn against the path-form
.../AWSReservedSSO_<permset>_* pattern (AWS rejects a bare wildcard in
Principal). One entry per PERMISSION SET — multiple governed users on
one permission set collapse to a single trust statement.

Pure functions here operate on a trust-policy dict (the role's
AssumeRolePolicyDocument); the IAM GetRole/UpdateAssumeRolePolicy calls
live in routes/users.py. No I/O in this module.
"""
from __future__ import annotations

import re

# Sid marker stamped on every tg-added IDC-govern trust statement, so
# add is idempotent and remove only ever touches tg's own statements
# (never the install-time IAM/SAML/SSO trust shaped by #1064).
GOVERN_SID_PREFIX = "TgGovernIdc"

_IDC_ROLE_PATH = "/aws-reserved/sso.amazonaws.com/"
# AWSReservedSSO_<permset>_<suffix>: <permset> may contain underscores,
# so anchor on the TRAILING _<suffix> (hex, IDC-assigned). The wildcard
# pattern replaces that suffix with _* so an IDC re-provision (new
# suffix) still matches.
_PERMSET_RE = re.compile(
    r"^(?P<permset>AWSReservedSSO_.+)_[0-9A-Fa-f]+$")
_ROLE_NAME_FROM_ARN_RE = re.compile(
    r":role/(?:aws-reserved/sso\.amazonaws\.com/)?(?P<name>[^/]+)$")
_ACCT_RE = re.compile(r"^arn:aws:iam::(?P<acct>\d+):")


def permset_arnlike(principal_arn: str | None) -> str | None:
    """Build the churn-safe ArnLike pattern for an IDC role ARN:

      arn:aws:iam::<acct>:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_<permset>_*

    Accepts the path-form ARN the classifier now stores (#1065) OR the
    older collapsed role/AWSReservedSSO_<permset>_<suffix> form, and
    normalizes both to the path form with a wildcard suffix. Returns
    None if the ARN isn't an IDC permission-set role (caller treats
    that as "nothing to wire")."""
    if not principal_arn:
        return None
    macct = _ACCT_RE.match(principal_arn)
    mname = _ROLE_NAME_FROM_ARN_RE.search(principal_arn)
    if not macct or not mname:
        return None
    name = mname.group("name")
    if not name.startswith("AWSReservedSSO_"):
        return None
    mp = _PERMSET_RE.match(name)
    # If the suffix is present, wildcard it; if the name already ends in
    # _* (or has no parseable suffix) keep the permset token as-is.
    permset = mp.group("permset") if mp else name.rstrip("_*")
    acct = macct.group("acct")
    return (
        f"arn:aws:iam::{acct}:role"
        f"{_IDC_ROLE_PATH}{permset}_*"
    )


def _statements(doc: dict) -> list:
    st = doc.get("Statement")
    if st is None:
        return []
    return st if isinstance(st, list) else [st]


def _matches_arnlike(stmt: dict, arnlike: str) -> bool:
    """True if a statement's ArnLike condition targets this pattern."""
    cond = stmt.get("Condition") or {}
    al = cond.get("ArnLike") or {}
    val = al.get("aws:PrincipalArn")
    if isinstance(val, list):
        return arnlike in val
    return val == arnlike


def has_trust(doc: dict, arnlike: str) -> bool:
    """True if the doc already trusts this permission-set pattern (via
    ANY statement — install-time or tg-govern)."""
    return any(_matches_arnlike(s, arnlike) for s in _statements(doc))


def _has_govern_trust(doc: dict, arnlike: str) -> bool:
    """True if a tg-OWNED (TgGovernIdc Sid) statement already trusts
    this pattern. add/remove key off this so tg manages exactly its own
    statement — independent of any install-time trust (#1064) that may
    coincidentally share the pattern."""
    for s in _statements(doc):
        sid = s.get("Sid", "")
        if (isinstance(sid, str) and sid.startswith(GOVERN_SID_PREFIX)
                and _matches_arnlike(s, arnlike)):
            return True
    return False


def add_trust(doc: dict, arnlike: str, account: str) -> tuple[dict, bool]:
    """Idempotently add a tg-owned Allow sts:AssumeRole statement gated
    by an ArnLike on aws:PrincipalArn == <arnlike>. Returns (new_doc,
    changed). tg's own statement already present → unchanged. Principal
    is the account root (AWS rejects a bare wildcard in Principal); the
    ArnLike scopes it to this one permission set. tg always manages its
    OWN statement (keyed on the TgGovernIdc Sid), so unmanage can later
    remove exactly that without touching an install-time trust."""
    if _has_govern_trust(doc, arnlike):
        return doc, False
    stmts = list(_statements(doc))
    # Stable Sid derived from the permset token so re-adds are no-ops
    # and removes can target it precisely.
    token = arnlike.rsplit("/", 1)[-1].rstrip("_*")
    sid = f"{GOVERN_SID_PREFIX}{re.sub(r'[^A-Za-z0-9]', '', token)}"
    stmts.append({
        "Sid": sid,
        "Effect": "Allow",
        "Principal": {"AWS": f"arn:aws:iam::{account}:root"},
        "Action": "sts:AssumeRole",
        "Condition": {"ArnLike": {"aws:PrincipalArn": arnlike}},
    })
    new = dict(doc)
    new["Version"] = doc.get("Version", "2012-10-17")
    new["Statement"] = stmts
    return new, True


def remove_trust(doc: dict, arnlike: str) -> tuple[dict, bool]:
    """Remove the tg-govern statement(s) trusting <arnlike>. Only drops
    statements carrying the GOVERN_SID_PREFIX Sid AND matching the
    pattern, so an install-time trust statement is never touched.
    Returns (new_doc, changed)."""
    kept = []
    changed = False
    for s in _statements(doc):
        sid = s.get("Sid", "")
        if (isinstance(sid, str) and sid.startswith(GOVERN_SID_PREFIX)
                and _matches_arnlike(s, arnlike)):
            changed = True
            continue
        kept.append(s)
    if not changed:
        return doc, False
    new = dict(doc)
    new["Version"] = doc.get("Version", "2012-10-17")
    new["Statement"] = kept
    return new, True
