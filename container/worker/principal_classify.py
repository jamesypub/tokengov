"""
principal_classify — shared Bedrock principal-ARN classification.

Extracted from metrics_aggregator (#723, slice 1/5 of #720) so the
CW-Logs aggregator AND the forthcoming CUR job classify identical
ARNs through ONE implementation — machine roles are the dominant
spender, so attribution drift between two copies would be
expensive. Pure functions, no I/O; moved verbatim from
metrics_aggregator (the leading underscore dropped — now a shared
public API).

Peer parser: container/api/routes/users.py:_role_name_from_arn
extracts just the role NAME from an already-classified role ARN;
it's a narrower helper and stays put.
"""
from __future__ import annotations
import re

_EMAIL_RE = re.compile(r"[^/\s]+@[^/\s]+")
_ASSUMED_ROLE_RE = re.compile(
    r"^arn:aws:sts::(?P<acct>\d+):assumed-role/"
    r"(?P<role>[^/]+)/(?P<session>.+)$"
)
_IAM_USER_RE = re.compile(
    r"^arn:aws:iam::(?P<acct>\d+):user/(?P<user>.+)$"
)
_FEDERATED_RE = re.compile(
    r"^arn:aws:sts::(?P<acct>\d+):federated-user/(?P<user>.+)$"
)
_ROOT_RE = re.compile(r"^arn:aws:iam::(?P<acct>\d+):root$")

_IDC_ROLE_PATH = "/aws-reserved/sso.amazonaws.com/"
_IDC_ROLE_NAME_RE = re.compile(r"(^|/)AWSReservedSSO_")


def classify_principal(arn: str) -> tuple[
    str | None, str | None, str | None, str | None
]:
    """Parse a Bedrock invocation `identity.arn` into the
    (identity_key, email, principal_type, principal_arn)
    tuple that v1.0 (#345) records on the User row.

    Returns (None, None, None, None) when the ARN is empty
    or unparseable. The caller still records the row with
    principal_type='unknown'.
    """
    if not arn:
        return None, None, None, None

    if _ROOT_RE.match(arn):
        m = _ROOT_RE.match(arn)
        return (
            f"root:{m.group('acct')}",
            None,
            "root",
            arn,
        )

    m = _ASSUMED_ROLE_RE.match(arn)
    if m:
        acct = m.group("acct")
        role = m.group("role")
        session = m.group("session")
        # #1065: for an IDC permission-set role the assumed-role ARN
        # collapses the path — the role capture is either the bare
        # `AWSReservedSSO_<permset>_<suffix>` or, when AWS emits the
        # full path, `aws-reserved/sso.amazonaws.com/AWSReservedSSO_…`
        # (then `role` is `aws-reserved` and the SSO segment rides in
        # `session`). Rebuild the VALID path-form role ARN so
        # principal_arn can be written into tg-consumer's trust policy
        # verbatim (#1065 Govern) — the bare `role/AWSReservedSSO_…`
        # form is an Invalid principal, the same defect #1064 fixed in
        # the installer.
        if role.startswith("AWSReservedSSO_"):
            role_arn = (
                f"arn:aws:iam::{acct}:role"
                f"{_IDC_ROLE_PATH}{role}"
            )
        elif role == "aws-reserved" and "AWSReservedSSO_" in session:
            # path-form assumed-role ARN: session ==
            # sso.amazonaws.com/AWSReservedSSO_<permset>_<suffix>/<sess>
            sso_role = session.rsplit("/", 1)[0].rsplit("/", 1)[-1]
            role_arn = (
                f"arn:aws:iam::{acct}:role"
                f"{_IDC_ROLE_PATH}{sso_role}"
            )
        else:
            role_arn = f"arn:aws:iam::{acct}:role/{role}"
        # Service-linked roles ride under the
        # aws-service-role/ path. The "role" capture
        # for those is `aws-service-role` and the
        # session piece carries the actual service +
        # role name; rebuild a clean identity_key.
        if role == "aws-service-role":
            slr_path, _, slr_session = session.partition("/")
            return (
                f"slr:{slr_path}",
                None,
                "service_linked",
                arn,
            )
        # #810: key on the role-session-name (the last
        # `/`-segment of the assumed-role ARN) VERBATIM — no
        # email normalization, no `+tag` stripping, no
        # lowercasing. The spend identity_key and the deny
        # `aws:userid` condition are the SAME string (the session
        # name AWS emits in `aws:userid`'s `<RoleId>:<SessionName>`
        # half), so they MUST NOT diverge — a display-only
        # normalization would silently break caps.
        #   - email-shaped session → person: `user+ops@example.com`
        #     is its own distinct identity (NOT collapsed to a base
        #     email).
        #   - non-email session → machine: keyed on the session
        #     name itself (NOT `role:<RoleName>` — the #627 role
        #     collapse is dropped, owner decision; an instance-id
        #     session becomes its own key, fragmenting per
        #     instance, accepted eyes-open).
        # principal_arn still carries the rebuilt role ARN so
        # Manage/deny-attach can resolve the attachable role for
        # display — only the KEY changes.
        seg = session.rsplit("/", 1)[-1]
        if _EMAIL_RE.fullmatch(seg):
            return (seg, seg, "assumed_role", role_arn)
        return (seg, None, "service", role_arn)

    m = _IAM_USER_RE.match(arn)
    if m:
        user = m.group("user")
        em = _EMAIL_RE.search(user)
        return (
            em.group(0).lower() if em else user,
            em.group(0).lower() if em else None,
            "iam_user",
            arn,
        )

    m = _FEDERATED_RE.match(arn)
    if m:
        user = m.group("user")
        em = _EMAIL_RE.search(user)
        return (
            em.group(0).lower() if em else user,
            em.group(0).lower() if em else None,
            "federated",
            arn,
        )

    return f"unknown:{arn}", None, "unknown", arn


