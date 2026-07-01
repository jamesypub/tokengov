"""
Unit tests for the Jira issue-key extractor and the
extension to github_sync that records pr_jira_refs.
"""
from __future__ import annotations
import json
import pytest


def test_extract_keys_basic():
    from worker.jobs.github_sync import extract_jira_keys
    keys = extract_jira_keys(
        "Refs PROJ-12 and DATA-7 only",
        {"PROJ", "DATA"},
    )
    assert keys == ["PROJ-12", "DATA-7"]


def test_extract_keys_filters_unallowed_projects():
    """K8S-2024 must not surface when K8S isn't allowlisted."""
    from worker.jobs.github_sync import extract_jira_keys
    keys = extract_jira_keys(
        "K8S-2024 release notes; PROJ-1 is the real link",
        {"PROJ"},
    )
    assert keys == ["PROJ-1"]


def test_extract_keys_dedup_preserves_order():
    from worker.jobs.github_sync import extract_jira_keys
    keys = extract_jira_keys(
        "PROJ-1 then PROJ-2 then PROJ-1 again",
        {"PROJ"},
    )
    assert keys == ["PROJ-1", "PROJ-2"]


def test_extract_keys_empty_inputs():
    from worker.jobs.github_sync import extract_jira_keys
    assert extract_jira_keys(None, {"PROJ"}) == []
    assert extract_jira_keys("", {"PROJ"}) == []
    # No allowlist == no extraction
    assert extract_jira_keys("PROJ-1", set()) == []


def test_extract_keys_branch_format():
    """Branch names like `feat/PROJ-7-fix` should still work."""
    from worker.jobs.github_sync import extract_jira_keys
    keys = extract_jira_keys(
        "feat/PROJ-7-some-slug",
        {"PROJ"},
    )
    assert keys == ["PROJ-7"]


def test_record_jira_refs_writes_per_source(pg_url, clean_db):
    """Title hit takes precedence over body, body over branch."""
    from db.session import get_db
    from db.models import PrJiraRef
    from worker.jobs.github_sync import _record_jira_refs

    with get_db() as db:
        n = _record_jira_refs(
            db,
            repo_full_name="owner/repo",
            pr_number=1,
            title="Fix login flow PROJ-1",
            body="Also closes PROJ-2",
            branch="feat/PROJ-3-x",
            gh_number=1,
            token=None,
            allowed={"PROJ"},
        )
        assert n == 3

    with get_db() as db:
        rows = (
            db.query(PrJiraRef)
            .filter(PrJiraRef.repo == "owner/repo")
            .all()
        )
        by_key = {r.issue_key: r.source for r in rows}
        assert by_key["PROJ-1"] == "title"
        assert by_key["PROJ-2"] == "body"
        assert by_key["PROJ-3"] == "branch"


def test_record_jira_refs_idempotent(pg_url, clean_db):
    from db.session import get_db
    from db.models import PrJiraRef
    from worker.jobs.github_sync import _record_jira_refs

    for _ in range(3):
        with get_db() as db:
            _record_jira_refs(
                db, "owner/r", 1, "PROJ-1", None, None,
                1, None, {"PROJ"},
            )

    with get_db() as db:
        rows = (
            db.query(PrJiraRef)
            .filter(PrJiraRef.repo == "owner/r")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].issue_key == "PROJ-1"


def test_jira_project_keys_falls_back_to_env(pg_url, clean_db, monkeypatch):
    from db.session import get_db
    from worker.jobs.github_sync import _jira_project_keys

    monkeypatch.setenv("TG_JIRA_PROJECTS", "ALPHA, BETA")
    with get_db() as db:
        keys = _jira_project_keys(db)
    assert keys == {"ALPHA", "BETA"}


def test_jira_project_keys_uses_sites_when_present(pg_url, clean_db):
    from db.session import get_db
    from db.models import JiraSite
    from worker.jobs.github_sync import _jira_project_keys

    with get_db() as db:
        db.add(JiraSite(
            site_url="https://x.atlassian.net",
            auth_email="a@x.com",
            projects=json.dumps(["FOO", "BAR"]),
            added_by="t@x.com",
        ))

    with get_db() as db:
        keys = _jira_project_keys(db)
    assert keys == {"FOO", "BAR"}
