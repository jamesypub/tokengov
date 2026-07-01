"""
#650: 3-tier RBAC on user-detail actions.

Tiers:
  - org_admin   — every action, any user (unchanged).
  - team_admin  — management/governance actions for users in their
                  OWN team subtree; 403 outside it.
  - member      — ONLY self-service (edit own display_name, link/
                  unlink own GitHub); 403 on every management action
                  and on ANY action against another user.

Server-side enforcement is the control (UI gating is cosmetic), so
this exercises the matrix
  {org_admin, team_admin_in, team_admin_out, member_self, member_other}
  × {manage, cap, disable, setDisplayName, linkGithub}
against the real routes with a fake IAM seam.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ── caller switch ───────────────────────────────────────────────
# A module-level mutable so each test can set "who is calling"
# before hitting the app. The app's auth is monkeypatched to read
# it (mirrors the test_users.py / test_velocity_team_scope.py
# pattern, but switchable per request).
_CALLER = {"email": "org@test.com"}


def _set_caller(email):
    _CALLER["email"] = email


@pytest.fixture
def client(pg_url, clean_db, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: (_CALLER["email"], "session"),
    )
    # Fake the IAM seam so manage/unmanage never hit AWS.
    iam = _FakeIam()

    class _FakeSession:
        def client(self, name):
            return iam

    import api.aws_session as aws_session
    monkeypatch.setattr(
        aws_session, "get_aws_session", lambda: _FakeSession())
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123")

    _seed_world()
    _set_caller("org@test.com")  # default; tests override
    from api.main import app
    with TestClient(app) as c:
        c._iam = iam
        yield c


class _FakeIam:
    def __init__(self):
        self.attached, self.detached = [], []

    def attach_role_policy(self, RoleName, PolicyArn):
        self.attached.append((RoleName, PolicyArn))

    def detach_role_policy(self, RoleName, PolicyArn):
        self.detached.append((RoleName, PolicyArn))

    def get_paginator(self, name):
        # #799: these RBAC matrix principals route through a normal
        # (non-admin) role, so the admin-role guard must see no
        # AdministratorAccess and let the action through.
        class _Pager:
            def paginate(self, RoleName):
                return [{"AttachedPolicies": []}]
        return _Pager()


def _seed_world():
    """Two sibling teams; an org admin, a team admin of team A, and
    one member in each team. Members have a discovered principal so
    manage() gets past its principal_arn check and the only thing
    that can 403 is the RBAC gate."""
    from db.session import get_db
    from db.models import User, Team, AdminRole
    with get_db() as db:
        db.add(Team(team_id="A", name="Team A"))
        db.add(Team(team_id="B", name="Team B"))
        db.add(AdminRole(email="org@test.com", role="org_admin"))
        db.add(AdminRole(
            email="tadmina@test.com", role="team_admin",
            team_id="A"))
        for em, team in (
            ("membera@test.com", "A"),
            ("memberb@test.com", "B"),
        ):
            db.add(User(
                # #864: governed=True so the force-block RBAC matrix
                # exercises AUTHZ (200/403), not the new not-governed
                # 409 gate; manage is idempotent on an already-governed
                # principal and cap is unaffected, so the other matrices
                # stay green.
                email=em, status="active", team_id=team,
                identity_key=em, principal_type="assumed_role",
                principal_arn="arn:aws:iam::123:role/tg-consumer",
                role_type="iam", governed=True,
            ))


# ── management actions: manage / cap / disable ──────────────────

@pytest.mark.parametrize("caller,target,expect", [
    ("org@test.com",     "membera@test.com", 200),  # org: any
    ("org@test.com",     "memberb@test.com", 200),
    ("tadmina@test.com", "membera@test.com", 200),  # team: in-team
    ("tadmina@test.com", "memberb@test.com", 403),  # team: out
    ("membera@test.com", "membera@test.com", 403),  # member: self
    ("membera@test.com", "memberb@test.com", 403),  # member: other
])
def test_manage_matrix(client, caller, target, expect):
    _set_caller(caller)
    r = client.post(f"/api/users/{target}/manage")
    assert r.status_code == expect, r.text


@pytest.mark.parametrize("caller,target,expect", [
    ("org@test.com",     "memberb@test.com", 200),
    ("tadmina@test.com", "membera@test.com", 200),
    ("tadmina@test.com", "memberb@test.com", 403),
    ("membera@test.com", "membera@test.com", 403),
    ("membera@test.com", "memberb@test.com", 403),
])
def test_cap_matrix(client, caller, target, expect):
    _set_caller(caller)
    r = client.put(
        f"/api/users/{target}/cap", json={"cap_usd": 5.0})
    assert r.status_code == expect, r.text


@pytest.mark.parametrize("caller,target,expect", [
    ("org@test.com",     "memberb@test.com", 200),
    ("tadmina@test.com", "membera@test.com", 200),
    ("tadmina@test.com", "memberb@test.com", 403),
    ("membera@test.com", "membera@test.com", 403),  # no self-force-block
    ("membera@test.com", "memberb@test.com", 403),
])
def test_force_block_matrix(client, caller, target, expect):
    # #750: /disable → /force-block; same RBAC matrix as before.
    _set_caller(caller)
    r = client.post(
        f"/api/users/{target}/force-block",
        json={"confirm_email": target})
    assert r.status_code == expect, r.text


# ── self-service: display name + GitHub link ────────────────────

@pytest.mark.parametrize("caller,target,expect", [
    ("org@test.com",     "memberb@test.com", 200),  # admin on other
    ("tadmina@test.com", "membera@test.com", 200),  # team on in-team
    ("tadmina@test.com", "memberb@test.com", 403),  # team out → 403
    ("membera@test.com", "membera@test.com", 200),  # SELF allowed
    ("membera@test.com", "memberb@test.com", 403),  # other → 403
])
def test_set_display_name_matrix(client, caller, target, expect):
    _set_caller(caller)
    r = client.patch(
        f"/api/users/{target}", json={"display_name": "Friendly"})
    assert r.status_code == expect, r.text


@pytest.mark.parametrize("caller,target,expect", [
    ("org@test.com",     "memberb@test.com", 200),
    ("tadmina@test.com", "membera@test.com", 200),
    ("tadmina@test.com", "memberb@test.com", 403),  # out-of-subtree
    ("membera@test.com", "membera@test.com", 200),  # SELF link
    ("membera@test.com", "memberb@test.com", 403),  # other → 403
])
def test_link_github_matrix(client, caller, target, expect):
    _set_caller(caller)
    r = client.put(
        f"/api/users/{target}/linked-accounts/github",
        json={"external_handle": "octocat"})
    assert r.status_code == expect, r.text


def test_delete_linked_github_subtree_scoped(client):
    """Regression: the DELETE route used to allow ANY team_admin to
    unlink ANY user (not subtree-scoped, unlike PUT). #650 tightened
    it — a team_admin must be admin of the target's team."""
    _set_caller("tadmina@test.com")
    # out-of-subtree user → 403 (the bug would have returned 204)
    r = client.delete(
        "/api/users/memberb@test.com/linked-accounts/github")
    assert r.status_code == 403, r.text
    # in-subtree user → allowed
    r = client.delete(
        "/api/users/membera@test.com/linked-accounts/github")
    assert r.status_code == 204, r.text


# ── org-level ops stay org-admin-only ───────────────────────────

def test_team_admin_cannot_create_user(client):
    _set_caller("tadmina@test.com")
    r = client.post(
        "/api/users", json={"email": "new@test.com", "team_id": "A"})
    assert r.status_code == 403, r.text


def test_team_admin_cannot_delete_user(client):
    _set_caller("tadmina@test.com")
    r = client.delete("/api/users/membera@test.com")
    assert r.status_code == 403, r.text
