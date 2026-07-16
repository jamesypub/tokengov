"""TeamMembership is the single source of truth (one user, one team).

Covers the source-of-truth acceptance:
  - the assign_user_team helper: create / no-op / cross-team reject /
    clear, and the shadow User.team_id kept in lockstep;
  - the count == list fix (a user assigned a team shows in the LIST and
    the count matches);
  - one-team invariant enforced across the write paths;
  - backfill idempotency.

Real Postgres testcontainer (shared conftest), org_admin caller.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def client(pg_url, clean_db):
    import api.auth as auth_mod
    auth_mod._validate_request = (
        lambda req, db: ("admin@test.com", "session")
    )
    from api.main import app
    from db.session import get_db
    from db.models import AdminRole, Team, User
    with get_db() as db:
        db.add(AdminRole(email="admin@test.com", role="org_admin"))
        db.add(Team(team_id="A", name="Team A"))
        db.add(Team(team_id="B", name="Team B"))
        # A pre-existing user with a shadow team_id but NO membership —
        # the exact count!=list / backfill case.
        db.add(User(email="legacy@t.com", status="active", team_id="A"))
    with TestClient(app) as c:
        yield c


# ── the helper (unit) ───────────────────────────────────────
# These use the `client` fixture only so app startup seeds teams A/B
# (the User.team_id FK needs the team row); they drive the helper
# directly against the same testcontainer DB.

def test_assign_creates_membership_and_shadows_column(client):
    from db.session import get_db
    from db.models import User, TeamMembership
    from db.teams_membership import assign_user_team, user_team
    with get_db() as db:
        db.add(User(email="u@t.com", status="active"))
        db.flush()
        assign_user_team(db, "u@t.com", "A", added_by="admin@test.com")
        db.flush()
        assert user_team(db, "u@t.com") == "A"
        # shadow column mirrors the membership
        u = db.query(User).filter(User.email == "u@t.com").first()
        assert u.team_id == "A"
        assert db.query(TeamMembership).filter(
            TeamMembership.email == "u@t.com").count() == 1


def test_assign_same_team_is_noop(client):
    from db.session import get_db
    from db.models import User, TeamMembership
    from db.teams_membership import assign_user_team
    with get_db() as db:
        db.add(User(email="u@t.com", status="active"))
        db.flush()
        assign_user_team(db, "u@t.com", "A", added_by="x")
        assign_user_team(db, "u@t.com", "A", added_by="x")  # again
        db.flush()
        assert db.query(TeamMembership).filter(
            TeamMembership.email == "u@t.com").count() == 1


def test_assign_different_team_rejects_409(client):
    from db.session import get_db
    from db.models import User
    from db.teams_membership import assign_user_team, user_team
    with get_db() as db:
        db.add(User(email="u@t.com", status="active"))
        db.flush()
        assign_user_team(db, "u@t.com", "A", added_by="x")
        db.flush()
        with pytest.raises(HTTPException) as ei:
            assign_user_team(db, "u@t.com", "B", added_by="x")
        assert ei.value.status_code == 409
        # unchanged — still in A (no auto-move)
        assert user_team(db, "u@t.com") == "A"


def test_assign_none_clears_membership_and_shadow(client):
    from db.session import get_db
    from db.models import User, TeamMembership
    from db.teams_membership import assign_user_team, user_team
    with get_db() as db:
        db.add(User(email="u@t.com", status="active"))
        db.flush()
        assign_user_team(db, "u@t.com", "A", added_by="x")
        db.flush()
        assign_user_team(db, "u@t.com", None, added_by="x")
        db.flush()
        assert user_team(db, "u@t.com") is None
        u = db.query(User).filter(User.email == "u@t.com").first()
        assert u.team_id is None
        assert db.query(TeamMembership).filter(
            TeamMembership.email == "u@t.com").count() == 0


# ── endpoints: count == list ────────────────────────────────

def test_add_member_shows_in_list_and_count_matches(client):
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(email="new@t.com", status="active"))
    r = client.post("/api/teams/A/members", json={"email": "new@t.com"})
    assert r.status_code == 200, r.text
    # LIST includes the member …
    members = client.get("/api/teams/A/members").json()["members"]
    emails = {m["email"] for m in members}
    assert "new@t.com" in emails
    # … and the COUNT equals the list length for team A.
    teams = client.get("/api/teams").json()["teams"]
    team_a = next(t for t in teams if t["team_id"] == "A")
    assert team_a["member_count"] == len(emails)


def test_add_member_cross_team_rejects(client):
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(email="mover@t.com", status="active"))
    assert client.post(
        "/api/teams/A/members", json={"email": "mover@t.com"}
    ).status_code == 200
    # Adding to a DIFFERENT team → 409 (reject, not move).
    r = client.post("/api/teams/B/members", json={"email": "mover@t.com"})
    assert r.status_code == 409
    # Still only in A.
    a = {m["email"] for m in client.get(
        "/api/teams/A/members").json()["members"]}
    b = {m["email"] for m in client.get(
        "/api/teams/B/members").json()["members"]}
    assert "mover@t.com" in a and "mover@t.com" not in b


# ── backfill ────────────────────────────────────────────────

def test_backfill_creates_missing_membership_then_idempotent(client):
    from db.session import get_db
    from db.models import User, TeamMembership
    from db.teams_membership import backfill_memberships, user_team
    with get_db() as db:
        # A legacy-shaped user: shadow team_id=B set directly, no
        # membership row (the count!=list case). Bypass the helper to
        # simulate pre-migration data.
        db.add(User(email="pre@t.com", status="active", team_id="B"))
        db.flush()
        assert db.query(TeamMembership).filter(
            TeamMembership.email == "pre@t.com").count() == 0
        created1 = backfill_memberships(db)
        db.flush()
        assert created1 >= 1
        assert user_team(db, "pre@t.com") == "B"
        # Idempotent: a second run creates nothing.
        assert backfill_memberships(db) == 0
        assert db.query(TeamMembership).filter(
            TeamMembership.email == "pre@t.com").count() == 1
