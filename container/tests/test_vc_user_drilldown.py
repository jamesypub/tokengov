"""
Tests for V&C user drill-down endpoints (ticket #264):
- /api/usage includes prs_merged_30d per row
- /api/velocity/speed?breakdown=user&team=X returns user rows
"""
from __future__ import annotations
import datetime as dt
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def clean_vc(pg_url):
    import db.session as _dbs
    from sqlalchemy import text
    with _dbs.engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE TABLE users, teams, team_memberships, "
            "cur_user_spend, "
            "github_activity, github_repos, "
            "linked_accounts, admin_config, admin_roles, "
            "pr_classifications "
            "RESTART IDENTITY CASCADE"
        ))
    yield


@pytest.fixture
def client(pg_url, clean_vc, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"),
    )
    from db.session import get_db
    from db.models import (
        AdminRole, Team, User,
        CurUserSpend, GithubActivity, LinkedAccount,
    )
    now = dt.datetime.now(dt.timezone.utc)
    with get_db() as db:
        db.add(AdminRole(
            email="admin@test.com", role="org_admin",
        ))
        db.add(Team(team_id="t1", name="Team One"))
        db.add(User(
            email="alice@test.com",
            team_id="t1",
            status="active",
        ))
        db.add(User(
            email="bob@test.com",
            team_id="t1",
            status="active",
        ))
        db.add(LinkedAccount(
            email="alice@test.com",
            vendor="github",
            external_handle="alice-gh",
            linked_by="admin@test.com",
        ))
        db.add(LinkedAccount(
            email="bob@test.com",
            vendor="github",
            external_handle="bob-gh",
            linked_by="admin@test.com",
        ))
        usage_hour = now.date()  # #643: per-day grain, today
        db.add(CurUserSpend(
            email="alice@test.com",
            model_id="claude-sonnet",
            usage_hour=usage_hour,
            total_tokens=1000,
            input_tokens=700,
            output_tokens=300,
            spend_usd=1.50,
        ))
        db.add(CurUserSpend(
            email="bob@test.com",
            model_id="claude-haiku",
            usage_hour=usage_hour,
            total_tokens=500,
            input_tokens=300,
            output_tokens=200,
            spend_usd=0.50,
        ))
        created = now - dt.timedelta(hours=24)
        for i in range(3):
            db.add(GithubActivity(
                repo="org/repo",
                pr_number=i + 1,
                title=f"PR {i+1}",
                author_login="alice-gh",
                merged_at=now - dt.timedelta(days=i),
                created_at=created - dt.timedelta(days=i),
            ))
        db.add(GithubActivity(
            repo="org/repo",
            pr_number=10,
            title="Bob PR",
            author_login="bob-gh",
            merged_at=now - dt.timedelta(days=1),
            created_at=created - dt.timedelta(days=1),
        ))

    from api.main import app
    with TestClient(app) as c:
        yield c


def test_usage_prs_merged(client):
    r = client.get("/api/usage?team=t1")
    assert r.status_code == 200
    rows = r.json()["rows"]
    by_email = {row["email"]: row for row in rows}
    assert "alice@test.com" in by_email
    assert by_email["alice@test.com"]["prs_merged_30d"] == 3
    assert by_email["bob@test.com"]["prs_merged_30d"] == 1


def test_usage_prs_merged_no_linked_account(client):
    r = client.get("/api/usage")
    assert r.status_code == 200
    rows = r.json()["rows"]
    for row in rows:
        assert "prs_merged_30d" in row


def test_speed_user_breakdown(client):
    r = client.get(
        "/api/velocity/speed"
        "?breakdown=user&team=t1&window=30d",
    )
    assert r.status_code == 200
    body = r.json()
    assert "users" in body
    users = body["users"]
    emails = [u["email"] for u in users]
    assert "alice@test.com" in emails
    alice = next(u for u in users if u["email"] == "alice@test.com")
    assert alice["prs_merged"] == 3
    assert alice["median_hours"] is not None
    assert alice["p90_hours"] is not None


def test_speed_user_breakdown_requires_team(client):
    r = client.get(
        "/api/velocity/speed?breakdown=user",
    )
    assert r.status_code == 400


def test_speed_user_breakdown_empty_team(client):
    r = client.get(
        "/api/velocity/speed"
        "?breakdown=user&team=nonexistent&window=30d",
    )
    assert r.status_code == 200
    assert r.json()["users"] == []


# ────────────────────────────────────────────────
# /api/velocity/leaderboard/users — Cost drilldown (#270)
# ────────────────────────────────────────────────

def test_cost_user_breakdown_returns_per_user_rows(client):
    r = client.get(
        "/api/velocity/leaderboard/users"
        "?team_id=t1&window=30d&type=all",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["team"] == "t1"
    users = body["users"]
    by_email = {u["email"]: u for u in users}
    # Both alice (3 PRs) and bob (1 PR) appear — they have
    # linked_accounts and PRs in the window.
    assert "alice@test.com" in by_email
    assert "bob@test.com" in by_email
    alice = by_email["alice@test.com"]
    assert alice["prs_merged"] == 3
    # #643: spend_usd is the REAL per-day figure now (no month
    # spread) — alice's $1.50 row is dated today, inside the 30d
    # window, so the exact full amount lands.
    assert alice["spend_usd"] == 1.5
    # mix_pct keys are story/bug/task summing to ~100.
    mix = alice["mix_pct"]
    assert set(mix.keys()) == {"story", "bug", "task"}
    assert sum(mix.values()) in (99, 100, 101)
    # No PrClassification rows seeded → all PRs fall to
    # the "task" bucket (matches pr_classify fallback).
    assert mix["task"] == 100
    # dollar_per_pr is computed when prs > 0.
    assert alice["dollar_per_pr"] is not None


def test_cost_user_breakdown_type_filter(client):
    """type filter narrows prs_merged. With no
    PrClassification rows seeded, every PR is implicitly
    'task', so type=task returns all PRs and type=story
    returns zero."""
    r = client.get(
        "/api/velocity/leaderboard/users"
        "?team_id=t1&window=30d&type=story",
    )
    assert r.status_code == 200
    users = r.json()["users"]
    # All zero — no PRs are story-classified.
    for u in users:
        assert u["prs_merged"] == 0


def test_cost_user_breakdown_empty_team(client):
    r = client.get(
        "/api/velocity/leaderboard/users"
        "?team_id=nonexistent&window=30d",
    )
    assert r.status_code == 200
    assert r.json()["users"] == []


def test_cost_user_breakdown_requires_team_id(client):
    r = client.get("/api/velocity/leaderboard/users")
    # FastAPI rejects missing required query param with 422.
    assert r.status_code == 422
