"""A client/session CONSTRUCTION failure in the IAM-writing handlers
(govern / ungovern) must return a truthful 502, never a bare 500 — the
same gap already fixed for the idc-enforcement route.

_iam_client() → get_aws_session().client("iam") raises on a
build/creds failure (ProfileNotFound, NoCredentials — a BotoCoreError,
NOT a ClientError), which used to propagate unhandled → 500. These
tests make _iam_client raise and assert the govern/ungovern handlers
degrade to 502 with a truthful message.
"""
from __future__ import annotations

import pytest
from botocore.exceptions import ProfileNotFound
from fastapi.testclient import TestClient


@pytest.fixture
def client(pg_url, clean_db, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"))
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    from db.session import get_db
    from db.models import AdminRole, User
    with get_db() as db:
        db.add(AdminRole(email="admin@test.com", role="org_admin"))
        # A governable IAM principal (has a role ARN) so manage gets
        # past its no-ARN gate and reaches the attach.
        db.add(User(
            email="dev@corp.com", status="active",
            identity_key="dev@corp.com",
            principal_type="assumed_role", role_type="iam",
            principal_arn="arn:aws:iam::123456789012:role/AppRole"))
    from api.main import app
    with TestClient(app) as c:
        yield c


def _break_iam(monkeypatch):
    from api.routes import users as users_mod

    def _boom():
        raise ProfileNotFound(profile="tg-missing")
    monkeypatch.setattr(users_mod, "_iam_client", _boom)


def test_manage_returns_502_not_500_on_client_build_failure(
        client, monkeypatch):
    _break_iam(monkeypatch)
    r = client.post("/api/users/dev@corp.com/manage")
    assert r.status_code == 502, r.text     # NOT 500
    assert "Govern did NOT complete" in r.text


def test_unmanage_returns_502_not_500_on_client_build_failure(
        client, monkeypatch):
    # Govern first (with a working stub), then break the client for the
    # ungovern detach path.
    from api.routes import users as users_mod
    from unittest.mock import MagicMock
    ok_iam = MagicMock()
    ok_iam.get_paginator.return_value.paginate.return_value = [
        {"AttachedPolicies": []}]
    monkeypatch.setattr(users_mod, "_iam_client", lambda: ok_iam)
    assert client.post("/api/users/dev@corp.com/manage").status_code == 200
    # Now the detach path can't build a client → 502, not 500.
    _break_iam(monkeypatch)
    r = client.post("/api/users/dev@corp.com/unmanage")
    assert r.status_code == 502, r.text
    assert "did NOT complete" in r.text
