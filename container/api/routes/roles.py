from __future__ import annotations
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import AdminRole, User
from db.org_config import tg_owns_directory
from api.auth import get_caller_email, Scope

router = APIRouter()


def _db():
    with get_db() as db:
        yield db


def _cognito_provisioning_enabled(db: Session) -> bool:
    """Cognito user-provisioning is gated on whether tg OWNS the
    directory (#926). When tg owns it (Cognito), the checkbox +
    AdminCreateUser path is live; when an external IdP owns it
    (federated SAML/OIDC), tg provisions nothing — users come from
    the IdP. Read from the DB flag (config-as-data, runtime-editable),
    NOT the install-time TG_AUTH_PROVIDER env var (#926 moved the
    provider from env → DB so it can change without a redeploy)."""
    return tg_owns_directory(db)


def _provision_cognito_user(
    email: str, resend: bool = False,
) -> None:
    """Create the user in the configured Cognito pool and
    let Cognito send the invitation email. Raises on failure
    so the caller can abort before inserting the admin row
    (transactional contract — acceptance #3).

    Pool id comes from TG_COGNITO_USER_POOL_ID (a
    tg-cognito-pool stack output). The worker/api task role
    grants cognito-idp:AdminCreateUser scoped to that pool
    only when EnableCognitoAdminProvisioning=true.

    resend=True re-sends the invitation to an EXISTING user
    (MessageAction=RESEND) without resetting their password.
    This is the re-issue path for a stale invite: the original
    email is frozen with whatever console URL was current when
    it was sent, so when the ALB DNS churns (a tg-container-stack
    recreate mints a fresh tg-alb-<random> host) that link goes
    dead. Re-sending generates a new email carrying the pool's
    CURRENT CallbackUrl — which the installer reconciles to the
    live ALB on every deploy. RESEND only works while the user
    is still FORCE_CHANGE_PASSWORD (never signed in); once they
    set a password the temp-password invite no longer applies."""
    pool_id = os.environ.get(
        "TG_COGNITO_USER_POOL_ID", "").strip()
    if not pool_id:
        raise HTTPException(
            500,
            "Cognito provisioning requested but "
            "TG_COGNITO_USER_POOL_ID is unset",
        )
    import boto3
    cognito = boto3.client(
        "cognito-idp",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    kwargs = dict(
        UserPoolId=pool_id,
        Username=email,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        DesiredDeliveryMediums=["EMAIL"],
    )
    if resend:
        # RESEND re-delivers the invite to an existing user;
        # UserAttributes must be omitted (the API rejects
        # attribute changes alongside RESEND).
        kwargs["MessageAction"] = "RESEND"
        kwargs.pop("UserAttributes")
    try:
        cognito.admin_create_user(**kwargs)
    except Exception as e:
        # Surface as a clean 500 with a reason rather than an
        # uncaught stack trace. Raising here (before the row
        # insert) is what guarantees no half-state: the admin
        # row is never added when Cognito provisioning fails.
        raise HTTPException(
            500,
            f"Cognito AdminCreateUser failed: "
            f"{type(e).__name__}: {e}",
        )


def _scope(
    request: Request,
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


@router.get("/admin-roles")  # UI alias
@router.get("/roles")
def list_roles(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    roles = db.query(AdminRole).order_by(AdminRole.email).all()
    return {"roles": [
        {
            "email":      r.email,
            "role":       r.role,
            "team_id":    r.team_id,
            "granted_by": r.granted_by,
            "granted_at": r.granted_at.isoformat() if r.granted_at else None,
        }
        for r in roles
    ]}


@router.post("/admin-roles")  # UI alias
@router.post("/roles")
def grant_role(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    email   = (body.get("email") or "").lower().strip()
    role    = body.get("role")
    team_id = body.get("team_id")
    # Cognito provisioning is now AUTOMATIC whenever tg owns the
    # directory — the Add-user flow no longer sends an opt-in flag
    # (adding a person on a Cognito deployment always creates their
    # login; on an external-IdP deployment tg authorizes only and
    # calls no Cognito API). The legacy `provision_cognito` body flag
    # is accepted-but-ignored for older clients: provisioning is driven
    # by tg_owns_directory, not the flag. A `false` from an old client
    # does NOT suppress it — the directory decides.
    # #104: parent_team_admin retired — accept it but
    # silently coerce so any client still sending the old
    # name (older tg-admin binaries, scripts) keeps working.
    if role == "parent_team_admin":
        role = "team_admin"
    # #927: `member` is now a valid grantable role — a member is
    # authorized to log in but holds NO admin power (the gate scopes
    # them; is_org_admin/is_team_admin match only those exact roles,
    # so a member row grants neither — privilege-safe by construction).
    if not email or role not in ("org_admin", "team_admin", "member"):
        raise HTTPException(
            400, "email and valid role required")
    exists = db.query(AdminRole).filter(
        AdminRole.email == email,
        AdminRole.role == role,
        AdminRole.team_id == team_id,
    ).first()
    if exists:
        raise HTTPException(409, f"{email} already has {role}")
    # Transactional contract (acceptance #3): provision the
    # Cognito user BEFORE inserting the admin row. If
    # AdminCreateUser raises, we never add the row — no
    # half-state where TG thinks the admin exists but they
    # can't log in. The row insert is flushed only after
    # Cognito succeeds.
    cognito_provisioned = False
    if _cognito_provisioning_enabled(db):
        _provision_cognito_user(email)
        cognito_provisioned = True
    r = AdminRole(email=email, role=role, team_id=team_id, granted_by=scope.email)
    db.add(r)
    db.flush()
    return {
        "email": email,
        "role": role,
        "team_id": team_id,
        "cognito_provisioned": cognito_provisioned,
        # Mirror enable_login's shape so the Add-user modal can key its
        # SSO-vs-Cognito done-screen copy off one field on either path.
        "directory": "cognito" if cognito_provisioned else "external_idp",
    }


@router.post("/admin-roles/{email}/enable-login")  # UI alias
@router.post("/roles/{email}/enable-login")
def enable_login(
    email: str,
    body: dict = None,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """#927: bring a person into the app's login from the Users
    screen. TWO steps, IdP-aware:

      1. **Authorize** (always): ensure an authz row the login gate
         accepts — an `admin_roles` row with the granted role
         (default `member`; an admin may grant `team_admin`/`org_admin`
         with a team).
      2. **Provision** (conditional): when tg owns the directory
         (#926 `tg_owns_directory` true → Cognito), create the Cognito
         user + send the invite via the existing
         `_provision_cognito_user`. When an external IdP owns it,
         SKIP — the user signs in via SSO (JIT-authorized by the gate).

    Transactional (per #357): provision BEFORE the row insert so a
    Cognito failure leaves NO 'in tg but can't log in' half-state.
    Org/team-admin gated (same scope as granting a role)."""
    body = dict(body or {})
    email = (email or "").lower().strip()
    role = (body.get("role") or "member").strip()
    team_id = body.get("team_id")
    if role == "parent_team_admin":
        role = "team_admin"
    if not email:
        raise HTTPException(400, "email required")
    if role not in ("org_admin", "team_admin", "member"):
        raise HTTPException(400, "valid role required")
    # Authorization: granting an ADMIN role is
    # org-admin-only (mirrors grant_role's require_org_admin). The
    # MEMBER path may be done by a team_admin — but ONLY into a team
    # they administer (else any team_admin could mint authz into any
    # team). An org_admin may target any team.
    if role in ("org_admin", "team_admin"):
        scope.require_org_admin()
    else:  # member
        if not scope.is_org_admin:
            if not scope.is_team_admin:
                raise HTTPException(
                    403,
                    "org_admin or team_admin required to enable login")
            # Team-scoped: a team_admin can only enable a member into
            # a team within their subtree (admin_team_ids). A missing
            # team_id, or one outside their subtree, is 403 — enforce
            # the team-scope this endpoint claims.
            if not team_id or team_id not in (scope.admin_team_ids or []):
                raise HTTPException(
                    403,
                    "team_admin can only enable login for a member of "
                    "a team you administer — pass a team_id within "
                    "your teams")
    # Anti-spray: only enable login for someone tg
    # ALREADY knows — a CUR-discovered `users` row (state 1) or an
    # existing authz row (state 2). Without this, the Cognito
    # AdminCreateUser below would email an ARBITRARY address. Mirrors
    # reinvite_admin's known-person 404 guard. (The UI only surfaces
    # the action on known rows, but the API is the contract.)
    known_user = db.query(User).filter(User.email == email).first()
    existing = db.query(AdminRole).filter(
        AdminRole.email == email).first()
    if known_user is None and existing is None:
        raise HTTPException(
            404,
            f"{email} is not a person tg knows (no observed usage and "
            "no prior registration) — enable login only for a "
            "discovered or pre-registered user")
    # Idempotent: already-enabled (any authz row for this email) →
    # 409 so the UI hides the action rather than double-inviting.
    if existing is not None:
        raise HTTPException(
            409, f"{email} already has a login (role "
                 f"'{existing.role}')")
    # Step 2 BEFORE step 1 (transactional): provision the IdP identity
    # only when tg owns the directory; an external-IdP deployment skips
    # it (the user comes from the IdP). A provision failure raises →
    # no authz row added.
    cognito_provisioned = False
    if _cognito_provisioning_enabled(db):
        _provision_cognito_user(email)
        cognito_provisioned = True
    # Step 1: the authz row the login gate accepts.
    db.add(AdminRole(
        email=email, role=role, team_id=team_id,
        granted_by=scope.email))
    db.flush()
    return {
        "email": email,
        "role": role,
        "team_id": team_id,
        "login_enabled": True,
        "cognito_provisioned": cognito_provisioned,
        # The UI toast keys off this: Cognito → "invite sent / use
        # Forgot password"; external IdP → "they sign in via SSO".
        "directory": "cognito" if cognito_provisioned else "external_idp",
    }


@router.post("/admin-roles/{email}/reinvite")  # UI alias
@router.post("/roles/{email}/reinvite")
def reinvite_admin(
    email: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Re-send the Cognito invite to an existing admin.

    The fix for the stale sign-in URL (a sent invite is frozen
    with whatever ALB DNS was current when it went out; a later
    tg-container-stack recreate mints a new host and the old
    link dies). Re-sending issues a fresh email carrying the
    pool's CURRENT CallbackUrl. Idempotent and safe: only works
    while the user has not yet set a password (still on the
    temporary one). Org-admin only, and a no-op-with-400 on
    non-Cognito deployments so the same UI works everywhere."""
    scope.require_org_admin()
    email = (email or "").lower().strip()
    if not email:
        raise HTTPException(400, "email required")
    if not _cognito_provisioning_enabled(db):
        raise HTTPException(
            400,
            "Re-invite is only available when tg owns the "
            "directory (Cognito). This deployment federates to an "
            "external IdP — re-invites are managed there.",
        )
    # Only re-invite someone tg actually knows as an admin —
    # don't let this become an open invite-spray to arbitrary
    # addresses.
    known = db.query(AdminRole).filter(
        AdminRole.email == email).first()
    if not known:
        raise HTTPException(404, f"{email} is not an admin")
    _provision_cognito_user(email, resend=True)
    return {"email": email, "reinvited": True}


@router.delete("/admin-roles/{email}/{team_path:path}")
def revoke_role_with_team(
    email: str,
    team_path: str,
    body: dict = None,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """
    UI variant: DELETE /admin-roles/{email}/{team_id}.
    The React UI sends the team in the path; role is in body.
    """
    body = dict(body or {})
    body["team_id"] = team_path
    return revoke_role(email, body, scope, db)


@router.delete("/admin-roles/{email}")  # UI alias
@router.delete("/roles/{email}")
def revoke_role(
    email: str,
    body: dict = None,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    body = body or {}
    role    = body.get("role")
    team_id = body.get("team_id")
    q = db.query(AdminRole).filter(AdminRole.email == email)
    if role:
        q = q.filter(AdminRole.role == role)
    if team_id:
        q = q.filter(AdminRole.team_id == team_id)

    # Prevent removing the last org_admin
    if role == "org_admin" or not role:
        remaining = db.query(AdminRole).filter(
            AdminRole.role == "org_admin",
            AdminRole.email != email,
        ).count()
        if remaining == 0:
            raise HTTPException(400, "Cannot remove the last org_admin")

    deleted = q.delete()
    if not deleted:
        raise HTTPException(404, "Role not found")
    db.flush()
    return {"revoked": email}
