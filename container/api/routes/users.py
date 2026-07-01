from __future__ import annotations
import os
import re
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy.orm import Session
from sqlalchemy import func

from db.session import get_db
from db.models import (
    User, TeamMembership, CurUserSpend, ModelPricing, AdminRole,
    PrincipalModel,
)
from db.org_config import (
    get_org_default_quota_usd, get_spend_estimate_strategy,
    get_spend_estimate_enforcement,
)
from db.spend_estimate import project_for_principal
from db.usage_windows import month_start_utc
from api.auth import get_caller_email, Scope


def _spend_projection(db, email, billed_mtd, strategy=None, cap=None):
    """The per-principal billed-vs-estimated split for a user row.
    Returns {billed, estimated, projected, unbilled_hours,
    estimate_low_sample, projected_over_cap}. `projected_over_cap` is
    the warn-mode signal: billed alone is under cap but billed +
    estimated >= cap (the ESTIMATE is what crosses; an already-billed-
    over user is a normal billed-over case). Best-effort — a lookup
    failure degrades to no estimate (billed-only) rather than failing
    the user list."""
    try:
        strat = strategy or get_spend_estimate_strategy(db)
        proj = project_for_principal(
            db, email, billed_mtd=billed_mtd, strategy=strat)
        over = bool(
            cap is not None and cap > 0
            and proj["billed"] < cap and proj["projected"] >= cap)
        return {
            "billed":              proj["billed"],
            "estimated":           proj["estimated"],
            "projected":           proj["projected"],
            "unbilled_hours":      proj["unbilled_hours"],
            "estimate_low_sample": proj["low_sample"],
            "projected_over_cap":  over,
        }
    except Exception:  # noqa: BLE001 — estimate is non-fatal decoration
        return {}

router = APIRouter()


# #345: roles considered "managed" for the unmanaged-users
# surface. Comma-separated env so two-env installs can list
# both a dev and stage role. Default matches the post-#349
# rename. Read once at import — admins change this via CFN
# parameter on tg-container-stack, not at runtime.
_MANAGED_ROLE_NAMES = {
    n.strip()
    for n in os.environ.get(
        "TG_MANAGED_ROLE_NAMES", "tg-consumer"
    ).split(",")
    if n.strip()
}
_ROLE_FROM_ARN_RE = re.compile(
    r"^arn:aws:iam::\d+:role/(?P<role>.+)$"
)


def _is_managed(u: User) -> bool:
    """A user is 'managed' when they reach Bedrock through a
    role TG controls — today the assume-role chokepoint
    `tg-consumer` (and federated users that land in
    that same role)."""
    if u.principal_type not in ("assumed_role", "federated"):
        return False
    if not u.principal_arn:
        return False
    m = _ROLE_FROM_ARN_RE.match(u.principal_arn)
    if not m:
        return False
    return m.group("role") in _MANAGED_ROLE_NAMES


# #627: manage/unmanage attaches/detaches tg-BedrockQuotaDeny to
# a discovered principal's role. The policy name + account come
# from env (same contract as the reconciler).
_DENY_POLICY_NAME = os.environ.get(
    "DENY_POLICY_NAME", "tg-BedrockQuotaDeny")


def _role_name_from_arn(principal_arn: str | None) -> str | None:
    """Extract the IAM role NAME from a principal's role ARN
    (arn:aws:iam::<acct>:role/<name>). Returns None if the ARN
    is absent or not a role ARN."""
    if not principal_arn:
        return None
    m = _ROLE_FROM_ARN_RE.match(principal_arn)
    return m.group("role") if m else None


def _iam_client():
    """Testable IAM client factory — patched in tests. Uses the
    container's task-role credential chain (same as analytics)."""
    from api.aws_session import get_aws_session
    return get_aws_session().client("iam")


# #809 (reverses #799/#804): the AdministratorAccess refusal is gone.
# Under the #746 DENYLIST (fail-open) tg-BedrockQuotaDeny blocks only
# the org-blocked models + the named identities, so attaching it
# role-wide to ANY role — admin included — is correct, not a freeze.
# manage/force_block keep ONLY the genuinely-unenforceable guard:
# a principal with no attachable IAM role ARN (#707).


def _deny_policy_arn(iam) -> str:
    """Resolve the tg-BedrockQuotaDeny managed-policy ARN from
    the caller's account (STS GetCallerIdentity), or the
    AWS_ACCOUNT_ID env if set."""
    account = os.environ.get("AWS_ACCOUNT_ID", "")
    if not account:
        sts = _sts_client()
        account = sts.get_caller_identity()["Account"]
    return f"arn:aws:iam::{account}:policy/{_DENY_POLICY_NAME}"


def _sts_client():
    from api.aws_session import get_aws_session
    return get_aws_session().client("sts")


# #1065: the tg-owned chokepoint role whose trust policy gates who may
# assume it. Same env contract as the reconciler (#349); default
# tg-consumer.
def _consumer_role_name() -> str:
    return (
        os.environ.get("TG_TOKEN_CONSUMER_ROLE_NAME")
        or os.environ.get("TG_BEDROCK_ROLE_NAME")
        or "tg-consumer"
    )


