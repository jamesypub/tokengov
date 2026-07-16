"""
Tests for the in-container Jira synth-seed worker job
(worker/jobs/jira_synth_seed.py). Mirrors the test
coverage of internal/scripts/jira_synth.py but exercises
the path actually invoked at runtime — via
/api/internal/run-job/jira_synth_seed.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

import pytest


def _enable_jira():
    """#447: jira_synth_seed.run() early-returns unless the
    runtime admin_config flag jira_enabled is on. Flip it for
    the tests that exercise the actual seed path."""
    from db.session import get_db
    from db.jira_feature import set_jira_enabled
    with get_db() as db:
        set_jira_enabled(db, True)


def test_ratios_to_counts_sum_to_total():
    from worker.jobs.jira_synth_seed import _ratios_to_counts
    counts = dict(_ratios_to_counts(50))
    assert sum(counts.values()) == 50
    assert counts["Story"] == 25
    assert counts["Bug"]   == 15
    assert counts["Task"]  in (7, 8)
    assert counts["Epic"]  >= 1


def test_generate_plan_shape():
    from worker.jobs.jira_synth_seed import generate_plan
    repos = [("o/r", i + 1) for i in range(20)]
    issues, edits = generate_plan("PROJ", 30, repos)
    assert len(issues) == 30
    for ip in issues:
        if ip.issue_type == "Epic":
            assert ip.parent_epic_key is None
            assert ip.sprint_id is None
        else:
            assert ip.parent_epic_key is not None
            assert ip.sprint_id in (10001, 10002, 10003)
            assert ip.story_points is not None
            assert ip.story_points > 0
    assert len(edits) == 20
    nonepic_keys = {
        i.issue_key for i in issues
        if i.issue_type != "Epic"
    }
    for ed in edits:
        assert ed.issue_key in nonepic_keys


def test_run_writes_and_is_idempotent(pg_url, clean_db):
    """Full /run path: insert PRs → run() → assert
    rows; second run() upserts only."""
    from db.session import get_db
    from db.models import (
        GithubActivity, JiraIssue, PrJiraRef,
    )
    from worker.jobs.jira_synth_seed import run

    _enable_jira()
    now = datetime.now(timezone.utc)
    with get_db() as db:
        for n in range(5):
            db.add(GithubActivity(
                repo="o/r", pr_number=n + 1,
                title=f"original PR {n+1}",
                author_login="alice",
                labels="[]", issue_refs="[]",
                merged_at=now,
            ))

    r1 = run()
    # Defaults → 50 issues; first 5 PRs get refs.
    assert r1["issues_inserted"] == 50
    assert r1["refs_inserted"]   == 5
    assert r1["prs_rewritten"]   == 5

    # Idempotent
    r2 = run()
    assert r2["issues_inserted"] == 0
    assert r2["issues_updated"]  == 50
    assert r2["refs_inserted"]   == 0
    assert r2["refs_existing"]   == 5
    assert r2["prs_rewritten"]   == 0

    with get_db() as db:
        assert db.query(JiraIssue).count() == 50
        assert db.query(PrJiraRef).count() == 5
        prs = (
            db.query(GithubActivity)
            .order_by(GithubActivity.pr_number)
            .all()
        )
        for pr in prs:
            assert "PROJ-" in pr.title


def test_run_creates_synthetic_site_ok(pg_url, clean_db):
    """run() must upsert ONE synthetic jira_sites row with
    sync_status='ok' so the V&C Jira surface (_jira_linked)
    renders the seeded data. Stage has no DB-write path, so
    the seed job itself has to create the link row."""
    from db.session import get_db
    from db.models import JiraSite
    from worker.jobs.jira_synth_seed import (
        run, SYNTH_SITE_URL, SYNTH_ADDED_BY,
    )

    _enable_jira()
    r = run()
    assert r["site_action"] == "inserted"

    with get_db() as db:
        sites = db.query(JiraSite).all()
        assert len(sites) == 1
        s = sites[0]
        assert s.site_url == SYNTH_SITE_URL
        assert s.sync_status == "ok"
        assert s.added_by == SYNTH_ADDED_BY
        assert json.loads(s.projects) == ["PROJ"]


def test_run_synthetic_site_idempotent(pg_url, clean_db):
    """Re-running must not create a duplicate jira_sites row,
    and must restore sync_status='ok' if a prior tick flipped
    it (defends against the auth_failed regression)."""
    from db.session import get_db
    from db.models import JiraSite
    from worker.jobs.jira_synth_seed import run, SYNTH_SITE_URL

    _enable_jira()
    assert run()["site_action"] == "inserted"

    # Simulate a stray tick / manual poke flipping status.
    with get_db() as db:
        db.query(JiraSite).filter(
            JiraSite.site_url == SYNTH_SITE_URL
        ).first().sync_status = "auth_failed"

    r2 = run()
    assert r2["site_action"] == "updated"
    with get_db() as db:
        rows = db.query(JiraSite).filter(
            JiraSite.site_url == SYNTH_SITE_URL
        ).all()
        assert len(rows) == 1
        assert rows[0].sync_status == "ok"


def test_synthetic_site_uses_configured_project(
    pg_url, clean_db, monkeypatch,
):
    from db.session import get_db
    from db.models import JiraSite
    from worker.jobs.jira_synth_seed import run, SYNTH_SITE_URL

    monkeypatch.setenv("TG_JIRA_SYNTH_PROJECT", "DATA")
    _enable_jira()
    run()
    with get_db() as db:
        s = db.query(JiraSite).filter(
            JiraSite.site_url == SYNTH_SITE_URL
        ).first()
        assert json.loads(s.projects) == ["DATA"]


def test_run_respects_env_overrides(
    pg_url, clean_db, monkeypatch,
):
    from db.session import get_db
    from db.models import GithubActivity, JiraIssue
    from worker.jobs.jira_synth_seed import run

    monkeypatch.setenv("TG_JIRA_SYNTH_PROJECT", "DATA")
    monkeypatch.setenv("TG_JIRA_SYNTH_COUNT", "10")

    _enable_jira()
    now = datetime.now(timezone.utc)
    with get_db() as db:
        db.add(GithubActivity(
            repo="o/r", pr_number=1,
            title="t", author_login="a",
            labels="[]", issue_refs="[]",
            merged_at=now,
        ))

    r = run()
    assert "project=DATA" in r["detail"]
    with get_db() as db:
        keys = {
            i.issue_key for i in db.query(JiraIssue).all()
        }
        assert all(k.startswith("DATA-") for k in keys)
        assert len(keys) == 10


def test_internal_run_job_endpoint_dispatches(
    pg_url, clean_db, monkeypatch,
):
    """The /api/internal/run-job/jira_synth_seed surface
    used by the bash wrapper actually dispatches the
    worker job and returns 200."""
    from fastapi.testclient import TestClient
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import (
        AdminRole, GithubActivity, JiraIssue,
    )
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"),
    )
    monkeypatch.setenv("TG_JIRA_SYNTH_COUNT", "8")

    _enable_jira()
    now = datetime.now(timezone.utc)
    with get_db() as db:
        db.add(AdminRole(
            email="admin@test.com", role="org_admin"))
        db.add(GithubActivity(
            repo="o/r", pr_number=1,
            title="t", author_login="a",
            labels="[]", issue_refs="[]",
            merged_at=now,
        ))

    from api.main import app
    with TestClient(app) as c:
        r = c.post("/internal/run-job/jira_synth_seed")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"

    with get_db() as db:
        assert db.query(JiraIssue).count() == 8


def test_run_skips_when_jira_disabled(pg_url, clean_db):
    """#447: with the runtime flag OFF (default), jira_synth_seed
    early-returns and seeds nothing — even if PRs exist."""
    from db.session import get_db
    from db.models import GithubActivity, JiraIssue, JiraSite
    from worker.jobs.jira_synth_seed import run

    now = datetime.now(timezone.utc)
    with get_db() as db:
        db.add(GithubActivity(
            repo="o/r", pr_number=1,
            title="t", author_login="a",
            labels="[]", issue_refs="[]",
            merged_at=now,
        ))

    r = run()
    assert r.get("skipped") is True
    assert r.get("skip_reason") == "jira_disabled"
    with get_db() as db:
        assert db.query(JiraIssue).count() == 0
        assert db.query(JiraSite).count() == 0
