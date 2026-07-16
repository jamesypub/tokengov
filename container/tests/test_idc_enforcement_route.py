"""Route contract for GET /api/users/{email}/idc-enforcement.

Asserts the endpoint reports the VERIFIED enforcement state for a
governed IDC user — pending when the deny reaches no role they use,
enforced when it's on their SSO role — so the UI never presents
unverified intent as enforced. Patches the IAM client (no live AWS);
uses the shared conftest Postgres testcontainer.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(pg_url, clean_db):
    import api.auth as auth_mod
    auth_mod._validate_request = (
        lambda req, db: ("admin@test.com", "session")
    )
    from api.main import app
    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        if not db.query(AdminRole).filter(
            AdminRole.email == "admin@test.com"
        ).first():
            db.add(AdminRole(email="admin@test.com", role="org_admin"))
    with TestClient(app) as c:
        yield c


_IDC_ARN = ("arn:aws:iam::123456789012:role/aws-reserved/"
            "sso.amazonaws.com/AWSReservedSSO_Dev_abc123")


def _seed_idc_user(email, arn=_IDC_ARN, governed=True):
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(
            email=email, status="active", principal_arn=arn,
            principal_type="assumed_role", role_type="idc",
            governed=governed))


def _stub_iam(monkeypatch, *, sso_attached, consumer_attached,
              trust_wired):
    """Patch users._iam_client to a MagicMock reflecting the three
    read-only facts the classifier reads."""
    iam = MagicMock()

    def _paginator(op):
        p = MagicMock()
        if op == "list_attached_role_policies":
            pols = ([{"PolicyName": "tg-BedrockQuotaDeny"}]
                    if sso_attached else [])
            p.paginate.return_value = [{"AttachedPolicies": pols}]
        else:
            roles = ([{"RoleName": "tg-consumer"}]
                     if consumer_attached else [])
            p.paginate.return_value = [{"PolicyRoles": roles}]
        return p
    iam.get_paginator.side_effect = _paginator
    iam.get_caller_identity.return_value = {"Account": "123456789012"}
    # tg-consumer trust doc — an ArnLike matching the permset iff wired.
    trust = {"Version": "2012-10-17", "Statement": []}
    if trust_wired:
        trust["Statement"].append({
            "Sid": "TgGovernIdcDev",
            "Effect": "Allow",
            "Principal": {"AWS": "arn:aws:iam::123456789012:root"},
            "Action": "sts:AssumeRole",
            "Condition": {"ArnLike": {"aws:PrincipalArn": (
                "arn:aws:iam::123456789012:role/aws-reserved/"
                "sso.amazonaws.com/AWSReservedSSO_Dev_*")}},
        })
    iam.get_role.return_value = {
        "Role": {"AssumeRolePolicyDocument": trust}}

    from api.routes import users as users_mod
    monkeypatch.setattr(users_mod, "_iam_client", lambda: iam)
    # AWS_ACCOUNT_ID so _deny_policy_arn doesn't call STS.
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")


def test_pending_when_deny_reaches_no_role(client, monkeypatch):
    _seed_idc_user("dev@corp.com")
    _stub_iam(monkeypatch, sso_attached=False,
              consumer_attached=False, trust_wired=False)
    r = client.get("/api/users/dev@corp.com/idc-enforcement")
    assert r.status_code == 200
    assert r.json() == {"state": "pending", "enforced": False}


def test_enforced_here_when_deny_on_sso_role(client, monkeypatch):
    _seed_idc_user("dev@corp.com")
    _stub_iam(monkeypatch, sso_attached=True,
              consumer_attached=False, trust_wired=False)
    r = client.get("/api/users/dev@corp.com/idc-enforcement")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "enforced_here"
    assert body["enforced"] is True


def test_enforced_via_consumer_when_deny_on_consumer_and_trust(
        client, monkeypatch):
    _seed_idc_user("dev@corp.com")
    _stub_iam(monkeypatch, sso_attached=False,
              consumer_attached=True, trust_wired=True)
    r = client.get("/api/users/dev@corp.com/idc-enforcement")
    assert r.json()["enforced"] is True
    assert r.json()["state"] == "enforced_via_consumer"


def test_consumer_deny_without_trust_is_pending(client, monkeypatch):
    _seed_idc_user("dev@corp.com")
    _stub_iam(monkeypatch, sso_attached=False,
              consumer_attached=True, trust_wired=False)
    r = client.get("/api/users/dev@corp.com/idc-enforcement")
    assert r.json() == {"state": "pending", "enforced": False}


def test_client_construction_failure_degrades_to_unknown(
        client, monkeypatch):
    # The IAM client/session BUILD can fail (ProfileNotFound,
    # NoCredentials) — that must degrade to unknown + HTTP 200, never a
    # 500 (a 500 to the browser is a FAIL; the module contract is "a
    # failed read → unknown, never a false enforced"). Exercises the
    # construction path the other tests bypass by patching the returned
    # client.
    from api.routes import users as users_mod
    from botocore.exceptions import ProfileNotFound

    def _boom():
        raise ProfileNotFound(profile="tg-missing")
    monkeypatch.setattr(users_mod, "_iam_client", _boom)
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    _seed_idc_user("dev@corp.com")
    r = client.get("/api/users/dev@corp.com/idc-enforcement")
    assert r.status_code == 200      # NOT a 500
    assert r.json() == {"state": "unknown", "enforced": False}


def test_non_idc_user_returns_pending_not_enforced(client, monkeypatch):
    # A non-IDC governed user isn't classified here — the UI only calls
    # this for governed IDC users. Endpoint returns a benign default.
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(
            email="iam@corp.com", status="active",
            principal_arn="arn:aws:iam::123456789012:role/tg-consumer",
            principal_type="assumed_role", role_type="iam",
            governed=True))
    _stub_iam(monkeypatch, sso_attached=False,
              consumer_attached=True, trust_wired=True)
    r = client.get("/api/users/iam@corp.com/idc-enforcement")
    assert r.json() == {"state": "pending", "enforced": False}
