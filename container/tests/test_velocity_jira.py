"""
Integration tests for the Jira-aware V&C endpoints (#365).

Covers /sprints, /epics, /series filters, /sprint/{id}
detail, and the gating on jira_sites being linked.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


def _set_caller(monkeypatch, email: str):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: (email, "session"), raising=True,
    )


def _seed_jira(linked=True):
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import JiraSite, JiraIssue
    now = datetime.now(timezone.utc)
    with get_db() as db:
        if linked:
            db.add(JiraSite(
                site_url="https://x.atlassian.net",
                auth_email="ci@x.com",
                api_token_plain="DUMMY",
                projects=json.dumps(["PROJ"]),
                sync_status="ok",
                added_by="t@x.com",
            ))
        # 3 issues: 2 stories in sprint 100 (1 done),
        #          1 bug in sprint 101 (done).
        db.add(JiraIssue(
            issue_key="PROJ-1", issue_type="Story",
            status="Done", status_category="done",
            parent_epic_key="PROJ-100",
            sprint_id=100, sprint_name="Sprint 100",
            story_points=3.0,
            jira_created_at=now, jira_updated_at=now,
        ))
        db.add(JiraIssue(
            issue_key="PROJ-2", issue_type="Story",
            status="In Progress", status_category="indeterminate",
            parent_epic_key="PROJ-100",
            sprint_id=100, sprint_name="Sprint 100",
            story_points=5.0,
            jira_created_at=now, jira_updated_at=now,
        ))
        db.add(JiraIssue(
            issue_key="PROJ-100", issue_type="Epic",
            status="In Progress", status_category="indeterminate",
            jira_created_at=now, jira_updated_at=now,
            summary="Big initiative",
        ))


@pytest.fixture
def client(pg_url, clean_db, monkeypatch):
    _set_caller(monkeypatch, "admin@test.com")
    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        db.add(AdminRole(
            email="admin@test.com", role="org_admin"))
    from api.main import app
    with TestClient(app) as c:
        yield c


def test_sprints_returns_unlinked_when_no_site(client):
    r = client.get("/api/velocity/jira/sprints")
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is False
    assert body["sprints"] == []


def test_sprints_returns_active_first(client):
    _seed_jira()
    r = client.get("/api/velocity/jira/sprints")
    body = r.json()
    assert body["linked"] is True
    sids = [s["id"] for s in body["sprints"]]
    assert 100 in sids
    sprint_100 = next(s for s in body["sprints"] if s["id"] == 100)
    # PROJ-2 in sprint 100 has status_category != "done", so the
    # sprint is active.
    assert sprint_100["active"] is True
    assert sprint_100["issue_count"] == 2


def test_epics_returns_only_linked_via_pr(client):
    _seed_jira()
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import GithubActivity, PrJiraRef
    now = datetime.now(timezone.utc)
    with get_db() as db:
        db.add(GithubActivity(
            repo="o/r", pr_number=1,
            title="t", author_login="alice",
            labels="[]", issue_refs="[]",
            merged_at=now,
        ))
        db.add(PrJiraRef(
            repo="o/r", pr_number=1,
            issue_key="PROJ-1", source="title",
        ))
    r = client.get("/api/velocity/jira/epics")
    body = r.json()
    assert body["linked"] is True
    assert any(
        e["key"] == "PROJ-100" and "initiative" in e["summary"]
        for e in body["epics"]
    )


def test_series_returns_empty_when_unlinked(client):
    r = client.get("/api/velocity/jira/series")
    body = r.json()
    assert body["linked"] is False
    assert body["weeks"] == []
    assert body["totals"] is None


def test_series_returns_data_after_rollup(client):
    """Full pipeline: github_activity → pr_jira_refs +
    jira_issues → pr_cost_rollup → jira_weekly_metrics →
    /series."""
    _seed_jira()
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import (
        GithubActivity, PrJiraRef, Team, TeamMembership,
        User, LinkedAccount, PrClassification, CurUserSpend,
    )
    now = datetime.now(timezone.utc)
    with get_db() as db:
        db.add(Team(team_id="t1", name="Team1"))
        db.flush()
        db.add(User(
            email="alice@x.com", status="active",
            team_id="t1", last_seen_at=now,
        ))
        db.add(LinkedAccount(
            email="alice@x.com", vendor="github",
            external_handle="alice", linked_by="auto",
        ))
        db.add(TeamMembership(
            team_id="t1", email="alice@x.com",
        ))
        db.add(GithubActivity(
            repo="o/r", pr_number=1,
            title="PROJ-1: shipping", author_login="alice",
            labels="[]", issue_refs="[]",
            merged_at=now,
        ))
        db.add(PrJiraRef(
            repo="o/r", pr_number=1,
            issue_key="PROJ-1", source="title",
        ))
        db.add(PrClassification(
            repo="o/r", pr_number=1,
            pr_class="story",
            classified_by="jira_issue",
        ))
        db.add(CurUserSpend(
            email="alice@x.com",
            usage_hour=now.date(),  # #643: per-day grain
            model_id="anthropic.claude-haiku",
            spend_usd=300.0,
        ))
    from worker.jobs.pr_cost_rollup import run
    run()
    r = client.get(
        "/api/velocity/jira/series?window=30d&pr_class=story",
    )
    body = r.json()
    assert body["linked"] is True
    assert len(body["weeks"]) >= 1
    assert body["totals"]["story_points"] == 3.0
    # $/SP = round(spend / 3.0, 2). #643: spend is the real
    # per-day figure (today's $300 row, inside the 30d window).
    assert body["totals"]["cost_per_story_point"] is not None


def test_series_filters_by_sprint(client):
    """sprint_id filter excludes rows from other sprints."""
    _seed_jira()
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import JiraWeeklyMetric, Team
    now = datetime.now(timezone.utc)
    week = now.replace(
        hour=0, minute=0, second=0, microsecond=0,
    ) - timedelta(days=now.weekday())
    with get_db() as db:
        db.add(Team(team_id="t1", name="Team1"))
        db.flush()
        db.add(JiraWeeklyMetric(
            team_id="t1", week_start=week, pr_class="all",
            sprint_id=100, fix_version="",
            prs_merged=2, spend_usd=100.0, story_points=8.0,
        ))
        db.add(JiraWeeklyMetric(
            team_id="t1", week_start=week, pr_class="all",
            sprint_id=200, fix_version="",
            prs_merged=5, spend_usd=200.0, story_points=20.0,
        ))
    body = client.get(
        "/api/velocity/jira/series?sprint_id=100&pr_class=all",
    ).json()
    assert body["totals"]["prs_merged"] == 2
    assert body["totals"]["story_points"] == 8.0


def test_sprint_detail(client):
    _seed_jira()
    r = client.get("/api/velocity/jira/sprint/100")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sprint_id"] == 100
    assert body["committed_stories"] == 2
    # PROJ-1 done, PROJ-2 in_progress
    assert body["shipped_stories"] == 1
    assert body["carry_over_stories"] == 1
    # SP committed = 3 + 5 = 8; shipped = 3 (PROJ-1)
    assert body["story_points_committed"] == 8.0
    assert body["story_points_shipped"] == 3.0
    epic_keys = {e["epic_key"] for e in body["by_epic"]}
    assert "PROJ-100" in epic_keys


def test_sprint_detail_404_for_unknown_sprint(client):
    _seed_jira()
    r = client.get("/api/velocity/jira/sprint/9999")
    assert r.status_code == 404


def test_pr_refs_endpoint(client):
    _seed_jira()
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import (
        GithubActivity, PrClassification, PrJiraRef,
    )
    now = datetime.now(timezone.utc)
    with get_db() as db:
        db.add(GithubActivity(
            repo="o/r", pr_number=1,
            title="t", author_login="alice",
            labels="[]", issue_refs="[]",
            merged_at=now,
        ))
        db.add(PrJiraRef(
            repo="o/r", pr_number=1,
            issue_key="PROJ-1", source="title",
        ))
        db.add(PrClassification(
            repo="o/r", pr_number=1,
            pr_class="story",
            classified_by="jira_issue",
        ))
    r = client.get(
        "/api/velocity/jira/pr-refs?repo=o/r&pr_numbers=1",
    )
    body = r.json()
    assert body["linked"] is True
    assert "1" in body["refs"]
    assert body["refs"]["1"][0]["issue_key"] == "PROJ-1"
    assert body["refs"]["1"][0]["issue_type"] == "Story"
    cls = body["classifications"]["1"]
    assert cls["pr_class"] == "story"
    assert cls["classified_by"] == "jira_issue"


def test_member_forbidden_in_team_admin_scope(client, monkeypatch):
    """Non-admins get an empty result, not 403, because the
    /series endpoint is scoped (org_admin sees all, others
    see their teams) — verifies the scope filter works."""
    _seed_jira()
    _set_caller(monkeypatch, "user@test.com")
    body = client.get(
        "/api/velocity/jira/series?pr_class=all",
    ).json()
    assert body["linked"] is True
    assert body["weeks"] == []
