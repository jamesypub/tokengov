"""
Verifies the Jira-priority probe in pr_classify.

The full classify chain is now:
  jira_issue → issue_link → pr_label → fallback

These tests check (a) Jira wins when present, (b) fallback
to GitHub labels when no Jira ref, and (c) probe_trace
records the chain.
"""
from __future__ import annotations
import json
import pytest


def _classify(pr_dict, jira_lookup=None, jira_mapping=None):
    """Adapter — calls classify_one with empty issue-label
    lookup so we focus on the new probe."""
    from worker.jobs.pr_classify import classify_one
    lmap = {
        "story": ["enhancement"],
        "bug":   ["bug"],
        "task":  ["chore"],
    }
    return classify_one(
        pr_dict, lambda r, n: None, lmap,
        jira_lookup=jira_lookup,
        jira_mapping=jira_mapping,
    )


def test_jira_story_wins_over_pr_label():
    """Jira says Story, PR label says bug → story wins."""
    def lookup(key):
        return "Story" if key == "PROJ-1" else None

    verdict = _classify(
        {
            "repo": "o/r", "pr_number": 1,
            "labels": ["bug"],
            "issue_refs": [],
            "jira_keys": ["PROJ-1"],
        },
        jira_lookup=lookup,
    )
    assert verdict["pr_class"] == "story"
    assert verdict["classified_by"] == "jira_issue"
    probes = verdict["probe_trace"]["probes"]
    assert any(
        p.get("probe") == "jira_issue" and p.get("result") == "hit"
        for p in probes
    )


def test_jira_bug_classifies_bug():
    def lookup(key):
        return "Bug" if key == "PROJ-2" else None
    verdict = _classify(
        {
            "repo": "o/r", "pr_number": 2,
            "labels": [],
            "issue_refs": [],
            "jira_keys": ["PROJ-2"],
        },
        jira_lookup=lookup,
    )
    assert verdict["pr_class"] == "bug"
    assert verdict["classified_by"] == "jira_issue"


def test_unknown_jira_type_falls_through_to_pr_label():
    """If the Jira issue type doesn't match any class, the
    Jira probe records `no_class_match` and we fall through
    to PR labels."""
    def lookup(key):
        return "Discovery" if key == "PROJ-3" else None
    verdict = _classify(
        {
            "repo": "o/r", "pr_number": 3,
            "labels": ["bug"],
            "issue_refs": [],
            "jira_keys": ["PROJ-3"],
        },
        jira_lookup=lookup,
    )
    assert verdict["pr_class"] == "bug"
    assert verdict["classified_by"] == "pr_label"
    probes = verdict["probe_trace"]["probes"]
    assert any(p.get("probe") == "jira_issue"
               and p.get("result") == "no_class_match" for p in probes)
    assert any(p.get("probe") == "pr_label"
               and p.get("result") == "hit" for p in probes)


def test_no_jira_ref_falls_back_to_existing_chain():
    """No Jira keys at all → behaves identically to v1."""
    verdict = _classify({
        "repo": "o/r", "pr_number": 4,
        "labels": ["enhancement"],
        "issue_refs": [],
        "jira_keys": [],
    }, jira_lookup=lambda k: None)
    assert verdict["pr_class"] == "story"
    assert verdict["classified_by"] == "pr_label"


def test_first_match_wins_on_multiple_jira_keys():
    """First key with a class match wins; remaining keys
    are recorded as ignored_first_wins in the trace."""
    def lookup(key):
        return {"PROJ-1": "Story", "PROJ-2": "Bug"}.get(key)
    verdict = _classify(
        {
            "repo": "o/r", "pr_number": 5,
            "labels": [],
            "issue_refs": [],
            "jira_keys": ["PROJ-1", "PROJ-2"],
        },
        jira_lookup=lookup,
    )
    assert verdict["pr_class"] == "story"
    probes = verdict["probe_trace"]["probes"]
    ignored = [
        p for p in probes
        if p.get("probe") == "jira_issue"
        and p.get("result") == "ignored_first_wins"
    ]
    assert len(ignored) == 1
    assert ignored[0]["ref"] == "PROJ-2"


def test_run_uses_jira_when_pr_has_ref(pg_url, clean_db):
    """End-to-end: pr_classify.run wires JiraIssue + PrJiraRef
    into the verdict for a real github_activity row."""
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import (
        GithubActivity, JiraIssue, PrJiraRef, PrClassification,
    )
    from worker.jobs.pr_classify import run

    now = datetime.now(timezone.utc)
    with get_db() as db:
        db.add(GithubActivity(
            repo="owner/r", pr_number=42,
            title="something", author_login="alice",
            labels=json.dumps(["bug"]),  # PR-label says bug
            issue_refs="[]",
            merged_at=now,
        ))
        db.add(PrJiraRef(
            repo="owner/r", pr_number=42,
            issue_key="PROJ-9", source="title",
        ))
        db.add(JiraIssue(
            issue_key="PROJ-9",
            issue_type="Story",
            status="Done", status_category="done",
            jira_created_at=now, jira_updated_at=now,
        ))

    run()

    with get_db() as db:
        row = (
            db.query(PrClassification)
            .filter(PrClassification.repo == "owner/r")
            .filter(PrClassification.pr_number == 42)
            .first()
        )
        assert row is not None
        assert row.pr_class == "story"
        assert row.classified_by == "jira_issue"
