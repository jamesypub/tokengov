"""Team-membership source of truth (one user = one team, v1.1.1).

TeamMembership is the single authoritative record of which team a user
belongs to. User.team_id is NO LONGER an independently-written value —
it only ever SHADOWS the single membership row (kept in sync here so the
~20 existing read sites that filter/label on User.team_id keep working
without a risky column drop). Members-list, count, spend, and scope all
resolve team from the SAME source, so they cannot diverge (the count!=
list bug).

Invariant: AT MOST ONE TeamMembership per user email. Assigning a user
who is already in a different team is REJECTED (owner decision) — the
caller removes them first; we never silently move them.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from db.models import User, TeamMembership


def user_team(db: Session, email: str) -> str | None:
    """The user's team id, resolved from their single TeamMembership
    row (None if they belong to no team). This is the read side of the
    source of truth."""
    m = (
        db.query(TeamMembership.team_id)
        .filter(TeamMembership.email == email)
        .first()
    )
    return m[0] if m else None


def assign_user_team(
    db: Session, email: str, team_id: str | None, *, added_by: str | None,
) -> None:
    """Set (or clear) a user's team via the membership source of truth.

    - team_id None/empty → remove any membership (user has no team) and
      clear the shadow column.
    - team_id set, user already in THAT team → no-op.
    - team_id set, user already in a DIFFERENT team → 409 (reject; the
      caller removes them from the other team first).
    - team_id set, user in no team → create the membership.

    The User.team_id column is updated to mirror the membership so it
    is never written as an independent value. Does NOT flush; the
    caller commits within its own transaction.
    """
    existing = (
        db.query(TeamMembership)
        .filter(TeamMembership.email == email)
        .first()
    )
    want = (team_id or "").strip() or None

    if want is None:
        if existing is not None:
            db.delete(existing)
            db.flush()
    elif existing is None:
        db.add(TeamMembership(
            email=email, team_id=want, added_by=added_by))
        # Flush so a subsequent assign in the SAME session sees this
        # row via the existence query above (a pending, un-flushed add
        # is invisible to db.query and would double-insert → the
        # email/team_id unique-constraint violation).
        db.flush()
    elif existing.team_id != want:
        raise HTTPException(
            409,
            f"{email} is already in team {existing.team_id} — remove "
            "them from that team first (one user, one team)")
    # else: already in the wanted team → no-op.

    # Keep the shadow column in lockstep with the membership so the
    # legacy User.team_id read/filter sites stay correct.
    u = db.query(User).filter(User.email == email).first()
    if u is not None:
        u.team_id = want


def backfill_memberships(db: Session) -> int:
    """Idempotent backfill: for every user with a shadow team_id but no
    membership row, create the membership. Returns the number created.
    Skips a user whose email has no `users` row (FK) — none here since
    we iterate users — and any user already having a membership.
    Run once at startup so existing installs keep their teams under the
    new source of truth."""
    have = {
        e for (e,) in db.query(TeamMembership.email).distinct().all()
    }
    created = 0
    rows = (
        db.query(User.email, User.team_id)
        .filter(User.team_id.isnot(None))
        .all()
    )
    for email, team_id in rows:
        if email in have:
            continue
        db.add(TeamMembership(
            email=email, team_id=team_id, added_by="backfill"))
        have.add(email)
        created += 1
    return created