def _account_id() -> str:
    """Deployment account — env first, else STS."""
    acct = os.environ.get("AWS_ACCOUNT_ID", "")
    if acct:
        return acct
    return _sts_client().get_caller_identity()["Account"]


def _add_idc_consumer_trust(principal_arn: str | None) -> None:
    """#1065: add the dev's SSO permission-set role to tg-consumer's
    AssumeRolePolicyDocument (path-form, ArnLike wildcard on the
    suffix). Idempotent. Raises HTTPException(502) on any IAM failure so
    Govern doesn't falsely report success without the trust."""
    from api import idc_trust
    arnlike = idc_trust.permset_arnlike(principal_arn)
    if not arnlike:
        # Not a recognizable IDC permission-set role (e.g. CUR hasn't
        # populated principal_arn yet). Nothing to wire — the deny is
        # still emitted; the trust lands on a later Govern once CUR has
        # the role. Don't fail the Govern.
        return
    role = _consumer_role_name()
    iam = _iam_client()
    from botocore.exceptions import ClientError
    try:
        cur = iam.get_role(RoleName=role)
        doc = (cur.get("Role") or {}).get(
            "AssumeRolePolicyDocument") or {}
        new_doc, changed = idc_trust.add_trust(
            doc, arnlike, _account_id())
        if changed:
            import json
            iam.update_assume_role_policy(
                RoleName=role,
                PolicyDocument=json.dumps(new_doc))
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code")
        raise HTTPException(
            502,
            f"could not add the IDC trust to {role} ({code}): {e}. "
            "Govern did NOT complete — the deny would be advisory "
            "without this trust.",
        )


def _remove_idc_consumer_trust_if_unused(db: Session, u: User) -> None:
    """#1065: drop this permission set's trust from tg-consumer iff no
    OTHER governed IDC user maps to the same permission set. Best-effort
    on the IAM side is NOT acceptable for correctness, but a missing
    role / already-removed statement is success. Never strips a trust a
    still-governed user needs."""
    from api import idc_trust
    arnlike = idc_trust.permset_arnlike(u.principal_arn)
    if not arnlike:
        return
    # Any OTHER still-governed IDC user whose principal_arn maps to the
    # SAME permission-set pattern keeps the trust.
    others = (
        db.query(User)
        .filter(
            User.governed.is_(True),
            User.role_type == "idc",
            User.email != u.email,
        )
        .all()
    )
    for o in others:
        if idc_trust.permset_arnlike(o.principal_arn) == arnlike:
            return  # a still-governed peer needs this trust
    role = _consumer_role_name()
    iam = _iam_client()
    from botocore.exceptions import ClientError
    try:
        cur = iam.get_role(RoleName=role)
        doc = (cur.get("Role") or {}).get(
            "AssumeRolePolicyDocument") or {}
        new_doc, changed = idc_trust.remove_trust(doc, arnlike)
        if changed:
            import json
            iam.update_assume_role_policy(
                RoleName=role,
                PolicyDocument=json.dumps(new_doc))
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code")
        if code not in ("NoSuchEntity",):
            raise HTTPException(
                502,
                f"could not remove the IDC trust from {role} "
                f"({code}): {e}",
            )


def _db():
    with get_db() as db:
        yield db


