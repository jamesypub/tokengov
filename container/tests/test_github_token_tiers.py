"""#1043: per-repo token tier resolver + route token surface.

Covers the 3-tier resolution (public→anon, override, org default),
the cross-tenant fail-safe (probe BEFORE org default), rate-limit vs
auth split, and the API never returning the token.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ───────────────── resolver unit tests (no DB needed) ─────────────────

class _Row:
    """Minimal stand-in for a GithubRepo (resolver only reads a few
    attrs + mutates is_public/last_probed_at)."""
    def __init__(self, **kw):
        self.repo = kw.get("repo", "octo/x")
        self.token_mode = kw.get("token_mode", "auto")
        self.is_public = kw.get("is_public")
        self.last_probed_at = None
        self.pat_secret_arn = kw.get("pat_secret_arn")
        self.pat_plain = kw.get("pat_plain")


def test_resolve_force_public_is_anonymous(monkeypatch):
    import worker.jobs.github_sync as gs
    # Even if an org token exists, public mode never uses it.
    monkeypatch.setattr(gs, "_read_token", lambda: "ghp_org")
    res = gs._resolve_repo_token(_Row(token_mode="public"))
    assert res["token"] is None
    assert res["token_kind"] == "public"
    assert res["paused"] is False


def test_resolve_override_uses_repo_pat(monkeypatch):
    import worker.jobs.github_sync as gs
    res = gs._resolve_repo_token(
        _Row(token_mode="override", pat_plain="ghp_repo"))
    assert res["token"] == "ghp_repo"
    assert res["token_kind"] == "override"


def test_resolve_override_missing_pauses(monkeypatch):
    import worker.jobs.github_sync as gs
    res = gs._resolve_repo_token(_Row(token_mode="override"))
    assert res["token"] is None
    assert res["token_kind"] == "missing"
    assert res["paused"] is True


def test_resolve_auto_public_probe_anonymous(monkeypatch):
    import worker.jobs.github_sync as gs
    monkeypatch.setattr(gs, "_probe_public", lambda r: True)
    monkeypatch.setattr(gs, "_read_token", lambda: "ghp_org")
    row = _Row(token_mode="auto", is_public=None)
    res = gs._resolve_repo_token(row)
    assert res["token"] is None          # anonymous, NOT the org token
    assert res["token_kind"] == "public"
    assert row.is_public is True          # cached


def test_resolve_auto_private_uses_org_default(monkeypatch):
    import worker.jobs.github_sync as gs
    monkeypatch.setattr(gs, "_probe_public", lambda r: False)
    monkeypatch.setattr(gs, "_read_token", lambda: "ghp_org")
    res = gs._resolve_repo_token(_Row(token_mode="auto", is_public=None))
    assert res["token"] == "ghp_org"
    assert res["token_kind"] == "org"


def test_resolve_auto_private_no_token_pauses(monkeypatch):
    import worker.jobs.github_sync as gs
    monkeypatch.setattr(gs, "_probe_public", lambda r: False)
    monkeypatch.setattr(gs, "_read_token", lambda: None)
    res = gs._resolve_repo_token(_Row(token_mode="auto", is_public=None))
    assert res["token"] is None
    assert res["token_kind"] == "missing"
    assert res["paused"] is True


def test_resolve_probes_before_org_default_failsafe(monkeypatch):
    """THE load-bearing cross-tenant test: an unprobed row with an org
    token present must be PROBED before the org token is handed over —
    a private row must never fall through to another owner's PAT
    unprobed."""
    import worker.jobs.github_sync as gs
    order = []
    monkeypatch.setattr(
        gs, "_read_token",
        lambda: (order.append("read_token"), "ghp_org")[1])

    def probe(r):
        order.append("probe")
        return False  # private
    monkeypatch.setattr(gs, "_probe_public", probe)

    gs._resolve_repo_token(_Row(token_mode="auto", is_public=None))
    # probe happened, and it happened BEFORE the org token was read.
    assert "probe" in order
    assert order.index("probe") < order.index("read_token")


def test_resolve_inconclusive_probe_does_not_fall_through(monkeypatch):
    """Probe returns None (rate-limited/transient) → do NOT use the org
    token; leave unprobed + paused, re-probe next run."""
    import worker.jobs.github_sync as gs
    monkeypatch.setattr(gs, "_probe_public", lambda r: None)
    monkeypatch.setattr(gs, "_read_token", lambda: "ghp_org")
    res = gs._resolve_repo_token(_Row(token_mode="auto", is_public=None))
    assert res["token"] is None
    assert res["token_kind"] == "unprobed"
    assert res["paused"] is True


def test_rate_limit_detection():
    import urllib.error
    import worker.jobs.github_sync as gs

    class _H:
        def __init__(self, d):
            self._d = d

        def get(self, k):
            return self._d.get(k)

    def err(code, headers):
        e = urllib.error.HTTPError(
            "u", code, "msg", None, None)
        e.headers = _H(headers)
        return e

    # 403 with remaining=0 → rate limited
    assert gs._is_rate_limited(err(403, {"X-RateLimit-Remaining": "0"}))
    # 429 → rate limited
    assert gs._is_rate_limited(err(429, {}))
    # 403 with a Retry-After → rate limited
    assert gs._is_rate_limited(err(403, {"Retry-After": "60"}))
    # 401 → NOT rate limited (auth failure)
    assert not gs._is_rate_limited(err(401, {}))
    # 403 with remaining left → not a rate limit
    assert not gs._is_rate_limited(
        err(403, {"X-RateLimit-Remaining": "55"}))


# ───────── #1050: API path derived from owner/name, not host key ─────────

def test_api_path_strips_host_prefix():
    import worker.jobs.github_sync as gs
    # host-prefixed canonical key → bare owner/name for the REST API.
    assert gs._api_path("github.com/anthropics/skills") == "anthropics/skills"
    # bare legacy key → unchanged.
    assert gs._api_path("NVIDIA/SkillSpector") == "NVIDIA/SkillSpector"


def test_probe_uses_api_path_for_host_prefixed_row(monkeypatch):
    """The visibility probe must build /repos/owner/name (200), not
    /repos/github.com/owner/name (404 → mis-flagged private)."""
    import worker.jobs.github_sync as gs
    seen = {}

    def fake_probe(path):
        seen["path"] = path
        return True  # public
    monkeypatch.setattr(gs, "_probe_public", fake_probe)

    row = _Row(repo="github.com/anthropics/skills",
               token_mode="auto", is_public=None)
    res = gs._resolve_repo_token(row)
    assert seen["path"] == "anthropics/skills"   # NOT github.com/...
    assert res["token_kind"] == "public"


def test_sync_one_repo_fetches_api_path_keeps_db_key(monkeypatch):
    """_sync_one_repo fetches the owner/name path but keeps DB rows
    keyed on the canonical host-prefixed key (no history split)."""
    import worker.jobs.github_sync as gs
    captured = {}

    def fake_fetch(api_path, token, horizon):
        captured["api_path"] = api_path
        return []  # no PRs → no upserts, just exercises path derivation
    monkeypatch.setattr(gs, "_fetch_closed_prs", fake_fetch)

    res = gs._sync_one_repo("github.com/anthropics/skills", None)
    assert captured["api_path"] == "anthropics/skills"
    assert res["status"] == "ok"


# ───────────────── route: token never serialized ─────────────────

@pytest.fixture
def clean_github(pg_url):
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
    # No add-time network probe in tests — treat every repo as unprobed.
    import api.routes.integrations_github as ig
    monkeypatch.setattr(ig, "_probe_public_visibility", lambda r: None)
    monkeypatch.setattr(ig, "_org_default_present", lambda: False)
    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        db.add(AdminRole(email="admin@test.com", role="org_admin"))
    from api.main import app
    with TestClient(app) as c:
        yield c


def test_set_repo_token_returns_kind_not_token(client):
    client.post(
        "/api/integrations/github/repos",
        json={"repo": "octo/secret"},
    )
    r = client.patch(
        "/api/integrations/github/repos/octo/secret",
        json={"token": "ghp_supersecret_value"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token_kind"] == "override"
    assert body["token_mode"] == "override"
    # the token itself is NEVER in the response — only a last4 hint.
    assert "ghp_supersecret_value" not in r.text
    assert body.get("pat_last4") == "alue"


def test_force_public_mode_resets_probe(client):
    client.post(
        "/api/integrations/github/repos",
        json={"repo": "octo/pubrepo"},
    )
    r = client.patch(
        "/api/integrations/github/repos/octo/pubrepo",
        json={"token_mode": "public"},
    )
    assert r.status_code == 200
    assert r.json()["token_mode"] == "public"


def test_invalid_token_mode_400(client):
    client.post(
        "/api/integrations/github/repos",
        json={"repo": "octo/x"},
    )
    r = client.patch(
        "/api/integrations/github/repos/octo/x",
        json={"token_mode": "bogus"},
    )
    assert r.status_code == 400
