"""
Tests for `_scoped_team_filter` in api/routes/velocity.py — the
team-switcher filter that ALSO prevents privilege-escalation
via `?team=<not-owned>`. Follow-up to PR #231 / #227. (#232)

For each of `velocity_leaderboard` and `velocity_speed` we
exercise four cases against a team_admin who admins exactly
one of two seeded teams:

  1. team omitted        → caller sees their visible team(s)
  2. team="*"            → same as omitted
  3. team=<owned>        → only that team
  4. team=<not-owned>    → empty intersection (NOT the
                            requested team) — security case

Mirrors the auth-route test style added in PR #231: each test
exercises one observable behavior, assertions pin actual values
not just shape.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def client(pg_url, clean_db, monkeypatch):
    """Per-test client: patches sigv4 to claim the team_admin
    identity, seeds the V&C feature flag, and clears V&C-specific
    tables that conftest's clean_db doesn't touch (team_*_metrics,
    linked_accounts, github_repos have no FK to teams so CASCADE
    misses them)."""
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("tadmin@test.com", "session"),
    )

    import db.session as _dbs
    with _dbs.engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE TABLE team_weekly_metrics, "
            "team_daily_metrics, linked_accounts, "
            "github_repos RESTART IDENTITY CASCADE"
        ))

    from db.session import get_db
    from db.models import (
        AdminRole, Team,
    )
    with get_db() as db:
        db.add(Team(team_id="t_owned",     name="Owned"))
        db.add(Team(team_id="t_not_owned", name="NotOwned"))
        db.add(AdminRole(
            email="tadmin@test.com",
            role="team_admin",
            team_id="t_owned",
        ))

    from api.main import app
    with TestClient(app) as c:
        yield c


def _seed_weekly(team_id, *, cycle_h=4.0, prs=3, spend=12.5):
    """Seed a TeamWeeklyMetric row for the current week — within
    the default 30d window (last 4 weeks). Adds the `all` class
    row required by both endpoints."""
    from db.session import get_db
    from db.models import TeamWeeklyMetric
    now = datetime.now(timezone.utc)
    day_floor = now.replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    week_start = day_floor - timedelta(days=day_floor.weekday())
    with get_db() as db:
        db.add(TeamWeeklyMetric(
            team_id=team_id,
            week_start=week_start,
            pr_class="all",
            prs_merged=prs,
            spend_usd=spend,
            cycle_median_hours=cycle_h,
            cycle_p90_hours=cycle_h * 2,
        ))


def _team_ids(payload):
    return {t["team_id"] for t in payload["teams"]}


# ── velocity_leaderboard ────────────────────────────────────────────────────


def test_leaderboard_team_omitted_returns_visible_teams(client):
    """Case 1: no `team` arg → caller sees only their admin
    scope. team_admin of t_owned sees t_owned, NOT t_not_owned."""
    _seed_weekly("t_owned")
    _seed_weekly("t_not_owned")

    r = client.get("/api/velocity/leaderboard")
    assert r.status_code == 200
    assert _team_ids(r.json()) == {"t_owned"}


def test_leaderboard_team_wildcard_same_as_omitted(client):
    """Case 2: `team=*` is the sidebar's "Org / all" — must NOT
    expand scope beyond the role-visibility set."""
    _seed_weekly("t_owned")
    _seed_weekly("t_not_owned")

    r = client.get("/api/velocity/leaderboard?team=*")
    assert r.status_code == 200
    assert _team_ids(r.json()) == {"t_owned"}


def test_leaderboard_team_owned_returns_only_that_team(client):
    """Case 3: `team=<owned>` filters to the requested team. The
    sibling team must not appear even though it has metrics."""
    _seed_weekly("t_owned")
    _seed_weekly("t_not_owned")

    r = client.get("/api/velocity/leaderboard?team=t_owned")
    assert r.status_code == 200
    assert _team_ids(r.json()) == {"t_owned"}


def test_leaderboard_team_not_owned_returns_empty(client):
    """Case 4 — security: `team=<not-owned>` must NOT honor the
    request. Filter intersects with role visibility, which is
    empty for an unowned team → no rows leak. Bug surface this
    test pins: a regression that returns t_not_owned's row would
    let any team_admin scrape any team's data via URL hack."""
    _seed_weekly("t_owned")
    _seed_weekly("t_not_owned")

    r = client.get(
        "/api/velocity/leaderboard?team=t_not_owned")
    assert r.status_code == 200
    body = r.json()
    assert body["teams"] == []
    # Org row also rolls up over the (empty) visible set, so
    # the spend/prs aggregates must be zero — not the
    # not-owned team's numbers.
    assert body["org"]["spend_usd"] == 0.0
    assert body["org"]["prs_merged"] == 0


# ── velocity_speed ──────────────────────────────────────────────────────────


def test_speed_team_omitted_returns_visible_teams(client):
    """Case 1: no `team` arg → caller sees only their admin
    scope on the speed view as well."""
    _seed_weekly("t_owned")
    _seed_weekly("t_not_owned")

    r = client.get("/api/velocity/speed")
    assert r.status_code == 200
    assert _team_ids(r.json()) == {"t_owned"}


def test_speed_team_wildcard_same_as_omitted(client):
    """Case 2: `team=*` does not bypass role visibility."""
    _seed_weekly("t_owned")
    _seed_weekly("t_not_owned")

    r = client.get("/api/velocity/speed?team=*")
    assert r.status_code == 200
    assert _team_ids(r.json()) == {"t_owned"}


def test_speed_team_owned_returns_only_that_team(client):
    """Case 3: `team=<owned>` narrows correctly."""
    _seed_weekly("t_owned")
    _seed_weekly("t_not_owned")

    r = client.get("/api/velocity/speed?team=t_owned")
    assert r.status_code == 200
    assert _team_ids(r.json()) == {"t_owned"}


def test_speed_team_not_owned_returns_empty(client):
    """Case 4 — security: `team=<not-owned>` must yield empty.
    Mirror of the leaderboard security test on the speed
    endpoint, which has its own response shape."""
    _seed_weekly("t_owned")
    _seed_weekly("t_not_owned")

    r = client.get("/api/velocity/speed?team=t_not_owned")
    assert r.status_code == 200
    body = r.json()
    assert body["teams"] == []
    # Org row reflects zero visible activity.
    assert body["org"]["median_hours"] is None
