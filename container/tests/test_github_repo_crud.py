"""
Tests for POST / PATCH / DELETE /api/integrations/github/repos
(ticket #262).
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def clean_github(pg_url):
    """Truncate github tables between tests."""
    import db.session as _dbs
    from sqlalchemy import text
    with _dbs.engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE TABLE github_repos, github_activity "
            "RESTART IDENTITY CASCADE"
        ))
    yield


@pytest.fixture
def client(pg_url, clean_db, clean_github, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"),
    )
    from db.session import get_db
    from db.models import AdminRole, Team
    with get_db() as db:
        db.add(AdminRole(
            email="admin@test.com", role="org_admin",
        ))
        db.add(Team(team_id="t1", name="Team1"))

    from api.main import app
    with TestClient(app) as c:
        yield c


def test_post_repo(client):
    r = client.post(
        "/api/integrations/github/repos",
        json={"repo": "owner/test", "team_id": None},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["repo"] == "owner/test"
    assert body["team_id"] is None

    listed = client.get(
        "/api/integrations/github/repos",
    ).json()
    repos = [row["repo"] for row in listed]
    assert "owner/test" in repos


def test_post_repo_duplicate_409(client):
    client.post(
        "/api/integrations/github/repos",
        json={"repo": "owner/dup"},
    )
    r = client.post(
        "/api/integrations/github/repos",
        json={"repo": "owner/dup"},
    )
    assert r.status_code == 409


def test_post_repo_invalid_format(client):
    for bad in ["noslash", "trailing/", "/leading", ""]:
        r = client.post(
            "/api/integrations/github/repos",
            json={"repo": bad},
        )
        assert r.status_code == 400, \
            f"expected 400 for {bad!r}, got {r.status_code}"


# ───────────────── #1042: URL-first input ─────────────────

def test_post_repo_full_github_url(client):
    r = client.post(
        "/api/integrations/github/repos",
        json={"repo": "https://github.com/NVIDIA/SkillSpector"},
    )
    assert r.status_code == 201
    body = r.json()
    # github.com rows stay BARE owner/name (PK = REST path + activity
    # join key + parity with legacy rows); host/path carry the identity.
    assert body["repo"] == "NVIDIA/SkillSpector"
    assert body["host"] == "github.com"
    assert body["path"] == "NVIDIA/SkillSpector"
    assert body["is_github"] is True
    assert body["sync_status"] == "ok"


@pytest.mark.parametrize("raw", [
    "https://github.com/NVIDIA/SkillSpector.git",
    "https://github.com/NVIDIA/SkillSpector/tree/main",
    "git@github.com:NVIDIA/SkillSpector.git",
])
def test_post_repo_url_variants_canonical(client, raw):
    r = client.post(
        "/api/integrations/github/repos",
        json={"repo": raw},
    )
    assert r.status_code == 201, r.text
    assert r.json()["repo"] == "NVIDIA/SkillSpector"


def test_post_repo_shorthand_still_works(client):
    r = client.post(
        "/api/integrations/github/repos",
        json={"repo": "NVIDIA/SkillSpector"},
    )
    assert r.status_code == 201
    assert r.json()["repo"] == "NVIDIA/SkillSpector"


def test_post_repo_url_dedups_against_shorthand(client):
    # A pasted URL and the bare shorthand are the same repo — second
    # add is a 409, not a duplicate row.
    client.post(
        "/api/integrations/github/repos",
        json={"repo": "NVIDIA/SkillSpector"},
    )
    r = client.post(
        "/api/integrations/github/repos",
        json={"repo": "https://github.com/NVIDIA/SkillSpector/tree/main"},
    )
    assert r.status_code == 409


def test_post_repo_gitlab_subgroup_paused(client):
    r = client.post(
        "/api/integrations/github/repos",
        json={"repo": "https://gitlab.example.com/team/sub/proj"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["host"] == "gitlab.example.com"
    assert body["path"] == "team/sub/proj"
    assert body["is_github"] is False
    # Stored but parked — sync is github.com-gated.
    assert body["sync_status"] == "paused"
    assert body["token_kind"] == "missing"


def test_post_repo_specific_error_not_owner_name(client):
    r = client.post(
        "/api/integrations/github/repos",
        json={"repo": "https://github.com/justone"},
    )
    assert r.status_code == 400
    # Specific reason, not the old dead-end "must be owner/name".
    assert "owner/name" in r.json()["detail"]


def test_patch_repo(client):
    client.post(
        "/api/integrations/github/repos",
        json={"repo": "owner/patch-me"},
    )
    r = client.patch(
        "/api/integrations/github/repos/owner/patch-me",
        json={"team_id": "t1"},
    )
    assert r.status_code == 200
    assert r.json()["team_id"] == "t1"


def test_patch_repo_404(client):
    r = client.patch(
        "/api/integrations/github/repos/nobody/nope",
        json={"team_id": None},
    )
    assert r.status_code == 404


def test_delete_repo(client):
    from db.session import get_db
    from db.models import GithubActivity
    client.post(
        "/api/integrations/github/repos",
        json={"repo": "owner/gone"},
    )
    with get_db() as db:
        import datetime as _dt
        db.add(GithubActivity(
            repo="owner/gone",
            pr_number=1,
            title="old PR",
            author_login="alice",
            merged_at=_dt.datetime.now(
                _dt.timezone.utc,
            ),
        ))

    r = client.delete(
        "/api/integrations/github/repos/owner/gone",
    )
    assert r.status_code == 200
    assert r.json()["detail"] == "deleted"

    listed = client.get(
        "/api/integrations/github/repos",
    ).json()
    assert all(row["repo"] != "owner/gone" for row in listed)

    with get_db() as db:
        count = db.query(GithubActivity).filter(
            GithubActivity.repo == "owner/gone",
        ).count()
    assert count == 1, \
        "github_activity rows must survive repo deletion"


def test_delete_repo_404(client):
    r = client.delete(
        "/api/integrations/github/repos/nobody/nope",
    )
    assert r.status_code == 404