def _scope(
    request: Request,
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


def _user_dict(
    u: User,
    spend: float = 0.0,
    roles: list | None = None,
    db: Session | None = None,
    org_default_cap: float | None = None,
    models: list | None = None,
    estimate_strategy: str | None = None,
) -> dict:
    if org_default_cap is None:
        org_default_cap = (
            get_org_default_quota_usd(db) if db is not None else 0.0
        )
    effective = (
        u.cap_usd if u.cap_usd is not None else org_default_cap
    )
    # cap_source distinguishes the three states the UserDetail
    # Cap card needs to render correctly (#277):
    #   - user_override: u.cap_usd is set (explicit per-user policy)
    #   - org_default:   u.cap_usd is null AND the org default > 0
    #   - none:          u.cap_usd is null AND no org default set
    if u.cap_usd is not None:
        cap_source = "user_override"
    elif org_default_cap and org_default_cap > 0:
        cap_source = "org_default"
    else:
        cap_source = "none"
    # pct_used is null when cap_usd is null OR cap is 0 — the
    # UserDetail "Used" card renders "—" in those cases instead
    # of displaying a misleading 0% or division-by-zero.
    pct_used = None
    if u.cap_usd is not None and u.cap_usd > 0:
        pct_used = round((float(spend) / float(u.cap_usd)) * 100, 1)
    # #345: principal-shape for the unmanaged-users surface.
    # `email` is null on machine principals (their identity is
    # the role, not a person); `identity_key` is the synthetic
    # primary id the UI uses for routing.
    is_service = u.principal_type in ("service", "service_linked")
    api_email = None if is_service else u.email
    return {
        "email":             api_email,
        "identity_key":      u.identity_key or u.email,
        "principal_arn":     u.principal_arn,
        "principal_type":    u.principal_type,
        "managed":           _is_managed(u),
        # #625 deny-only governance foundation:
        #   governed   — persisted deny-attached flag (set by
        #                child C; discovery never sets it).
        #   role_type  — idc | iam (idc = AWSReservedSSO_*, not
        #                directly manageable). Defaults to "iam"
        #                for legacy rows discovery hasn't re-seen.
        #   display_name — admin-set label; null until edited.
        #   models     — per-principal observed model ids.
        "governed":          bool(u.governed),
        "role_type":         u.role_type or "iam",
        "display_name":      u.display_name,
        "models":            models if models is not None else [],
        "is_service":        is_service,
        "status":            u.status,
        "cap_usd":           u.cap_usd,
        "cap_source":        cap_source,
        "pct_used":          pct_used,
        "effective_quota_usd": float(effective),
        "team_id":           u.team_id,
        "first_seen_at":     u.first_seen_at.isoformat() if u.first_seen_at else None,
        "last_seen_at":      u.last_seen_at.isoformat() if u.last_seen_at else None,
        # #750: force_blocked_at replaces disabled_at; the time-boxed
        # unblock_expires_at reprieve was removed.
        "force_blocked_at":  u.force_blocked_at.isoformat() if u.force_blocked_at else None,
        # When this user's record was last written (govern/block/cap/team
        # etc.). The apply-status UI compares it to the last deny_reconciler
        # run to show pending vs enforced — govern/block/unblock take effect
        # via the reconciler (~5-min tick), not instantly, so the admin can
        # see whether a just-made change is live yet (reload-durable; not a
        # client-only "just acted").
        "governance_updated_at": u.updated_at.isoformat() if u.updated_at else None,
        "version":           u.version,
        "mtd_spend_usd":     round(spend, 4),
        # Spend projection: billed MTD (== mtd_spend_usd) plus an
        # estimated-unbilled badge so the UI shows near-real-time burn
        # during the CUR lag without blending it into the billed figure.
        # Only for principals with an email key and a live db; the
        # estimate fills the lag from the user's own recent billed rate.
        **(_spend_projection(db, u.email, spend, estimate_strategy,
                              cap=effective)
           if (db is not None and u.email) else {}),
        "roles":             roles if roles is not None else [],
        # #927: login_enabled = has ANY authz row (admin OR member).
        # The Users-screen "Enable login" action shows only when this
        # is false (and the principal is a tg-manageable human).
        "login_enabled":     bool(roles),
    }


def _roles_by_email(db: Session, emails: list[str]) -> dict[str, list[dict]]:
    if not emails:
        return {}
    rows = (
        db.query(AdminRole)
        .filter(AdminRole.email.in_(emails))
        .order_by(AdminRole.role, AdminRole.team_id)
        .all()
    )
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r.email, []).append(
            {"role": r.role, "team_id": r.team_id}
        )
    return out


def _models_by_identity(
    db: Session, identity_keys: list[str]
) -> dict[str, list[str]]:
    """#625: per-principal observed model ids, keyed by
    identity_key (principal_models' key). Sorted for a stable
    UI ordering. One grouped query — no N+1 in list_users."""
    if not identity_keys:
        return {}
    rows = (
        db.query(
            PrincipalModel.identity_key,
            PrincipalModel.model_id,
        )
        .filter(PrincipalModel.identity_key.in_(identity_keys))
        .order_by(PrincipalModel.model_id)
        .all()
    )
    out: dict[str, list[str]] = {}
    for ident, model_id in rows:
        out.setdefault(ident, []).append(model_id)
    return out


def _user_spend(db: Session, email: str) -> float:
    # #643: MTD spend = SUM over this month's per-day rows.
    row = (
        db.query(func.sum(CurUserSpend.spend_usd))
        .filter(
            CurUserSpend.email == email,
            CurUserSpend.usage_hour >= month_start_utc(),
        )
        .scalar()
    )
    return float(row or 0)


def _spend_by_email(db: Session, emails: list[str]) -> dict[str, float]:
    """Current-month (MTD) spend for many users in one grouped
    query (avoids N+1 in list_users). #643: sums this month's
    per-day rows; mirrors `_user_spend`'s filter."""
    if not emails:
        return {}
    rows = (
        db.query(CurUserSpend.email, func.sum(CurUserSpend.spend_usd))
        .filter(
            CurUserSpend.email.in_(emails),
            CurUserSpend.usage_hour >= month_start_utc(),
        )
        .group_by(CurUserSpend.email)
        .all()
    )
    return {email: float(total or 0) for email, total in rows}