def resolve_principal(arn: str, key_map: dict | None = None) -> tuple[
    str | None, str | None, str | None, str | None
]:
    """classify_principal(), then apply the admin-maintained
    email↔Bedrock-key mapping so a long-term Bedrock API key's spend
    attributes to the mapped developer.

    `key_map` is `{iam_user_name -> (identity_key, email)}` built from
    `users.bedrock_key_user` (the non-secret IAM-user NAME an admin
    recorded). A long-term key is backed by an IAM user, so CUR bills
    its `user/<name>` principal — classify_principal() returns that as
    an `iam_user` keyed on the raw name (no email). When that name is
    in the map, RE-ATTRIBUTE to the owner: return the owner's
    (identity_key, email) with principal_type='iam_user' and the
    ORIGINAL principal_arn preserved (the ARN still points at the key's
    IAM user — only the identity the spend lands on changes).

    THE MATCHING RULE (a near-miss is a bug): rewrite ONLY an
    `iam_user` principal whose name literally appears in `key_map`. The
    map IS the discriminator — never a "not an email → treat as a key"
    heuristic (service roles, `AWSReservedSSO_*` sessions, machine
    sessions are neither emails nor keys and must be left untouched).
    An unmapped key-user keeps classify_principal()'s raw `iam_user`
    result unchanged (no regression). Pure — the map is passed in, so
    both resolution layers (sync + report display) call this one helper
    and can't diverge."""
    identity_key, email, ptype, principal_arn = classify_principal(arn)
    if not key_map or ptype != "iam_user":
        return identity_key, email, ptype, principal_arn
    # identity_key for an iam_user is the raw IAM-user name (or the
    # embedded email, lowercased) — the map is keyed on the raw name
    # exactly as stored in users.bedrock_key_user. Match on the raw
    # name captured from the ARN, not a normalized form.
    m = _IAM_USER_RE.match(arn)
    name = m.group("user") if m else identity_key
    mapped = key_map.get(name)
    if not mapped:
        return identity_key, email, ptype, principal_arn
    mapped_key, mapped_email = mapped
    return mapped_key, mapped_email, "iam_user", principal_arn


def classify_role_type(arn: str) -> str:
    """#625: classify a principal's role as IDC permission-set
    (`AWSReservedSSO_*`, path `/aws-reserved/sso.amazonaws.com/`)
    vs a normal IAM role. Returns "idc" or "iam".

    IDC roles are surfaced but not directly manageable in v1.1 —
    a deny attached to them is wiped on the next IDC
    re-provision (#618). The classifier keys off the invocation
    `identity.arn` (an assumed-role STS ARN), where the IDC path
    is collapsed into the role-name segment as `AWSReservedSSO_*`.
    Everything else — machine roles, tg-consumer, IAM users,
    federated, root, unknown — is "iam"."""
    if not arn:
        return "iam"
    if _IDC_ROLE_PATH in arn:
        return "idc"
    m = _ASSUMED_ROLE_RE.match(arn)
    if m and _IDC_ROLE_NAME_RE.search(m.group("role")):
        return "idc"
    return "iam"
