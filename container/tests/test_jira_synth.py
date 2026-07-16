"""
Tests for the Jira synthetic-data generator (#347c).

Cover:
  - Plan shape: issue ratios, parent-epic linkage, sprint
    distribution, story-point ranges.
  - Idempotency: applying the same plan twice doesn't
    duplicate jira_issues or pr_jira_refs.
  - PR title rewriting: the generated key is threaded into
    the github_activity row so the next github_sync tick
    re-extracts it (the round-trip the ticket calls out).
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# jira_synth.py moved to internal/scripts/ (#530 — it's internal
# test-data tooling, not a customer/published script).
REPO = Path(__file__).resolve().parents[2]
SCRIPT_PY = REPO / "internal" / "scripts"
if str(SCRIPT_PY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PY))


def test_ratios_to_counts_sum_to_total():
    from jira_synth import _ratios_to_counts
    counts = dict(_ratios_to_counts(50))
    assert sum(counts.values()) == 50
    # Approximate ratios: story 50%, bug 30%, task 15%, epic 5%
    assert counts["Story"] == 25
    assert counts["Bug"]   == 15
    assert counts["Task"]  in (7, 8)
    # Epic absorbs rounding
    assert counts["Epic"]  >= 1


def test_generate_plan_shape():
    from jira_synth import generate_plan
    repos = [("o/r", i + 1) for i in range(20)]
    plan = generate_plan("PROJ", 30, repos)
    assert len(plan.issues) == 30
    # Every non-Epic issue should have a parent epic key
    # (generator parents via round-robin) and a sprint.
    for issue in plan.issues:
        if issue.issue_type == "Epic":
            assert issue.parent_epic_key is None
            assert issue.sprint_id is None
        else:
            assert issue.parent_epic_key is not None
            assert issue.sprint_id in (10001, 10002, 10003)
            assert issue.story_points is not None
            assert issue.story_points > 0
    # PR edits cover all 20 PRs
    assert len(plan.pr_edits) == 20
    issue_keys = {i.issue_key for i in plan.issues
                  if i.issue_type != "Epic"}
    for ed in plan.pr_edits:
        assert ed.issue_key in issue_keys
        assert ed.repo == "o/r"


def test_apply_mock_writes_and_is_idempotent(pg_url, clean_db):
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import (
        GithubActivity, JiraIssue, PrJiraRef,
    )
    import jira_synth

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

    plan = jira_synth.generate_plan(
        "PROJ", 10,
        [("o/r", n + 1) for n in range(5)],
    )

    r1 = jira_synth.apply_mock(plan)
    assert r1["issues_inserted"] == 10
    assert r1["refs_inserted"] == 5
    assert r1["prs_rewritten"] == 5

    # Idempotency: second apply should insert zero issues
    # and zero refs, but may update existing rows.
    r2 = jira_synth.apply_mock(plan)
    assert r2["issues_inserted"] == 0
    assert r2["issues_updated"] == 10
    assert r2["refs_inserted"] == 0
    assert r2["refs_existing"] == 5
    # Title was rewritten only on first apply (key was
    # already present on second pass).
    assert r2["prs_rewritten"] == 0

    with get_db() as db:
        assert db.query(JiraIssue).count() == 10
        assert db.query(PrJiraRef).count() == 5
        # Every PR title contains its assigned key now.
        prs = (
            db.query(GithubActivity)
            .order_by(GithubActivity.pr_number)
            .all()
        )
        for pr in prs:
            assert "PROJ-" in pr.title


def test_apply_mock_skips_pr_when_pr_row_missing(
    pg_url, clean_db,
):
    """If the planned (repo, pr_number) doesn't exist in
    github_activity yet, the title-rewrite step is a noop
    but the pr_jira_refs row still lands so a future PR
    sync hooks up cleanly."""
    from db.session import get_db
    from db.models import PrJiraRef
    import jira_synth

    plan = jira_synth.generate_plan(
        "PROJ", 6, [("o/r", 999)],
    )
    r = jira_synth.apply_mock(plan)
    assert r["refs_inserted"] == 1
    assert r["prs_rewritten"] == 0

    with get_db() as db:
        ref = db.query(PrJiraRef).first()
        assert ref.pr_number == 999


def test_cli_mock_runs_end_to_end(pg_url, clean_db, capsys):
    """Smoke: invoking the CLI's main() with --mock writes
    rows and prints valid JSON summary."""
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import GithubActivity, JiraIssue
    import jira_synth

    now = datetime.now(timezone.utc)
    with get_db() as db:
        db.add(GithubActivity(
            repo="o/r", pr_number=1,
            title="t", author_login="a",
            labels="[]", issue_refs="[]",
            merged_at=now,
        ))

    rc = jira_synth.main([
        "--mock", "--project", "FOO", "--count", "8",
    ])
    assert rc == 0
    captured = capsys.readouterr().out.strip()
    payload = json.loads(captured)
    assert payload["mode"] == "mock"
    assert payload["project"] == "FOO"
    assert payload["issues_planned"] == 8

    with get_db() as db:
        assert db.query(JiraIssue).count() == 8


def test_real_site_requires_token_env(monkeypatch, capsys):
    """The CLI should refuse --real-site when the named
    token env var is unset, rather than silently posting
    nothing."""
    import jira_synth
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    rc = jira_synth.main([
        "--real-site",
        "--site", "https://x.atlassian.net",
        "--auth-email", "ci@x.com",
        "--project", "FOO", "--count", "2",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "JIRA_TOKEN" in err
