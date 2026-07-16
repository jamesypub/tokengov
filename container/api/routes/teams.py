from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import CurUserSpend, Team, TeamMembership, User
from db.teams_membership import assign_user_team
from db.usage_windows import month_start_utc
from api.auth import get_caller_email, Scope


def _team_spend_map(db: Session) -> dict[str, float]:
    """Sum this-month (MTD) CurUserSpend.spend_usd for every team via
    the TeamMembership source of truth (one user, one team). Users in
    no team are excluded; no cross-team splitting. Grouping on the
    membership (not the shadow User.team_id column) keeps spend on the
    SAME source as the members list + count. MTD = sum of this
    month's per-day rows."""
    rows = (
        db.query(
            TeamMembership.team_id,
            func.sum(CurUserSpend.spend_usd).label("total"),
        )
        .join(CurUserSpend, CurUserSpend.email == TeamMembership.email)
        .filter(CurUserSpend.usage_hour >= month_start_utc())
        .group_by(TeamMembership.team_id)
        .all()
    )
    return {r.team_id: float(r.total or 0) for r in rows}

router = APIRouter()


def _db():
    with get_db() as db:
        yield db


def _scope(
    request: Request,
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


def _team_dict(
    t: Team,
    member_count: int = 0,
    spend_usd: float = 0.0,
) -> dict:
    return {
        "team_id":     t.team_id,
        "name":        t.name,
        "description": t.description,
        "parent_team_id": t.parent_team_id,
        "created_by":  t.created_by,
        "created_at":  t.created_at.isoformat() if t.created_at else None,
        "member_count": member_count,
        "budget_usd":  t.budget_usd,
        "spend_usd":   spend_usd,
    }


def _validate_budget(body: dict) -> Optional[float]:
    """Return validated budget_usd value (None or non-neg
    float). Raise 400 on invalid input."""
    if "budget_usd" not in body:
        return None  # caller decides whether to leave unchanged
    raw = body.get("budget_usd")
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, "budget_usd must be a number")
    if v < 0:
        raise HTTPException(
            400, "budget_usd must be non-negative")
    return v


def _validate_parent(
    db: Session,
    team_id: str,
    parent_team_id: Optional[str],
) -> None:
    """Reject self-parent + cycles + missing parent.

    Walks parent_team_id up the chain; if we hit team_id
    (or NULL after a long walk) we know we're either
    cycling or rooted.
    """
    if not parent_team_id:
        return
    if parent_team_id == team_id:
        raise HTTPException(400, "Team cannot be its own parent")
    seen = set()
    cur = parent_team_id
    while cur is not None:
        if cur == team_id:
            raise HTTPException(
                400, "Cycle detected in parent_team_id"
            )
        if cur in seen:
            raise HTTPException(
                400, "Cycle detected in parent_team_id"
            )
        seen.add(cur)
        row = db.query(Team.parent_team_id).filter(
            Team.team_id == cur
        ).first()
        if row is None:
            raise HTTPException(
                400,
                f"parent_team_id {parent_team_id} not found",
            )
        cur = row[0]


