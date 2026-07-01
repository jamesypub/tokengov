"""
Tests for worker.jobs.vc_seed and vc_seed_synthetic — the V&C
demo seeders that the populate script now invokes via
/api/jobs/run on both local and remote (ECS) installs (#251).

These exercise the same DB code path that runs in production
(no docker-compose plumbing), so a green test means populate
will work against any reachable api.
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest


def _seed_team_and_members(team_id: str, n_members: int = 3):
    """Layer A/B both attribute PRs to users with email pattern
    '<team_id>-member-<N>@example.com' and team_id set on User.
    Mirror that here so the seeders find the test fixtures."""
    from db.session import get_db
    from db.models import Team, User
    with get_db() as db:
        if not db.query(Team).filter(
                Team.team_id == team_id).first():
            db.add(Team(team_id=team_id, name=team_id))
        for i in range(1, n_members + 1):
            email = f"{team_id}-member-{i}@example.com"
            if not db.query(User).filter(
                    User.email == email).first():
                db.add(User(
                    email=email,
                    team_id=team_id,
                    status="active",
                ))


@pytest.fixture
def vc_seed_teams(clean_db):
    """All 14 teams the seeders expect, with members."""
    layer_a = [
        "team-1", "team-2", "team-3",
        "team-1.1", "team-1.1.1",
    ]
    layer_b = [
        "team-1.2", "team-1.3",
        "team-2.1", "team-2.2", "team-2.3",
        "team-3.1", "team-3.2", "team-3.3",
        "team-1.1.2",
    ]
    for tid in layer_a + layer_b:
        _seed_team_and_members(tid)


def test_vc_seed_layer_a_inserts_activity_and_links(
    vc_seed_teams,
):
    """Layer A reads the bundled cache and writes
    github_activity + github_repos + linked_accounts."""
    import worker.jobs.vc_seed as vs
    out = vs.run()

    # Cache must exist in the test tree (committed fixture).
    fixtures = Path(__file__).resolve().parent / "fixtures"
    assert (fixtures / "public_repo_seed.json").is_file(), (
        "test fixture missing — Layer A requires it"
    )

    assert out["inserted"] >= 1, out
    assert "Layer A" in out["detail"]

    from db.session import get_db
    from db.models import (
        AdminConfig, GithubActivity, GithubRepo,
    )
    with get_db() as db:
        # github_repos got the 5 anchor repos.
        repos = {r.repo for r in db.query(GithubRepo).all()}
        assert "facebook/react" in repos
        assert "vercel/next.js" in repos
        assert "microsoft/typescript" in repos
        assert "microsoft/vscode" in repos
        assert "pytorch/pytorch" in repos

        # github_label_map got written and parses as JSON.
        lmap = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == "github_label_map")
            .first()
        )
        assert lmap is not None
        parsed = json.loads(lmap.value)
        assert "story" in parsed and "bug" in parsed

        n_act = db.query(GithubActivity).count()
        assert n_act >= 1


def test_vc_seed_is_idempotent(vc_seed_teams):
    """Re-running vc_seed.run() upserts; row counts don't grow."""
    import worker.jobs.vc_seed as vs
    vs.run()
    from db.session import get_db
    from db.models import GithubActivity
    with get_db() as db:
        n1 = db.query(GithubActivity).count()
    vs.run()
    with get_db() as db:
        n2 = db.query(GithubActivity).count()
    assert n1 == n2, (
        f"expected idempotent run; before={n1} after={n2}"
    )


def test_vc_seed_synthetic_writes_synthetic_activity(
    vc_seed_teams,
):
    """Layer B inserts tenant/* PRs + 'synthetic' quota_metrics."""
    import worker.jobs.vc_seed as vs
    import worker.jobs.vc_seed_synthetic as vss
    vs.run()
    out = vss.run()

    assert out["inserted"] >= 1, out
    assert "Layer B" in out["detail"]

    from db.session import get_db
    from db.models import GithubActivity, CurUserSpend
    with get_db() as db:
        synth = (
            db.query(GithubActivity)
            .filter(GithubActivity.repo.like("tenant/%"))
            .count()
        )
        assert synth >= 1

        synth_qm = (
            db.query(CurUserSpend)
            .filter(CurUserSpend.model_id == "synthetic")
            .count()
        )
        # Both Layer B (own teams) + Layer A (spend injection)
        # write 'synthetic' CurUserSpend rows.
        assert synth_qm >= 1


def test_vc_seed_synthetic_resets_synthetic_rows(
    vc_seed_teams,
):
    """Layer B wipes 'synthetic' rows on every run, so two
    consecutive runs land on the same row count (within RNG
    determinism — seed=42)."""
    import worker.jobs.vc_seed as vs
    import worker.jobs.vc_seed_synthetic as vss
    vs.run()
    out1 = vss.run()
    out2 = vss.run()
    assert out1["inserted"] == out2["inserted"], (
        f"non-idempotent: {out1['inserted']} vs "
        f"{out2['inserted']}"
    )


def test_vc_seed_jobs_registered_in_api():
    """/api/jobs/run must list the new jobs so the populate
    script can invoke them via HTTP — that is the only path
    that works for remote (ECS) installs (#251)."""
    from api.routes.jobs import _JOBS_BY_NAME
    assert "vc_seed" in _JOBS_BY_NAME
    assert "vc_seed_synthetic" in _JOBS_BY_NAME
    # Module path resolves.
    mod_name, fn_name = _JOBS_BY_NAME["vc_seed"]
    assert mod_name == "worker.jobs.vc_seed"
    assert fn_name == "run"