@router.get("/users")
def list_users(
    team: Optional[str] = None,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    q = db.query(User)
    if not scope.is_org_admin:
        if scope.admin_team_ids:
            q = q.filter(User.team_id.in_(scope.admin_team_ids))
        else:
            raise HTTPException(403, "Insufficient permissions")
    if team and team != "*":
        q = q.filter(User.team_id == team)
    users = q.order_by(User.email).all()
    emails = [u.email for u in users]
    roles = _roles_by_email(db, emails)
    spend = _spend_by_email(db, emails)
    identity_keys = [u.identity_key or u.email for u in users]
    models = _models_by_identity(db, identity_keys)
    org_default_cap = get_org_default_quota_usd(db)
    # Resolve the estimator strategy once for the whole list (avoids a
    # per-row config lookup); each row's projection is the cheap
    # rate×unbilled compute over its own billed history.
    estimate_strategy = get_spend_estimate_strategy(db)
    # Surface the enforcement mode so the UI gates the warn badge
    # (projected_over_cap is shown only under 'warn').
    estimate_enforcement = get_spend_estimate_enforcement(db)
    return {
        "estimate_enforcement": estimate_enforcement,
        "users": [
            _user_dict(
                u,
                spend=spend.get(u.email, 0.0),
                roles=roles.get(u.email, []),
                db=db,
                org_default_cap=org_default_cap,
                models=models.get(u.identity_key or u.email, []),
                estimate_strategy=estimate_strategy,
            )
            for u in users
        ],
    }


@router.get("/users/{email}")
def get_user(
    email: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    # #345: route param now accepts either an email (legacy
    # callers) or an identity_key like `role:MyEcsTaskRole`
    # (service rows). Lookup tries identity_key first since
    # it's the new primary; falls back to the email PK so
    # pre-#345 SPA bundles keep working.
    u = (
        db.query(User)
        .filter(User.identity_key == email)
        .first()
    )
    if not u:
        u = db.query(User).filter(User.email == email).first()
    if not u:
        raise HTTPException(404, f"User {email} not found")
    # #929: a member may read their OWN row (member self-service view) —
    # org_admin reads any, team_admin reads their subtree, the user
    # themselves reads self. Anyone else → 403. (Was admin-only, which
    # 403'd a member on their own data, #929 OQ1.)
    if (not scope.is_org_admin
            and u.team_id not in scope.admin_team_ids
            and not scope.is_self(u)):
        raise HTTPException(403, "Insufficient permissions")
    spend = _user_spend(db, email)
    roles = _roles_by_email(db, [email]).get(email, [])
    ident = u.identity_key or u.email
    models = _models_by_identity(db, [ident]).get(ident, [])
    return _user_dict(u, spend, roles, db=db, models=models)


@router.patch("/users/{email}")
def patch_user(
    email: str,
    body: dict,
    if_match: Optional[str] = Header(None),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """#625: set or clear a principal's admin-set
    `display_name` — a friendly label distinct from the
    ARN-derived caller. This is the ONLY field this endpoint
    touches; the caller (email / identity_key / principal_arn)
    stays read-only. Pass `{"display_name": "Acme batch job"}`
    to set, `{"display_name": null}` (or "") to clear.

    The path param accepts either an email (humans) or an
    identity_key like `role:MyEcsTaskRole` (service rows),
    mirroring GET /users/{email}. The display-name *edit UI*
    is child E; this ticket ships the column + API only."""
    # identity_key first (new primary), email PK fallback —
    # same resolution order as get_user.
    u = (
        db.query(User)
        .filter(User.identity_key == email)
        .first()
    )
    if not u:
        u = db.query(User).filter(User.email == email).first()
    if not u:
        raise HTTPException(404, f"User {email} not found")
    # #650: display_name is self-service — the user themselves,
    # OR an admin per the 3-tier rule (org_admin / team_admin of
    # the user's team). (Was org_admin-only; the prior team check
    # below require_org_admin() was dead code.)
    scope.require_self_or_team_admin_for(u)
    _check_version(u, if_match)
    # display_name is the only mutable field. Reject any other
    # key so callers can't smuggle a caller/principal edit
    # through this endpoint.
    allowed = {"display_name"}
    extra = set(body.keys()) - allowed
    if extra:
        raise HTTPException(
            400,
            f"PATCH /users only updates display_name; "
            f"unexpected field(s): {sorted(extra)}",
        )
    if "display_name" not in body:
        raise HTTPException(400, "display_name field required")
    name = body.get("display_name")
    if name is not None:
        if not isinstance(name, str):
            raise HTTPException(
                400, "display_name must be a string or null")
        name = name.strip() or None
    u.display_name = name
    u.version = (u.version or 1) + 1
    db.flush()
    ident = u.identity_key or u.email
    models = _models_by_identity(db, [ident]).get(ident, [])
    spend = _user_spend(db, ident)
    return _user_dict(u, spend, db=db, models=models)


def _resolve_user_or_404(db: Session, ident: str) -> User:
    """Resolve by identity_key first (new primary), email PK
    fallback — same order as get_user. Raises 404."""
    u = (
        db.query(User)
        .filter(User.identity_key == ident)
        .first()
    )
    if not u:
        u = db.query(User).filter(User.email == ident).first()
    if not u:
        raise HTTPException(404, f"User {ident} not found")
    return u


_IDC_ROLE_RE = re.compile(r":role/(?:.*/)?AWSReservedSSO_")


@router.post("/users/{email}/principal-arn")
def set_principal_arn(
    email: str,
    body: dict,
    if_match: Optional[str] = Header(None),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """#946: record an admin-supplied IAM role ARN on a
    pre-registered / ARN-less principal, so Govern is attachable
    WITHOUT waiting for Bedrock spend (the blocker was only that
    pre-registration left principal_arn null until CUR observed the
    user). Admin-only (org / team-admin of the user's team — same
    scope as govern), distinct from the self-service display_name
    PATCH on /users/{email}.

    Validates the value is a role ARN (`arn:aws:iam::<acct>:role/
    <name>` — reuses _role_name_from_arn), rejects IDC permission-set
    roles (AWSReservedSSO_* are never tg-governable; the deny is wiped
    on re-provision), and rejects a cross-account ARN (tg only
    attaches in the deployment account). On success sets principal_arn
    + derives principal_type='assumed_role' / role_type='iam'. The
    govern (manage) endpoint already attaches given a role ARN — no
    change there. Admin-recorded intent is treated identically to a
    CUR-observed ARN; CUR observation later wins if it differs (it's
    ground truth)."""
    u = _resolve_user_or_404(db, email)
    scope.require_team_admin_for(u)  # org or team admin — like govern
    _check_version(u, if_match)

    arn = (body.get("principal_arn") or "").strip()
    if not arn:
        raise HTTPException(400, "principal_arn required")
    role_name = _role_name_from_arn(arn)
    if not role_name:
        raise HTTPException(
            400,
            "principal_arn must be an IAM role ARN "
            "(arn:aws:iam::<acct>:role/<name>) — not an IAM user or "
            "root.",
        )
    if _IDC_ROLE_RE.search(arn):
        raise HTTPException(
            409,
            "IDC permission-set roles (AWSReservedSSO_*) are not "
            "governable by tg — govern via the permission set policy "
            "or an SCP instead.",
        )
    # Cross-account guard: tg can only AttachRolePolicy in its own
    # account, so reject an ARN from a different account up-front
    # (the attach is deny-only, but a wrong-account ARN just 502s
    # later — fail clearly here). Resolve the deployment account the
    # same way _deny_policy_arn does (env, else STS).
    m = re.match(r"^arn:aws:iam::(\d+):", arn)
    entered_acct = m.group(1) if m else ""
    deploy_acct = os.environ.get("AWS_ACCOUNT_ID", "")
    if not deploy_acct:
        try:
            deploy_acct = _sts_client().get_caller_identity()["Account"]
        except Exception:
            deploy_acct = ""  # can't resolve → skip the guard
    if deploy_acct and entered_acct and entered_acct != deploy_acct:
        raise HTTPException(
            400,
            f"principal_arn account ({entered_acct}) differs from the "
            f"deployment account ({deploy_acct}); tg can only attach "
            "the deny in its own account.",
        )

    u.principal_arn = arn
    # Admin-recorded a role ARN → treat as an assumed-role IAM
    # principal (the govern path only needs the role ARN; this keeps
    # the row's type/role_type coherent for the UI + reconciler).
    u.principal_type = "assumed_role"
    u.role_type = "iam"
    u.version = (u.version or 1) + 1
    db.flush()
    ident = u.identity_key or u.email
    models = _models_by_identity(db, [ident]).get(ident, [])
    spend = _user_spend(db, ident)
    return _user_dict(u, spend, db=db, models=models)


@router.post("/users/{email}/manage")
def manage_principal(
    email: str,
    body: dict = None,
    if_match: Optional[str] = Header(None),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """#627: enroll a discovered principal in deny-only
    governance. One-time `AttachRolePolicy` of
    tg-BedrockQuotaDeny to the principal's role, then mark the
    row `governed=true` so the reconciler maintains its quota
    statement.

    **IDC path (#1011):** for an `AWSReservedSSO_*` principal
    (`role_type=idc`) tg does NOT call `AttachRolePolicy` — a deny
    attached directly to an IDC-provisioned role is wiped on the
    next IDC re-provision (#618). Instead it just sets
    `governed=true` and lets the reconciler emit the per-principal
    `QuotaDeny` (`aws:userid *:<identity_key>`). The deny bites once
    the policy reaches a role the user actually uses — either they
    assume `tg-consumer` (tg attaches that itself, #809) OR the IDC
    admin referenced `tg-BedrockQuotaDeny` on the user's permission
    set (the #1010 `tg-QuotaDenyPermissionSet`, an attachment IDC
    owns so it survives re-provision). Govern is therefore advisory
    for the permission-set path — the UI states the precondition; it
    never implies hard enforcement. The attach (non-IDC) is
    **deny-only** — tg holds no grant power; worst case it
    over-restricts."""
    u = _resolve_user_or_404(db, email)
    scope.require_team_admin_for(u)  # #650: org or team admin
    _check_version(u, if_match)

    # #1011: IDC-aware govern — DO NOT attach the deny directly to an
    # AWSReservedSSO_* role (it would be wiped on re-provision, #618).
    # Set governed and let the reconciler emit the per-principal
    # QuotaDeny; enforcement lands via tg-consumer or the #1010
    # permission-set reference.
    if (u.role_type or "iam") == "idc":
        # #1065: wire the dev's SSO role into tg-consumer's trust so the
        # per-principal QuotaDeny (which lives on tg-consumer, the
        # durable chokepoint) actually evaluates for them — the deny
        # can't sit on the IDC role itself (wiped on re-provision,
        # #618/#1011). Adds who-may-assume, NO Bedrock grant. Path-form
        # + ArnLike wildcard on the permission-set suffix so an IDC
        # re-provision doesn't drop the trust; one entry per permission
        # set. Surface a clear error if the trust write fails — Govern
        # must not silently report success without enforcement (the
        # advisory trap this closes).
        _add_idc_consumer_trust(u.principal_arn)
        u.governed = True
        u.version = (u.version or 1) + 1
        db.flush()
        ident = u.identity_key or u.email
        models = _models_by_identity(db, [ident]).get(ident, [])
        spend = _user_spend(db, ident)
        return _user_dict(u, spend, db=db, models=models)

    role_name = _role_name_from_arn(u.principal_arn)
    if not role_name:
        raise HTTPException(
            400,
            "principal has no IAM role to attach the deny policy "
            f"to (principal_type={u.principal_type}, "
            f"arn={u.principal_arn})",
        )

    # One-time AttachRolePolicy (idempotent — already-attached
    # is success). Scoped to tg-BedrockQuotaDeny only.
    # #809: no admin-role refusal — denylist semantics make a
    # role-wide attach safe on ANY role (the reconciler also
    # maintains this attachment + the per-role attach set).
    iam = _iam_client()
    policy_arn = _deny_policy_arn(iam)
    from botocore.exceptions import ClientError
    try:
        iam.attach_role_policy(
            RoleName=role_name, PolicyArn=policy_arn)
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code")
        # Already attached → treat as success (idempotent manage).
        if code not in ("EntityAlreadyExists",):
            raise HTTPException(
                502,
                f"AttachRolePolicy failed ({code}) for role "
                f"{role_name}: {e}",
            )

    u.governed = True
    u.version = (u.version or 1) + 1
    db.flush()
    ident = u.identity_key or u.email
    models = _models_by_identity(db, [ident]).get(ident, [])
    spend = _user_spend(db, ident)
    return _user_dict(u, spend, db=db, models=models)


@router.post("/users/{email}/unmanage")
def unmanage_principal(
    email: str,
    body: dict = None,
    if_match: Optional[str] = Header(None),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """#627: remove a principal from deny-only governance.
    Clears `governed`, and detaches
    tg-BedrockQuotaDeny from the role **only when no other
    governed principal still uses that role** — otherwise the
    policy stays attached (the role-wide model-allowlist + the
    other principals' quota statements must keep applying) and
    only this principal's `governed` flag is cleared. The
    reconciler drops this principal's per-person quota statement
    on its next cycle.

    #827: Unmanage ALSO clears a manual force-block. Unmanage means
    "stop governing this principal entirely," so leaving
    `status=force_blocked` would let the reconciler re-add the deny
    on its next tick (`_should_deny` returns True for force_blocked
    regardless of `governed`) — the UI would say Unmanaged while IAM
    still denied (a force-blocked principal stayed denied after
    Unmanage). Clearing
    force_block here makes `_should_deny` False (under cap), so the
    reconciler drops this principal's QuotaDeny entry and — if it was
    the last governed user on the role — detaches the whole policy.
    The UI warns the admin (UnmanageModal) when the target is
    force_blocked so the unblock isn't a surprise.

    Note (advisory): a role holding `iam:*` can self-detach
    the deny — governance is advisory there. tg takes no SCP
    action; the UI must show the truthful state."""
    u = _resolve_user_or_404(db, email)
    scope.require_team_admin_for(u)  # #650: org or team admin
    _check_version(u, if_match)

    role_name = _role_name_from_arn(u.principal_arn)
    u.governed = False
    # #827: release a manual force-block atomically with ungoverning,
    # else the reconciler re-denies on its next tick. "active" is the
    # not-blocked resting state (mirrors /unblock, #750); if the
    # principal is still over cap the reconciler re-flips it to
    # "blocked" — but that's the cap deny, not the manual override.
    was_force_blocked = u.status == "force_blocked"
    if was_force_blocked:
        u.status = "active"
        u.force_blocked_at = None
    u.version = (u.version or 1) + 1
    db.flush()

    detached = False
    # #1065: for an IDC principal, ungoverning removes that permission
    # set's trust from tg-consumer — but ONLY if no OTHER governed IDC
    # user maps to the same permission set (mirror the reconciler's
    # shared-role care, #1011; never strip a trust a still-governed user
    # needs). The QuotaDeny still drops via the reconciler on the next
    # cycle; this just closes the assume-role hop when it's no longer
    # needed.
    if (u.role_type or "iam") == "idc":
        _remove_idc_consumer_trust_if_unused(db, u)
    # #1011: never DetachRolePolicy on an AWSReservedSSO_* role — tg
    # never attached it (the IDC manage path is attach-free), and a
    # direct detach would race IDC's own re-provision the same way an
    # attach would (#618). Clearing `governed` above is the whole
    # ungovern for an IDC principal; the reconciler drops its QuotaDeny
    # on the next cycle.
    if role_name and (u.role_type or "iam") != "idc":
        # Detach only when no OTHER governed principal
        # shares this role. Match by role ARN suffix (/role/<name>)
        # so assumed-role/role ARN shapes collapse to the role.
        others = (
            db.query(User)
            .filter(
                User.governed.is_(True),
                User.principal_arn.like(f"%:role/{role_name}"),
            )
            .count()
        )
        if others == 0:
            iam = _iam_client()
            policy_arn = _deny_policy_arn(iam)
            from botocore.exceptions import ClientError
            try:
                iam.detach_role_policy(
                    RoleName=role_name, PolicyArn=policy_arn)
                detached = True
            except ClientError as e:
                code = (e.response or {}).get(
                    "Error", {}).get("Code")
                # Already detached / role gone → success.
                if code not in ("NoSuchEntity",):
                    raise HTTPException(
                        502,
                        f"DetachRolePolicy failed ({code}) for "
                        f"role {role_name}: {e}",
                    )
                detached = True

    ident = u.identity_key or u.email
    models = _models_by_identity(db, [ident]).get(ident, [])
    spend = _user_spend(db, ident)
    out = _user_dict(u, spend, db=db, models=models)
    out["policy_detached"] = detached
    # #827: tell the UI whether unmanage ALSO lifted a force-block,
    # so the success toast can say "Unmanaged and unblocked".
    out["unblocked"] = was_force_blocked
    return out


@router.put("/users/{email}/status")
def set_status(
    email: str,
    body: dict,
    if_match: Optional[str] = Header(None),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    u = db.query(User).filter(User.email == email).first()
    if not u:
        raise HTTPException(404, f"User {email} not found")
    scope.require_team_admin_for(u)  # #650: org or team admin
    if if_match and str(u.version) != if_match:
        raise HTTPException(409, "Version mismatch — reload and retry")

    new_status = body.get("status")
    if new_status not in ("active", "blocked", "force_blocked"):
        raise HTTPException(400, f"Invalid status: {new_status}")

    u.status = new_status
    if new_status == "force_blocked":
        u.force_blocked_at = datetime.now(timezone.utc)
    elif new_status == "active":
        u.force_blocked_at = None
    u.version = (u.version or 1) + 1
    db.flush()
    return _user_dict(u, db=db)


@router.put("/users/{email}/cap")
def set_cap(
    email: str,
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    u = db.query(User).filter(User.email == email).first()
    if not u:
        raise HTTPException(404, f"User {email} not found")
    scope.require_team_admin_for(u)  # #650: org or team admin
    cap = body.get("cap_usd")
    if cap is not None and (not isinstance(cap, (int, float)) or cap < 0):
        raise HTTPException(400, "cap_usd must be a non-negative number or null")
    u.cap_usd = cap
    u.version = (u.version or 1) + 1
    db.flush()
    return _user_dict(u, db=db)


@router.post("/users/preregister")  # UI alias
@router.post("/users")
def create_user(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    email = (body.get("email") or "").lower().strip()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, f"User {email} already exists")
    u = User(
        email=email,
        status=body.get("status", "active"),
        cap_usd=body.get("cap_usd"),
        team_id=body.get("team_id"),
    )
    db.add(u)
    db.flush()
    return _user_dict(u, db=db)


@router.delete("/users/{email}")
def delete_user(
    email: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    u = db.query(User).filter(User.email == email).first()
    if not u:
        raise HTTPException(404, f"User {email} not found")
    # Cascade-clear team memberships & admin roles so the
    # FK on team_memberships doesn't block deletion.
    from db.models import TeamMembership, AdminRole
    db.query(TeamMembership).filter(
        TeamMembership.email == email).delete()
    db.query(AdminRole).filter(
        AdminRole.email == email).delete()
    db.delete(u)
    db.flush()
    return {"deleted": email}


def _check_version(u: User, if_match: Optional[str]):
    if if_match and str(u.version) != if_match:
        raise HTTPException(
            409, "Version mismatch — reload and retry")


@router.post("/users/{email}/approve")
def approve_user(
    email: str,
    body: dict = None,
    if_match: Optional[str] = Header(None),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """UI alias: approve a pre-registered user (status=active)."""
    scope.require_org_admin()
    u = db.query(User).filter(User.email == email).first()
    if not u:
        raise HTTPException(404, f"User {email} not found")
    _check_version(u, if_match)
    u.status = "active"
    u.force_blocked_at = None
    u.version = (u.version or 1) + 1
    db.flush()
    return _user_dict(u, db=db)


@router.put("/users/{email}/team")
def set_team(
    email: str,
    body: dict,
    if_match: Optional[str] = Header(None),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    u = db.query(User).filter(User.email == email).first()
    if not u:
        raise HTTPException(404, f"User {email} not found")
    scope.require_team_admin_for(u)  # #650: org or team admin
    _check_version(u, if_match)
    team_id = body.get("team_id")
    if team_id:
        from db.models import Team
        if not db.query(Team).filter(
            Team.team_id == team_id).first():
            raise HTTPException(
                400, f"Team {team_id} does not exist")
    u.team_id = team_id
    u.version = (u.version or 1) + 1
    db.flush()
    return _user_dict(u, db=db)


@router.put("/users/{email}/notes")
def set_notes(
    email: str,
    body: dict,
    if_match: Optional[str] = Header(None),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """
    Store a free-text note for a user. The User model has no
    `notes` column today, so we stash it in admin_config under
    a key prefix. This keeps the UI contract working without a
    schema migration.
    """
    u = db.query(User).filter(User.email == email).first()
    if not u:
        raise HTTPException(404, f"User {email} not found")
    scope.require_team_admin_for(u)  # #650: org or team admin
    _check_version(u, if_match)
    from db.models import AdminConfig
    key = f"user_notes:{email}"
    val = body.get("notes") or ""
    row = db.query(AdminConfig).filter(
        AdminConfig.key == key).first()
    if row:
        row.value = val
    else:
        db.add(AdminConfig(key=key, value=val))
    u.version = (u.version or 1) + 1
    db.flush()
    out = _user_dict(u, db=db)
    out["notes"] = val
    return out


# #750: Force block — a manual admin override that denies Bedrock
# now, regardless of spend, until Unblock. Replaces the old
# /disable (status="disabled"). The reconciler's _should_deny
# returns True for force_blocked.
@router.post("/users/{email}/force-block")
def force_block_user(
    email: str,
    body: dict,
    if_match: Optional[str] = Header(None),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    u = db.query(User).filter(User.email == email).first()
    if not u:
        raise HTTPException(404, f"User {email} not found")
    scope.require_team_admin_for(u)  # #650: org or team admin
    _check_version(u, if_match)
    confirm = (body or {}).get("confirm_email")
    if confirm and confirm != email:
        raise HTTPException(
            400, "confirm_email must match user email")
    # #809 (reverses #799's admin-role 409): an admin-role block is
    # enforceable now — the reconciler attaches tg-BedrockQuotaDeny to
    # whatever role this user assumes, and under denylist semantics a
    # role-wide attach is correct, not a freeze. So admin roles are no
    # longer refused. We DO still refuse the genuinely-unenforceable
    # case: a principal with no IAM role ARN at all (iam_user / root /
    # unknown) has nothing to attach a deny to, so a force-block there
    # would be a false success.
    role_name = _role_name_from_arn(u.principal_arn)
    if not role_name:
        raise HTTPException(
            409,
            f"Cannot block {email}: it has no IAM role to attach a "
            f"deny to (principal_type={u.principal_type}, "
            f"arn={u.principal_arn}). Bedrock-layer enforcement needs "
            "a role-based principal; nothing to block here.",
        )
    # Opt-in governance: tg enforces nothing on a principal it does
    # not govern. The reconciler gates ALL enforcement on `governed`
    # and self-heals an ungoverned blocked/force_blocked row back to
    # active on its next tick — so flipping the status here on a
    # non-governed principal would attach NO deny (Bedrock not
    # actually blocked) and then silently revert, giving the admin a
    # false "blocked" signal. Refuse instead and point at enrollment;
    # the admin manages (enrolls) the principal first, then blocks.
    if not u.governed:
        raise HTTPException(
            409,
            f"Cannot block {email}: it is not governed, so no deny is "
            "attached and Bedrock is not actually blocked (the status "
            "would revert on the next reconcile). Manage (enroll) the "
            "principal first, then block.",
        )
    u.status = "force_blocked"
    u.force_blocked_at = datetime.now(timezone.utc)
    u.version = (u.version or 1) + 1
    db.flush()
    return _user_dict(u, db=db)


# #750: Unblock — clears the manual force-block ONLY. It does NOT
# pin the user allowed: status returns to "active" as the
# not-blocked resting state, then the next reconcile tick
# re-evaluates spend >= cap and re-denies if still over cap (no
# cap-free window — the bug the old /enable had). To let an
# over-cap user through, raise their cap; there is no separate
# temporary-unblock path. Replaces the old /enable.
@router.post("/users/{email}/unblock")
def unblock_user(
    email: str,
    body: dict = None,
    if_match: Optional[str] = Header(None),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    u = db.query(User).filter(User.email == email).first()
    if not u:
        raise HTTPException(404, f"User {email} not found")
    scope.require_team_admin_for(u)  # #650: org or team admin
    _check_version(u, if_match)
    # Clear the manual override only. "active" is just the
    # not-force-blocked resting state — the reconciler will flip it
    # back to "blocked" on the next tick if spend is still >= cap.
    u.status = "active"
    u.force_blocked_at = None
    u.version = (u.version or 1) + 1
    db.flush()
    return _user_dict(u, db=db)
