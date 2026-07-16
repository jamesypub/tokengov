"""
Tests that velocity_leaderboard's per-team `devs` and `repos`
columns are sourced from `linked_accounts` and `github_repos`
rows — NOT hardcoded constants. (#252)

The original bug report was that every V&C row showed
"3 devs · 1 repo" because the populate script seeded exactly
3 members per team and exactly 1 repo per team. The fix was
in the seed data (varied member counts in
tg-test-data-populate.sh, plus extra ornamental repos in
tg-vc-seed-synthetic.py), which means the route code must
already be data-driven.

These tests pin that contract so a future regression that
defaults `devs` or `repos` to a literal can't pass.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text


@pytest.fixture
def client(pg_url, clean_db, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("oadmin@test.com", "session"),
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
        AdminRole, Team, TeamWeeklyMetric,
        User, LinkedAccount, GithubRepo,
    )
    now = datetime.now(timezone.utc)
    day_floor = now.replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    week_start = day_floor - timedelta(days=day_floor.weekday())

    teams_spec = [
        # team_id, n_members, n_repos
        ("t_alpha", 5, 3),
        ("t_beta",  2, 1),
        ("t_gamma", 4, 2),
    ]

    with get_db() as db:
        db.add(AdminRole(
            email="oadmin@test.com", role="org_admin"))
        for tid, _n_mem, _n_repos in teams_spec:
            db.add(Team(team_id=tid, name=tid))

    # Three teams with deliberately different head counts +
    # repo counts. If the route hardcodes either dimension
    # one of these assertions fails.
    with get_db() as db:
        for tid, n_mem, n_repos in teams_spec:
            for i in range(n_mem):
                email = f"{tid}-m{i}@test.com"
                db.add(User(email=email, team_id=tid))
                db.add(LinkedAccount(
                    email=email, vendor="github",
                    external_handle=f"gh-{tid}-{i}",
                    linked_by="seed",
                    linked_at=now,
                ))
            for i in range(n_repos):
                db.add(GithubRepo(
                    repo=f"tenant/{tid}-r{i}",
                    team_id=tid,
                    sync_status="ok",
                    last_sync_at=now,
                    added_by="seed",
                ))
            db.add(TeamWeeklyMetric(
                team_id=tid,
                week_start=week_start,
                pr_class="all",
                prs_merged=5,
                spend_usd=10.0,
                cycle_median_hours=4.0,
                cycle_p90_hours=12.0,
            ))

    from api.main import app
    with TestClient(app) as c:
        yield c


def _by_team(payload):
    return {t["team_id"]: t for t in payload["teams"]}


def test_devs_column_reflects_linked_accounts_count(client):
    """The `devs` field must be the per-team distinct count of
    LinkedAccount rows (vendor=github), not a constant."""
    r = client.get("/api/velocity/leaderboard")
    assert r.status_code == 200
    rows = _by_team(r.json())
    assert rows["t_alpha"]["devs"] == 5
    assert rows["t_beta"]["devs"] == 2
    assert rows["t_gamma"]["devs"] == 4
    # And — the contract this test is really pinning — the
    # three teams must NOT all return the same number.
    assert len({
        rows["t_alpha"]["devs"],
        rows["t_beta"]["devs"],
        rows["t_gamma"]["devs"],
    }) == 3


def test_repos_column_reflects_github_repos_count(client):
    """Same contract for `repos`: must come from github_repos
    rows scoped to team_id, never a constant."""
    r = client.get("/api/velocity/leaderboard")
    assert r.status_code == 200
    rows = _by_team(r.json())
    assert len(rows["t_alpha"]["repos"]) == 3
    assert len(rows["t_beta"]["repos"]) == 1
    assert len(rows["t_gamma"]["repos"]) == 2
    assert len({
        len(rows["t_alpha"]["repos"]),
        len(rows["t_beta"]["repos"]),
        len(rows["t_gamma"]["repos"]),
    }) == 3


def test_speed_view_uses_same_data_sources(client):
    """The Speed view also surfaces devs/repos and must follow
    the same data contract — pinned separately because it has
    its own copy of the count logic."""
    r = client.get("/api/velocity/speed?type=all")
    assert r.status_code == 200
    rows = _by_team(r.json())
    assert rows["t_alpha"]["devs"] == 5
    assert rows["t_beta"]["devs"] == 2
    assert len(rows["t_alpha"]["repos"]) == 3
    assert len(rows["t_beta"]["repos"]) == 1
