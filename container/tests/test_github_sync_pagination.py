"""
#656: github_sync paginates the closed-PR fetch instead of dropping
everything past the first 50. Covers:
  - _parse_next_link: Link-header rel="next" extraction.
  - 2-page Link-header walk → BOTH pages' merged PRs upserted.
  - early-stop on the synced horizon: once a page's last PR is older
    than last_sync_at − margin, pagination stops (no unbounded walk).
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone

import pytest

import worker.jobs.github_sync as gs


# ── _parse_next_link (pure) ─────────────────────────────────────

def test_parse_next_link_present():
    h = ('<https://api.github.com/repositories/1/pulls?page=2>; '
         'rel="next", '
         '<https://api.github.com/repositories/1/pulls?page=5>; '
         'rel="last"')
    assert gs._parse_next_link(h) == (
        "https://api.github.com/repositories/1/pulls?page=2")


def test_parse_next_link_absent():
    # last page: only rel="prev"/"first", no next.
    h = ('<https://api.github.com/x?page=4>; rel="prev", '
         '<https://api.github.com/x?page=1>; rel="first"')
    assert gs._parse_next_link(h) is None
    assert gs._parse_next_link(None) is None
    assert gs._parse_next_link("") is None


# ── fake HTTP layer ─────────────────────────────────────────────

class _FakeResp:
    def __init__(self, body, link):
        self._body = json.dumps(body).encode("utf-8")
        self.headers = {"Link": link} if link else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _pr(num, merged_at, updated_at):
    return {
        "number": num,
        "title": f"PR {num}",
        "merged_at": merged_at,
        "updated_at": updated_at,
        "created_at": updated_at,
        "user": {"login": "dev"},
        "labels": [],
        "body": "",
        "head": {"ref": "feat/x"},
    }


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 2-page pagination ───────────────────────────────────────────

def test_two_page_walk_upserts_both_pages(clean_db, monkeypatch):
    """A repo with two pages of closed PRs (Link rel=next on page 1)
    must upsert merged PRs from BOTH pages — the pre-#656 bug
    dropped page 2 silently."""
    now = datetime.now(timezone.utc)
    page1 = [_pr(n, _iso(now), _iso(now)) for n in range(1, 4)]
    page2 = [_pr(n, _iso(now), _iso(now)) for n in range(4, 7)]
    next_url = "https://api.github.com/repositories/1/pulls?page=2"

    calls = {"n": 0}

    def fake_req(url, token):
        calls["n"] += 1
        if "page=2" in url:
            return _FakeResp(page2, None)         # last page
        return _FakeResp(page1, f'<{next_url}>; rel="next"')

    monkeypatch.setattr(gs, "_gh_request_url", fake_req)

    # No prior last_sync_at row → horizon None → walk both pages.
    out = gs._sync_one_repo("org/repo", token="t")
    assert calls["n"] == 2, "should have followed the next page"
    assert out["fetched"] == 6
    assert out["upserted"] == 6

    from db.session import get_db
    from db.models import GithubActivity
    with get_db() as db:
        nums = {
            r.pr_number for r in
            db.query(GithubActivity)
            .filter(GithubActivity.repo == "org/repo").all()
        }
    assert nums == set(range(1, 7))


def test_early_stop_on_horizon(clean_db, monkeypatch):
    """With a prior last_sync_at, pagination stops once a page's
    last PR is older than the horizon (last_sync_at − margin) — the
    next page is not fetched (bounded work)."""
    now = datetime.now(timezone.utc)
    # Seed a repo synced 10 days ago → horizon = 9 days ago.
    from db.session import get_db
    from db.models import GithubRepo
    with get_db() as db:
        db.add(GithubRepo(
            repo="org/repo",
            last_sync_at=now - timedelta(days=10),
            sync_status="ok", added_by="test"))

    # Page 1: recent PRs, but its LAST item is 30 days old (well
    # past the 9-day horizon) → loop must stop, never fetch page 2.
    page1 = [
        _pr(1, _iso(now), _iso(now)),
        _pr(2, _iso(now - timedelta(days=30)),
            _iso(now - timedelta(days=30))),
    ]
    next_url = "https://api.github.com/repositories/1/pulls?page=2"
    calls = {"n": 0}

    def fake_req(url, token):
        calls["n"] += 1
        if "page=2" in url:
            pytest.fail("page 2 fetched despite crossing horizon")
        return _FakeResp(page1, f'<{next_url}>; rel="next"')

    monkeypatch.setattr(gs, "_gh_request_url", fake_req)
    out = gs._sync_one_repo("org/repo", token="t")
    assert calls["n"] == 1, "should stop after page 1 (horizon)"
    # Both merged PRs on page 1 still upserted (filter is merged_at,
    # not the horizon — horizon only bounds pagination).
    assert out["upserted"] == 2
