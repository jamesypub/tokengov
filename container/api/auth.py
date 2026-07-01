"""
Auth middleware — dispatches between the test-trust bypass
(scripts; gated by Environment, #570) and the session cookie
(browser OIDC/Cognito login, #130).

#576: the AWS-signed-STS validation path (a pre-signed STS
GetCallerIdentity replay) was the tg-admin desktop binary's auth.
The desktop client is deleted (#574 park -> #576 delete), so that
branch is dead code and removed here. Auth is now TWO methods:
`test` and `session`. A request that is neither (no test-trust,
no valid cookie) falls through to a clean 401 — not a signed-STS
attempt. The archived desktop + its auth path live at the
`desktop-admin-archived` git tag if ever needed.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import AdminRole  # noqa: F401 — used by Scope below


def _validate_session(request: Request, db: Session) -> Optional[str]:
    """
    Cookie-based session validation (#130). Looks up the cookie
    value in `web_sessions`. Returns the email on a hit, None
    when no cookie present (so the dispatcher falls through to a
    clean 401, #576), and raises 401 on an expired/unknown cookie
    so a stale browser doesn't silently get treated as anonymous.
    """
    sid = request.cookies.get("tg_session")
    if not sid:
        return None
    from datetime import datetime, timezone
    from db.models import WebSession
    sess = db.query(WebSession).filter(
        WebSession.id == sid).first()
    if not sess:
        raise HTTPException(401, "session not found — log in again")
    now = datetime.now(timezone.utc)
    if sess.expires_at <= now:
        # Best-effort cleanup; if the delete fails, the next
        # request still sees the same 401.
        try:
            db.query(WebSession).filter(
                WebSession.id == sid).delete()
            db.flush()
        except Exception:
            pass
        raise HTTPException(401, "session expired — log in again")
    sess.last_seen_at = now
    return sess.email.lower()


def _validate_request(request: Request, db: Session) -> tuple[str, str]:
    """
    Dispatcher: returns (email, auth_method). auth_method is one
    of "test" or "session" (#576 removed the signed-STS method
    with the desktop client). Order matters — test bypass first
    (so local dev /
    scripts work without creds, gated by Environment #570), then
    cookie (browser OIDC/Cognito login). Neither → clean 401.

    #587: on a successful resolve, bind the email to the `caller`
    log contextvar so this request's access line + every app log
    line it produces carry the user (best-effort — never let a
    logging concern break auth).
    """
    email, method = _resolve_caller(request, db)
    try:
        from api.log_context import caller_var
        caller_var.set(email)
    except Exception:  # noqa: BLE001 - logging must never break auth
        pass
    return email, method


def _resolve_caller(request: Request, db: Session) -> tuple[str, str]:
    test_trust = os.environ.get("TG_AUTH_TEST_TRUST") == "1"
    test_email = request.headers.get("x-tg-test-email", "")
    if test_trust:
        if test_email:
            return test_email.lower(), "test"
        bootstrap = os.environ.get(
            "BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
        if bootstrap:
            return bootstrap, "test"

    cookie_email = _validate_session(request, db)
    if cookie_email:
        return cookie_email, "session"

    # #576: no test-trust + no valid cookie → unauthenticated.
    # (Was the signed-STS desktop path; that client is deleted.)
    raise HTTPException(401, "authentication required — log in")


async def get_caller_email(
    request: Request,
    db: Session = Depends(lambda: next(_get_db_gen())),
) -> str:
    email, _method = _validate_request(request, db)
    return email


async def get_caller_auth(
    request: Request,
    db: Session = Depends(lambda: next(_get_db_gen())),
) -> tuple[str, str]:
    """Same as get_caller_email but also returns auth method."""
    return _validate_request(request, db)


def _get_db_gen():
    with get_db() as db:
        yield db


# ── Scope helper ────────────────────────────────────────────────────────────

class Scope:
    def __init__(self, email: str, db: Session):
        self.email = email
        self._db = db
        self._roles: Optional[list[AdminRole]] = None

    def _load(self):
        if self._roles is None:
            self._roles = (
                self._db.query(AdminRole)
                .filter(AdminRole.email == self.email)
                .all()
            )

    @property
    def is_org_admin(self) -> bool:
        self._load()
        return any(r.role == "org_admin" for r in self._roles)

    @property
    def is_team_admin(self) -> bool:
        self._load()
        return any(r.role == "team_admin" for r in self._roles)

    @property
    def is_member(self) -> bool:
        """#927: a plain member — authorized to log in but holds NO
        admin power. True when the only role(s) are `member` (no
        org_admin/team_admin row). An admin who ALSO has a member row
        is still an admin (is_member is the member-ONLY case)."""
        self._load()
        if not self._roles:
            return False
        return all(r.role == "member" for r in self._roles)

    @property
    def is_authorized(self) -> bool:
        """#927: has ANY authz row (member or admin) → the login gate
        admits them. The gate keys off this, not off admin-ness, so a
        member can authenticate (member-scoped — no admin surfaces)."""
        self._load()
        return bool(self._roles)

    @property
    def admin_team_ids(self) -> list[str]:
        """Team IDs this user can administer.

        Each team_admin row grants admin over that team plus
        all transitive descendants via Team.parent_team_id
        (any depth). Issue #104 retired parent_team_admin —
        descent is the universal rule.
        """
        self._load()
        seeds = {
            r.team_id for r in self._roles
            if r.role == "team_admin" and r.team_id
        }
        if not seeds:
            return []
        from db.models import Team
        # BFS down parent_team_id. One DB hit per level.
        visible = set(seeds)
        frontier = set(seeds)
        while frontier:
            rows = (
                self._db.query(Team.team_id)
                .filter(Team.parent_team_id.in_(frontier))
                .all()
            )
            kids = {r.team_id for r in rows} - visible
            if not kids:
                break
            visible.update(kids)
            frontier = kids
        return list(visible)

    def require_org_admin(self):
        if not self.is_org_admin:
            raise HTTPException(403, "org_admin role required")

    # ── #650: 3-tier RBAC on user-detail actions ────────────────
    # org_admin (any user) > team_admin (own team subtree) >
    # member (self only). These codify the tier the GitHub
    # linked-accounts routes already open-coded, so management +
    # self-service routes share one enforcement path.

    def can_admin_user(self, user) -> bool:
        """True if the caller may perform admin/management actions
        on `user`: an org_admin (any user), or a team_admin of the
        user's team (incl. descendants via admin_team_ids). False
        for a member, or when the user has no team an org_admin
        can't be matched against."""
        if self.is_org_admin:
            return True
        team_id = getattr(user, "team_id", None) if user else None
        if not team_id:
            return False
        return team_id in (self.admin_team_ids or [])

    def is_self(self, user_or_email) -> bool:
        """True if the caller IS the target (self-service)."""
        email = (
            getattr(user_or_email, "email", None)
            if not isinstance(user_or_email, str)
            else user_or_email
        )
        return (self.email or "").lower() == (email or "").lower()

    def require_team_admin_for(self, user):
        """Gate management/governance actions: org_admin OR
        team_admin of this user's team. 403 otherwise (incl. a
        team_admin acting outside their subtree, and any member)."""
        if not self.can_admin_user(user):
            raise HTTPException(
                403,
                "org_admin or team_admin of this user's team "
                "required",
            )

    def require_self_or_team_admin_for(self, user):
        """Gate self-service actions (display name, GitHub link):
        the user themselves, OR an admin per the tier above."""
        if self.is_self(user) or self.can_admin_user(user):
            return
        raise HTTPException(
            403,
            "must be the user, org_admin, or their team_admin",
        )