@router.get("/teams")
def list_teams(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    if scope.is_org_admin:
        teams = db.query(Team).order_by(Team.name).all()
    elif scope.admin_team_ids:
        teams = db.query(Team).filter(Team.team_id.in_(scope.admin_team_ids)).all()
    else:
        raise HTTPException(403, "Insufficient permissions")

    # Members come from two sources:
    #   - User.team_id  — user's primary team (set by
    #     populator + the user-edit UI)
    #   - TeamMembership — explicit add via
    #     /api/teams/{id}/members
    # TeamMembership is the single source of truth (one user, one
    # team). The count reads it ALONE — the same source the members
    # LIST reads — so count == list (fixes the old union-vs-list
    # divergence where a User.team_id with no membership was counted
    # but not listed). Backfill guarantees every prior team_id has a
    # membership row.
    members_by_team: dict[str, set[str]] = {}
    for team_id, email in db.query(
        TeamMembership.team_id, TeamMembership.email
    ).all():
        members_by_team.setdefault(team_id, set()).add(email)
    counts = {
        team_id: len(emails)
        for team_id, emails in members_by_team.items()
    }
    spend_map = _team_spend_map(db)
    return {"teams": [
        _team_dict(
            t,
            counts.get(t.team_id, 0),
            spend_map.get(t.team_id, 0.0),
        )
        for t in teams
    ]}


@router.post("/teams")
def create_team(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Team name required")
    # Caller may supply team_id (e.g. "team-1.1" for a
    # human-readable hierarchy). Otherwise auto-generate UUID.
    team_id = (body.get("team_id") or "").strip()
    if not team_id:
        team_id = str(uuid4())
    if db.query(Team).filter(Team.team_id == team_id).first():
        raise HTTPException(
            409, f"Team {team_id} already exists")
    parent_team_id = body.get("parent_team_id") or None
    if parent_team_id is not None:
        parent_team_id = str(parent_team_id).strip() or None
    _validate_parent(db, team_id, parent_team_id)
    budget = _validate_budget(body) if "budget_usd" in body else None
    t = Team(
        team_id=team_id,
        name=name,
        description=body.get("description"),
        parent_team_id=parent_team_id,
        created_by=scope.email,
        budget_usd=budget,
    )
    db.add(t)
    db.flush()
    return _team_dict(t)


@router.put("/teams/{team_id}")
def update_team(
    team_id: str,
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    t = db.query(Team).filter(Team.team_id == team_id).first()
    if not t:
        raise HTTPException(404, "Team not found")
    if "name" in body:
        t.name = body["name"]
    if "description" in body:
        t.description = body["description"]
    if "parent_team_id" in body:
        new_parent = body.get("parent_team_id") or None
        if new_parent is not None:
            new_parent = str(new_parent).strip() or None
        _validate_parent(db, team_id, new_parent)
        t.parent_team_id = new_parent
    if "budget_usd" in body:
        t.budget_usd = _validate_budget(body)
    db.flush()
    return _team_dict(t)


@router.delete("/teams/{team_id}")
def delete_team(
    team_id: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    t = db.query(Team).filter(Team.team_id == team_id).first()
    if not t:
        raise HTTPException(404, "Team not found")
    db.delete(t)
    db.flush()
    return {"deleted": team_id}


@router.get("/teams/{team_id}/members")
def list_members(
    team_id: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    if not scope.is_org_admin and team_id not in scope.admin_team_ids:
        raise HTTPException(403, "Insufficient permissions")
    members = (
        db.query(TeamMembership)
        .filter(TeamMembership.team_id == team_id)
        .all()
    )
    return {"members": [{"email": m.email, "added_by": m.added_by, "added_at": m.added_at.isoformat() if m.added_at else None} for m in members]}


@router.post("/teams/{team_id}/members")
def add_member(
    team_id: str,
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    if not scope.is_org_admin and team_id not in scope.admin_team_ids:
        raise HTTPException(403, "Insufficient permissions")
    email = (body.get("email") or "").lower().strip()
    if not email:
        raise HTTPException(400, "email required")
    # Source of truth + one-team invariant: assign via the shared
    # helper. Already in THIS team → no-op (idempotent); already in a
    # DIFFERENT team → 409 (reject, not auto-move). The helper also
    # shadows User.team_id so count/list/spend/scope stay consistent.
    assign_user_team(db, email, team_id, added_by=scope.email)
    db.flush()
    return {"email": email, "team_id": team_id}


@router.delete("/teams/{team_id}/members/{email}")
def remove_member(
    team_id: str,
    email: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    if not scope.is_org_admin and team_id not in scope.admin_team_ids:
        raise HTTPException(403, "Insufficient permissions")
    m = db.query(TeamMembership).filter(
        TeamMembership.team_id == team_id,
        TeamMembership.email == email,
    ).first()
    if not m:
        raise HTTPException(404, "Member not found")
    db.delete(m)
    # Clear the shadow column in lockstep with the membership so the
    # user is no longer counted/scoped under this team.
    u = db.query(User).filter(User.email == email).first()
    if u is not None and u.team_id == team_id:
        u.team_id = None
    db.flush()
    return {"removed": email}
