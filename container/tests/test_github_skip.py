"""
Tests for graceful "GitHub not configured" skip behavior in
the V&C worker pipeline (#278).

Customers who never configure the GitHub integration would
otherwise see a red `error` row every worker tick — the jobs
should detect "no PAT" and exit with status `skipped` so the
Jobs page renders neutral grey instead.

Covers:
  - github_sync: returns skipped:True when no PAT (real or
    placeholder) is stored.
  - pr_classify: skips when github_activity is empty AND no PAT.
  - pr_cost_rollup: same.
  - job_runner: persists status=skipped on the JobRun row.
"""
from __future__ import annotations
import json

import pytest


def _flag_on():
    """Seed enable_velocity_cost=true so the existing
    pre-#276 _flag_on() guard lets us reach the new skip
    paths. Once #276 lands, this fixture becomes a no-op."""
    from db.session import get_db
    from db.models import AdminConfig
    with get_db() as db:
        existing = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == "enable_velocity_cost")
            .first()
        )
        if existing:
            existing.value = "true"
        else:
            db.add(AdminConfig(
                key="enable_velocity_cost", value="true"))


def _seed_pat(value):
    """Write a PAT row to admin_config. `value` may be a JSON
    blob (matching the production shape) or a raw token; both
    are tolerated by _read_token() / _has_pat()."""
    from db.session import get_db
    from db.models import AdminConfig
    with get_db() as db:
        existing = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == "github_default_pat")
            .first()
        )
        if existing:
            existing.value = value
        else:
            db.add(AdminConfig(
                key="github_default_pat", value=value))


def _real_pat_payload():
    return json.dumps({
        "token": "ghp_realtoken_redacted",
        "connected_at": "2026-05-28T00:00:00+00:00",
        "rotated_by": "test",
    })


# ────────────────────────────────────────────────
# github_sync
# ────────────────────────────────────────────────

def test_github_sync_no_repos_is_not_an_error(clean_db, monkeypatch):
    """#1043: the global no-PAT skip is gone (it stopped public repos
    from ever syncing). With NO repos tracked, the run is a neutral
    'no repos configured' — still not a red error row (#278's intent),
    just decided by the repo list, not the token."""
    _flag_on()
    monkeypatch.setattr(
        "boto3.client",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("no aws"))
    )

    import worker.jobs.github_sync as gs
    out = gs.run()
    assert not out.get("skipped"), out
    assert out["detail"] == "no repos configured"


def test_github_sync_public_repo_syncs_without_token(
    clean_db, monkeypatch,
):
    """#1043 (the demo2 fix): a tracked public repo syncs ANONYMOUSLY
    when no org token is configured — no global skip. The probe says
    public, the fetch runs token-less, token_kind becomes 'public'."""
    _flag_on()
    monkeypatch.setattr(
        "boto3.client",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("no aws"))
    )
    from db.session import get_db
    from db.models import GithubRepo
    with get_db() as db:
        db.add(GithubRepo(
            repo="octo/public", host="github.com",
            path="octo/public", token_mode="auto",
            token_kind="unprobed",
        ))

    import worker.jobs.github_sync as gs
    # Probe → public; fetch runs token-less (capture the token).
    monkeypatch.setattr(gs, "_probe_public", lambda r: True)
    captured = {}

    def fake_fetch(repo, token, horizon):
        captured["token"] = token
        return []
    monkeypatch.setattr(gs, "_fetch_closed_prs", fake_fetch)

    out = gs.run()
    assert not out.get("skipped"), out
    # synced anonymously — no token handed to the fetch.
    assert captured.get("token") is None
    with get_db() as db:
        row = db.query(GithubRepo).filter(
            GithubRepo.repo == "octo/public").first()
        assert row.token_kind == "public"
        assert row.is_public is True


def test_github_sync_private_no_token_pauses_not_skips(
    clean_db, monkeypatch,
):
    """#1043: a private repo with no resolvable token is paused +
    token_kind='missing' — and the run does NOT global-skip."""
    _flag_on()
    monkeypatch.setattr(
        "boto3.client",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("no aws"))
    )
    from db.session import get_db
    from db.models import GithubRepo
    with get_db() as db:
        db.add(GithubRepo(
            repo="octo/private", host="github.com",
            path="octo/private", token_mode="auto",
            token_kind="unprobed",
        ))

    import worker.jobs.github_sync as gs
    monkeypatch.setattr(gs, "_probe_public", lambda r: False)

    out = gs.run()
    assert not out.get("skipped"), out
    with get_db() as db:
        row = db.query(GithubRepo).filter(
            GithubRepo.repo == "octo/private").first()
        assert row.token_kind == "missing"
        assert row.sync_status == "paused"


# ────────────────────────────────────────────────
# pr_classify
# ────────────────────────────────────────────────

def test_pr_classify_skips_when_empty_and_no_pat(clean_db):
    _flag_on()
    import worker.jobs.pr_classify as pc
    out = pc.run()
    assert out.get("skipped") is True, out
    assert out.get("skip_reason") == "no_github"


def test_pr_classify_runs_when_pat_present(clean_db):
    """A real PAT means GitHub WAS configured; even if
    activity is empty (e.g. brand-new install), pr_classify
    should run normally — the empty-activity case is fine
    for the regular flow (returns `0 classified`)."""
    _flag_on()
    _seed_pat(_real_pat_payload())
    import worker.jobs.pr_classify as pc
    out = pc.run()
    assert not out.get("skipped"), out


# ────────────────────────────────────────────────
# pr_cost_rollup
# ────────────────────────────────────────────────

def test_pr_cost_rollup_skips_when_empty_and_no_pat(clean_db):
    _flag_on()
    import worker.jobs.pr_cost_rollup as pcr
    out = pcr.run()
    assert out.get("skipped") is True, out
    assert out.get("skip_reason") == "no_github"


def test_pr_cost_rollup_runs_when_pat_present(clean_db):
    _flag_on()
    _seed_pat(_real_pat_payload())
    import worker.jobs.pr_cost_rollup as pcr
    out = pcr.run()
    assert not out.get("skipped"), out


# ────────────────────────────────────────────────
# job_runner persistence
# ────────────────────────────────────────────────

def test_job_runner_persists_skipped_status(clean_db):
    """When a wrapped job returns skipped:True, the JobRun
    row is written with status='skipped' (NOT 'succeeded') so
    the UI can render a neutral row, not a green OK."""
    from db.session import get_db
    from db.models import JobRun
    from worker.job_runner import job

    def fake_run():
        return {
            "detail": "skipped: no PAT",
            "skipped": True,
            "skip_reason": "no_pat",
        }

    wrapped = job("test_skipper", fake_run)
    out = wrapped(triggered_by="admin@test.com")
    assert out.get("skipped") is True

    with get_db() as db:
        rows = db.query(JobRun).filter(
            JobRun.job_name == "test_skipper").all()
        assert len(rows) == 1
        assert rows[0].status == "skipped"
        assert "skipped" in (rows[0].detail or "").lower()


def test_job_runner_succeeded_for_normal_result(clean_db):
    """Sanity: when result has no skipped flag, status is
    still 'succeeded' (no regression)."""
    from db.session import get_db
    from db.models import JobRun
    from worker.job_runner import job

    wrapped = job("test_normal", lambda: {"detail": "ok"})
    wrapped(triggered_by="admin@test.com")
    with get_db() as db:
        row = db.query(JobRun).filter(
            JobRun.job_name == "test_normal").first()
        assert row.status == "succeeded"
