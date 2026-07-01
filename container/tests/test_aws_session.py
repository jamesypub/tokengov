"""
Tests for api/aws_session.py.

#590 (#566C): the api queries Athena/CUR under its OWN task-role
creds (tg-app) via boto3's native chain — no assume-role hop, no
ApiRunnerNotConfigured. These tests assert the cached-session
behavior of the collapsed module.
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def reset_cache():
    """Drop the cached session before each test so each starts
    fresh."""
    import api.aws_session as aws_session
    aws_session.reset_session_cache()
    yield
    aws_session.reset_session_cache()


def test_returns_a_session(monkeypatch):
    """get_aws_session returns a boto3.Session built from the
    native credential chain (no assume-role)."""
    import api.aws_session as aws_session
    sentinel = MagicMock(name="boto3.Session")
    monkeypatch.setattr(
        aws_session.boto3, "Session",
        lambda *a, **kw: sentinel,
    )
    session = aws_session.get_aws_session()
    assert session is sentinel


def test_no_assume_role_call(monkeypatch):
    """The collapsed module must NOT call sts:AssumeRole — the
    whole point of #590 is to drop the hop. Guard against a
    regression that reintroduces it."""
    import api.aws_session as aws_session
    sts = MagicMock()
    monkeypatch.setattr(
        aws_session.boto3, "Session",
        lambda *a, **kw: MagicMock(),
    )
    monkeypatch.setattr(
        aws_session.boto3, "client",
        lambda *a, **kw: sts,
    )
    aws_session.get_aws_session()
    assert not sts.assume_role.called


def test_cached_across_calls(monkeypatch):
    """Second call returns the same Session without rebuilding —
    lru_cache holds it for process lifetime (one Session per
    container boot)."""
    import api.aws_session as aws_session
    calls = {"n": 0}

    def _factory(*a, **kw):
        calls["n"] += 1
        return MagicMock()

    monkeypatch.setattr(aws_session.boto3, "Session", _factory)
    s1 = aws_session.get_aws_session()
    s2 = aws_session.get_aws_session()
    assert s1 is s2
    assert calls["n"] == 1


def test_region_from_env(monkeypatch):
    """The session is built for AWS_REGION (default us-east-1)."""
    import api.aws_session as aws_session
    captured = {}

    def _factory(*a, **kw):
        captured.update(kw)
        return MagicMock()

    monkeypatch.setattr(aws_session.boto3, "Session", _factory)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    aws_session.get_aws_session()
    assert captured.get("region_name") == "us-west-2"


def test_module_imports_cleanly(monkeypatch):
    """Sanity: aws_session imports alongside aws_errors with no
    name collisions / circular imports, and the assume-role-era
    symbols are gone (#590)."""
    import api.aws_session as aws_session
    import api.aws_errors as aws_errors
    assert hasattr(aws_session, "get_aws_session")
    assert hasattr(aws_errors, "EXPIRED_CRED_DETAIL")
    # The #358 assume-role API was removed by #590.
    assert not hasattr(aws_session, "get_runner_session")
    assert not hasattr(aws_session, "ApiRunnerNotConfigured")
