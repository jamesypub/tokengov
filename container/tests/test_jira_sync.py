"""
Tests for the jira_sync worker job.

Approach: stub the HTTP client (`_jira_request`) to return
canned responses, and exercise the full orchestration in
`run()`. Keeps tests fast and offline.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError

import pytest


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _issue_payload(key, issue_type, status="Done"):
    return {
        "key": key,
        "fields": {
            "issuetype": {"name": issue_type},
            "summary": f"summary for {key}",
            "status": {
                "name": status,
                "statusCategory": {"key": "done"},
            },
            "priority": {"name": "Medium"},
            "assignee": {"emailAddress": "a@e.com"},
            "reporter": {"emailAddress": "r@e.com"},
            "created": "2024-01-01T00:00:00.000+0000",
            "updated": "2024-01-02T00:00:00.000+0000",
        },
    }


def _enable_jira(db):
    """#447: jira_sync now early-returns unless the runtime
    admin_config flag jira_enabled is on. Flip it for the tests
    that exercise the actual sync path."""
    from db.jira_feature import set_jira_enabled
    set_jira_enabled(db, True)


@pytest.fixture
def jira_setup(pg_url, clean_db):
    from db.session import get_db
    from db.models import JiraSite, PrJiraRef
    with get_db() as db:
        _enable_jira(db)
        db.add(JiraSite(
            site_url="https://x.atlassian.net",
            auth_email="ci@x.com",
            api_token_plain="DUMMY",
            projects=json.dumps(["PROJ"]),
            added_by="t@x.com",
        ))
        db.add(PrJiraRef(
            repo="owner/repo", pr_number=1,
            issue_key="PROJ-1", source="title",
        ))
        db.add(PrJiraRef(
            repo="owner/repo", pr_number=2,
            issue_key="PROJ-2", source="body",
        ))


def test_run_skips_when_no_sites(pg_url, clean_db):
    from db.session import get_db
    from worker.jobs.jira_sync import run
    with get_db() as db:
        _enable_jira(db)
    r = run()
    assert r.get("skipped") is True
    assert r.get("skip_reason") == "no_sites"


def test_run_skips_when_jira_disabled(pg_url, clean_db):
    """#447: with the runtime flag OFF (default), jira_sync
    early-returns before touching sites — even if sites + refs
    exist. The skip is recorded with skip_reason=jira_disabled."""
    from db.session import get_db
    from db.models import JiraSite, PrJiraRef
    import worker.jobs.jira_sync as js
    with get_db() as db:
        db.add(JiraSite(
            site_url="https://x.atlassian.net",
            auth_email="ci@x.com", api_token_plain="DUMMY",
            projects=json.dumps(["PROJ"]), added_by="t@x.com",
        ))
        db.add(PrJiraRef(
            repo="o/r", pr_number=1,
            issue_key="PROJ-1", source="title",
        ))

    def boom(*a, **k):
        raise AssertionError(
            "jira_sync must not run when jira_enabled is off")
    monkeypatch_request = js._jira_request
    js._jira_request = boom
    try:
        r = js.run()
    finally:
        js._jira_request = monkeypatch_request
    assert r.get("skipped") is True
    assert r.get("skip_reason") == "jira_disabled"


def test_run_upserts_issues(jira_setup, monkeypatch):
    """Happy path — both refs are fetched and inserted."""
    from db.session import get_db
    from db.models import JiraIssue
    import worker.jobs.jira_sync as js

    payload = {
        "issues": [
            _issue_payload("PROJ-1", "Story"),
            _issue_payload("PROJ-2", "Bug"),
        ],
    }
    body = json.dumps(payload).encode("utf-8")

    def fake_request(site_url, path, auth, method="GET"):
        if "/myself" in path:
            return _FakeResponse(b'{"accountId":"abc"}')
        return _FakeResponse(body)

    monkeypatch.setattr(js, "_jira_request", fake_request)

    r = js.run()
    assert "upserted=2" in r["detail"]

    with get_db() as db:
        rows = db.query(JiraIssue).all()
        assert {r.issue_key for r in rows} == {"PROJ-1", "PROJ-2"}
        types = {r.issue_key: r.issue_type for r in rows}
        assert types == {"PROJ-1": "Story", "PROJ-2": "Bug"}


def test_run_handles_429(jira_setup, monkeypatch):
    """429 marks site paused without raising."""
    from db.session import get_db
    from db.models import JiraSite
    import worker.jobs.jira_sync as js

    def fake_request(site_url, path, auth, method="GET"):
        if "/myself" in path:
            return _FakeResponse(b'{"accountId":"abc"}')
        raise HTTPError(
            site_url + path, 429, "Too Many", {"Retry-After": "60"},
            None,
        )

    monkeypatch.setattr(js, "_jira_request", fake_request)

    r = js.run()
    with get_db() as db:
        site = db.query(JiraSite).first()
        assert site.sync_status == "paused"
    assert "paused" in r["detail"]


def test_run_handles_401_on_myself(jira_setup, monkeypatch):
    """401 on /myself short-circuits to auth_failed."""
    from db.session import get_db
    from db.models import JiraSite, JiraIssue
    import worker.jobs.jira_sync as js

    def fake_request(site_url, path, auth, method="GET"):
        if "/myself" in path:
            raise HTTPError(
                site_url + path, 401, "Unauthorized", {}, None,
            )
        # Should not reach here
        raise AssertionError("search called despite 401")

    monkeypatch.setattr(js, "_jira_request", fake_request)
    js.run()

    with get_db() as db:
        site = db.query(JiraSite).first()
        assert site.sync_status == "auth_failed"
        assert db.query(JiraIssue).count() == 0


def test_keys_to_fetch_skips_fresh(jira_setup):
    from db.session import get_db
    from db.models import JiraIssue
    from worker.jobs.jira_sync import _keys_to_fetch

    now = datetime.now(timezone.utc)
    with get_db() as db:
        # PROJ-1 was just synced — should be skipped
        db.add(JiraIssue(
            issue_key="PROJ-1",
            issue_type="Story",
            status="Done",
            status_category="done",
            jira_created_at=now - timedelta(days=1),
            jira_updated_at=now - timedelta(hours=2),
            last_synced_at=now,
        ))

    with get_db() as db:
        keys = _keys_to_fetch(db)
    assert keys == ["PROJ-2"]


def test_run_skips_synthetic_site(pg_url, clean_db, monkeypatch):
    """A synthetic site (added_by='jira_synth_seed') must be
    skipped outright: no probe, no token read, sync_status
    left at 'ok'. Without this, the no-token branch flips it
    to auth_failed and the V&C Jira surface goes dark."""
    from db.session import get_db
    from db.models import JiraSite, PrJiraRef
    import worker.jobs.jira_sync as js

    with get_db() as db:
        _enable_jira(db)
        db.add(JiraSite(
            site_url="https://synthetic.invalid",
            auth_email="synthetic@synthetic.invalid",
            api_token_plain=None,
            projects=json.dumps(["PROJ"]),
            sync_status="ok",
            added_by="jira_synth_seed",
        ))
        db.add(PrJiraRef(
            repo="o/r", pr_number=1,
            issue_key="PROJ-1", source="title",
        ))

    def boom(*a, **k):
        raise AssertionError(
            "jira_sync must not hit the wire for synthetic site")

    monkeypatch.setattr(js, "_jira_request", boom)

    r = js.run()
    assert "synthetic_skipped=1" in r["detail"]
    with get_db() as db:
        s = db.query(JiraSite).first()
        assert s.sync_status == "ok"


def test_run_skips_synthetic_but_syncs_real(
    pg_url, clean_db, monkeypatch,
):
    """Mixed fleet: a synthetic site is skipped while a real
    site alongside it still syncs normally."""
    from db.session import get_db
    from db.models import JiraSite, JiraIssue, PrJiraRef
    import worker.jobs.jira_sync as js

    with get_db() as db:
        _enable_jira(db)
        db.add(JiraSite(
            site_url="https://synthetic.invalid",
            auth_email="synthetic@synthetic.invalid",
            projects=json.dumps(["PROJ"]),
            sync_status="ok",
            added_by="jira_synth_seed",
        ))
        db.add(JiraSite(
            site_url="https://real.atlassian.net",
            auth_email="ci@real.com",
            api_token_plain="DUMMY",
            projects=json.dumps(["REAL"]),
            added_by="t@real.com",
        ))
        db.add(PrJiraRef(
            repo="o/r", pr_number=1,
            issue_key="REAL-1", source="title",
        ))

    body = json.dumps(
        {"issues": [_issue_payload("REAL-1", "Story")]}
    ).encode("utf-8")

    def fake_request(site_url, path, auth, method="GET"):
        assert "synthetic.invalid" not in site_url
        if "/myself" in path:
            return _FakeResponse(b'{"accountId":"abc"}')
        return _FakeResponse(body)

    monkeypatch.setattr(js, "_jira_request", fake_request)

    r = js.run()
    assert "synthetic_skipped=1" in r["detail"]
    assert "upserted=1" in r["detail"]
    with get_db() as db:
        synth = db.query(JiraSite).filter(
            JiraSite.added_by == "jira_synth_seed"
        ).first()
        assert synth.sync_status == "ok"
        assert db.query(JiraIssue).count() == 1


def test_run_no_token_marks_auth_failed(pg_url, clean_db, monkeypatch):
    from db.session import get_db
    from db.models import JiraSite, PrJiraRef
    from worker.jobs.jira_sync import run

    with get_db() as db:
        _enable_jira(db)
        db.add(JiraSite(
            site_url="https://y.atlassian.net",
            auth_email="ci@y.com",
            projects=json.dumps(["FOO"]),
            added_by="t@y.com",
        ))
        db.add(PrJiraRef(
            repo="o/r", pr_number=1,
            issue_key="FOO-1", source="title",
        ))

    run()
    with get_db() as db:
        s = db.query(JiraSite).first()
        assert s.sync_status == "auth_failed"
