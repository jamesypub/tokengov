"""
Tests for api/routes/roles.py — alias symmetry between
/api/roles and /api/admin-roles. The React UI hits the
/admin-roles spelling; the tg-admin CLI sends /roles. Both
must return the same shape — drift here breaks one or the
other silently.
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(pg_url, clean_db, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"),
    )

    from db.session import get_db
    from db.models import AdminRole, Team
    from db.org_config import set_tg_owns_directory
    with get_db() as db:
        db.add(AdminRole(
            email="admin@test.com", role="org_admin"))
        db.add(Team(team_id="t1", name="Team1"))
        # These base tests exercise authorization + alias symmetry on an
        # EXTERNAL-IdP deployment (tg does not own the directory), so a
        # grant authorizes only and never calls Cognito. The
        # provisioning path has its own fixture (cognito_client) that
        # flips ownership on.
        set_tg_owns_directory(db, False)

    from api.main import app
    with TestClient(app) as c:
        yield c


def test_list_roles_alias_symmetry(client):
    """/api/roles and /api/admin-roles must return the
    same JSON. If they ever drift, one of the two clients
    breaks silently."""
    a = client.get("/api/roles")
    b = client.get("/api/admin-roles")
    assert a.status_code == 200
    assert b.status_code == 200
    assert a.json() == b.json()


def test_grant_via_admin_roles_visible_in_roles(client):
    """A POST /admin-roles must appear in GET /roles too —
    proves both spellings touch the same backing store."""
    r = client.post("/api/admin-roles", json={
        "email": "alice@test.com",
        "role":  "team_admin",
        "team_id": "t1",
    })
    assert r.status_code == 200
    # On a deployment tg does NOT own (no Cognito directory), grant
    # authorizes only — no provisioning — and reports the external
    # branch so the Add-user modal shows the "grant in your IdP" copy.
    body = r.json()
    assert body["cognito_provisioned"] is False
    assert body["directory"] == "external_idp"

    listing = client.get("/api/roles").json()["roles"]
    emails = {(r["email"], r["role"]) for r in listing}
    assert ("alice@test.com", "team_admin") in emails


def test_parent_team_admin_coerced_to_team_admin(client):
    """Issue #104: legacy clients still send
    parent_team_admin. The route must accept it and silently
    write team_admin so old binaries keep working."""
    r = client.post("/api/admin-roles", json={
        "email": "legacy@test.com",
        "role":  "parent_team_admin",
        "team_id": "t1",
    })
    assert r.status_code == 200
    assert r.json()["role"] == "team_admin"


def test_invalid_role_rejected(client):
    """Anything that isn't org_admin / team_admin (after
    coercion) → 400."""
    r = client.post("/api/admin-roles", json={
        "email": "x@test.com",
        "role":  "super_admin",
    })
    assert r.status_code == 400


def test_cannot_revoke_last_org_admin(client):
    """The "no last org_admin" guard must trigger when the
    only remaining org_admin tries to revoke their own
    org_admin role. Otherwise the system locks itself out."""
    r = client.request(
        "DELETE",
        "/api/admin-roles/admin@test.com",
        json={"role": "org_admin"},
    )
    assert r.status_code == 400
    assert "last org_admin" in r.json()["detail"]


def test_grant_duplicate_returns_409(client):
    """Granting the same (email, role, team) twice → 409,
    not silent success or 500."""
    body = {"email": "dup@test.com", "role": "team_admin",
            "team_id": "t1"}
    r1 = client.post("/api/admin-roles", json=body)
    r2 = client.post("/api/admin-roles", json=body)
    assert r1.status_code == 200
    assert r2.status_code == 409


# ── #357: Cognito user provisioning on add-admin ──────────


class _FakeCognito:
    """Records admin_create_user calls so tests can assert
    the API hit Cognito (or didn't). raise_on_create lets a
    test simulate an AdminCreateUser failure."""

    def __init__(self, raise_on_create=False):
        self.created = []
        self.raise_on_create = raise_on_create

    def admin_create_user(self, **kwargs):
        if self.raise_on_create:
            raise RuntimeError("AdminCreateUser failed")
        self.created.append(kwargs)
        return {"User": {"Username": kwargs["Username"]}}


@pytest.fixture
def cognito_client(monkeypatch):
    """Enable the Cognito provider + mock the boto3 client.
    Returns the fake so tests can inspect/assert calls.
    monkeypatch on os.environ so the gate
    (_cognito_provisioning_enabled) flips on."""
    monkeypatch.setenv("TG_AUTH_PROVIDER", "cognito")
    monkeypatch.setenv(
        "TG_COGNITO_USER_POOL_ID", "us-east-1_test123")
    # The base `client` fixture sets tg_owns_directory=false (external
    # IdP); flip it back ON here so the provisioning path is exercised.
    from db.session import get_db
    from db.org_config import set_tg_owns_directory
    with get_db() as db:
        set_tg_owns_directory(db, True)
    fake = _FakeCognito()

    import boto3
    monkeypatch.setattr(
        boto3, "client",
        lambda *a, **k: fake,
    )
    return fake


def test_grant_auto_provisions_when_directory_owned(
    client, cognito_client,
):
    """When tg owns the directory, grant_role now AUTO-provisions the
    Cognito user (no opt-in flag) with EMAIL delivery, inserts the row,
    and reports cognito_provisioned=true + directory=cognito. The
    Add-user flow dropped the opt-in checkbox — the directory decides."""
    r = client.post("/api/admin-roles", json={
        "email": "newadmin@test.com",
        "role":  "team_admin",
        "team_id": "t1",
        # NO provision_cognito flag — provisioning is automatic now.
    })
    assert r.status_code == 200
    body = r.json()
    assert body["cognito_provisioned"] is True
    assert body["directory"] == "cognito"
    # Cognito hit once, with the right username + delivery.
    assert len(cognito_client.created) == 1
    call = cognito_client.created[0]
    assert call["Username"] == "newadmin@test.com"
    assert call["DesiredDeliveryMediums"] == ["EMAIL"]
    assert call["UserPoolId"] == "us-east-1_test123"
    # Row landed.
    listing = client.get("/api/roles").json()["roles"]
    emails = {(x["email"], x["role"]) for x in listing}
    assert ("newadmin@test.com", "team_admin") in emails


def test_grant_provision_false_flag_does_not_suppress(
    client, cognito_client,
):
    """The legacy provision_cognito=false flag no longer suppresses
    provisioning — the directory-owned decision drives it, so an old
    client sending false still gets the Cognito user created."""
    r = client.post("/api/admin-roles", json={
        "email": "legacyfalse@test.com",
        "role":  "member",
        "provision_cognito": False,
    })
    assert r.status_code == 200
    assert r.json()["cognito_provisioned"] is True
    assert len(cognito_client.created) == 1


def test_provision_cognito_failure_rolls_back_row(
    client, monkeypatch,
):
    """If AdminCreateUser raises, the admin row must NOT be
    inserted (transactional contract, acceptance #3)."""
    monkeypatch.setenv("TG_AUTH_PROVIDER", "cognito")
    monkeypatch.setenv(
        "TG_COGNITO_USER_POOL_ID", "us-east-1_test123")
    # Own the directory (base fixture set it false) so provisioning is
    # attempted — and here it raises, exercising the rollback contract.
    from db.session import get_db
    from db.org_config import set_tg_owns_directory
    with get_db() as db:
        set_tg_owns_directory(db, True)
    fake = _FakeCognito(raise_on_create=True)
    import boto3
    monkeypatch.setattr(
        boto3, "client", lambda *a, **k: fake)

    r = client.post("/api/admin-roles", json={
        "email": "rollback@test.com",
        "role":  "team_admin",
        "team_id": "t1",
        "provision_cognito": True,
    })
    assert r.status_code == 500
    # Row must be absent — no half-state.
    listing = client.get("/api/roles").json()["roles"]
    emails = {x["email"] for x in listing}
    assert "rollback@test.com" not in emails


def test_provision_cognito_ignored_when_idp_external(
    client, monkeypatch,
):
    """#926: provisioning is gated on the tg_owns_directory DB flag,
    not TG_AUTH_PROVIDER. When an external IdP owns the directory
    (flag false), a provision_cognito=true flag is a silent no-op —
    never a 500, never a Cognito call."""
    from db.session import get_db
    from db.org_config import set_tg_owns_directory
    with get_db() as db:
        set_tg_owns_directory(db, False)
    called = []
    import boto3
    monkeypatch.setattr(
        boto3, "client",
        lambda *a, **k: called.append((a, k)),
    )
    r = client.post("/api/admin-roles", json={
        "email": "okta@test.com",
        "role":  "team_admin",
        "team_id": "t1",
        "provision_cognito": True,
    })
    assert r.status_code == 200
    assert r.json()["cognito_provisioned"] is False
    assert called == []


# ── #911: re-issue a stale invite (RESEND) ───────────


def test_reinvite_resends_with_message_action(
    client, cognito_client,
):
    """POST /admin-roles/<email>/reinvite re-sends the
    Cognito invite to an existing admin with
    MessageAction=RESEND (and no UserAttributes). This is the
    fix for a frozen sign-in URL after the ALB DNS churns —
    the new email carries the pool's current CallbackUrl."""
    # admin@test.com is seeded as org_admin by the fixture.
    r = client.post("/api/admin-roles/admin@test.com/reinvite")
    assert r.status_code == 200
    assert r.json() == {
        "email": "admin@test.com", "reinvited": True}
    assert len(cognito_client.created) == 1
    call = cognito_client.created[0]
    assert call["MessageAction"] == "RESEND"
    assert call["Username"] == "admin@test.com"
    # RESEND must not carry attribute changes.
    assert "UserAttributes" not in call


def test_reinvite_unknown_admin_404(client, cognito_client):
    """Re-invite only works for someone tg knows as an admin —
    never an open invite-spray to arbitrary addresses."""
    r = client.post(
        "/api/admin-roles/stranger@test.com/reinvite")
    assert r.status_code == 404
    assert cognito_client.created == []


def test_reinvite_unavailable_when_idp_external(client, monkeypatch):
    """#926: when an external IdP owns the directory (tg_owns_directory
    false), re-invite is unavailable (400), never a stray Cognito
    call — re-invites are managed in the IdP."""
    from db.session import get_db
    from db.org_config import set_tg_owns_directory
    with get_db() as db:
        set_tg_owns_directory(db, False)
    called = []
    import boto3
    monkeypatch.setattr(
        boto3, "client",
        lambda *a, **k: called.append((a, k)),
    )
    r = client.post("/api/admin-roles/admin@test.com/reinvite")
    assert r.status_code == 400
    assert called == []


def test_reinvite_alias_symmetry(client, cognito_client):
    """/roles/<email>/reinvite (CLI spelling) and
    /admin-roles/<email>/reinvite (UI spelling) hit the same
    handler."""
    r = client.post("/api/roles/admin@test.com/reinvite")
    assert r.status_code == 200
    assert r.json()["reinvited"] is True


# ── #927: Enable login (authorize + provision-if-Cognito) + member ──


def _seed_user(email, team_id=None):
    """#932: enable-login requires a known person — a CUR-discovered
    `users` row (state 1). Seed one so the anti-spray guard passes."""
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(email=email, team_id=team_id))


def test_grant_role_accepts_member(client):
    """#927: `member` is now a valid grantable role."""
    r = client.post("/api/admin-roles", json={
        "email": "m@test.com", "role": "member",
    })
    assert r.status_code == 200
    assert r.json()["role"] == "member"
    listing = client.get("/api/roles").json()["roles"]
    assert ("m@test.com", "member") in {
        (x["email"], x["role"]) for x in listing}


def test_enable_login_cognito_authorizes_and_provisions(
    client, cognito_client,
):
    """tg owns the directory (default) → enable-login provisions the
    Cognito user AND adds the member authz row; reports cognito."""
    _seed_user("new@test.com")
    r = client.post("/api/admin-roles/new@test.com/enable-login")
    assert r.status_code == 200
    body = r.json()
    assert body["login_enabled"] is True
    assert body["cognito_provisioned"] is True
    assert body["role"] == "member"
    assert body["directory"] == "cognito"
    assert len(cognito_client.created) == 1
    listing = client.get("/api/roles").json()["roles"]
    assert "new@test.com" in {x["email"] for x in listing}


def test_enable_login_external_idp_skips_provision(client, monkeypatch):
    """External IdP (tg_owns_directory false) → authorize only, no
    Cognito call; reports external_idp."""
    from db.session import get_db
    from db.org_config import set_tg_owns_directory
    with get_db() as db:
        set_tg_owns_directory(db, False)
    _seed_user("sso@test.com")
    called = []
    import boto3
    monkeypatch.setattr(boto3, "client",
                        lambda *a, **k: called.append((a, k)))
    r = client.post("/api/admin-roles/sso@test.com/enable-login")
    assert r.status_code == 200
    body = r.json()
    assert body["login_enabled"] is True
    assert body["cognito_provisioned"] is False
    assert body["directory"] == "external_idp"
    assert called == []
    listing = client.get("/api/roles").json()["roles"]
    assert "sso@test.com" in {x["email"] for x in listing}


def test_enable_login_provision_failure_adds_no_row(client, monkeypatch):
    """Transactional: a Cognito provision failure adds NO authz row."""
    _seed_user("fail@test.com")
    # Own the directory (base fixture set it false) so enable_login
    # attempts Cognito — here it raises, exercising the rollback.
    from db.session import get_db
    from db.org_config import set_tg_owns_directory
    with get_db() as db:
        set_tg_owns_directory(db, True)
    fake = _FakeCognito(raise_on_create=True)
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)
    r = client.post("/api/admin-roles/fail@test.com/enable-login")
    assert r.status_code == 500
    listing = client.get("/api/roles").json()["roles"]
    assert "fail@test.com" not in {x["email"] for x in listing}


def test_enable_login_already_enabled_409(client, cognito_client):
    """Already-enabled (any authz row) → 409."""
    _seed_user("dup@test.com")
    client.post("/api/admin-roles/dup@test.com/enable-login")
    r = client.post("/api/admin-roles/dup@test.com/enable-login")
    assert r.status_code == 409


def test_enable_login_alias_symmetry(client, cognito_client):
    """/roles/<email>/enable-login (CLI) == /admin-roles/... (UI)."""
    _seed_user("cli@test.com")
    r = client.post("/api/roles/cli@test.com/enable-login")
    assert r.status_code == 200
    assert r.json()["login_enabled"] is True


# ── Enable-login authorization: team-scope + anti-spray guards ──


def test_enable_login_unknown_person_rejected_no_spray(client, monkeypatch):
    """#932: an email tg doesn't know (no users row, no prior authz)
    → 404 and NO Cognito invite (anti-spray). Mirrors reinvite."""
    called = []
    import boto3
    monkeypatch.setattr(boto3, "client",
                        lambda *a, **k: called.append((a, k)))
    r = client.post("/api/admin-roles/stranger@evil.com/enable-login")
    assert r.status_code == 404
    assert called == []  # no AdminCreateUser fired


def test_enable_login_team_admin_scoped_to_own_team(
    client, monkeypatch, cognito_client,
):
    """#932: a team_admin can only enable a member into a team they
    administer — a target in another team → 403, no Cognito call."""
    from db.session import get_db
    from db.models import AdminRole, Team, User
    import api.auth as auth_mod
    # Caller is a team_admin of t1 only; t2 exists and is foreign.
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("ta@test.com", "session"))
    with get_db() as db:
        db.add(AdminRole(email="ta@test.com", role="team_admin",
                         team_id="t1"))
        db.add(Team(team_id="t2", name="Team2"))
        db.add(User(email="foreign@test.com", team_id="t2"))
    # Enabling a member into t2 (not in the caller's subtree) → 403.
    r = client.post("/api/admin-roles/foreign@test.com/enable-login",
                    json={"role": "member", "team_id": "t2"})
    assert r.status_code == 403
    assert cognito_client.created == []  # no provision on a denied call


def test_enable_login_team_admin_cannot_grant_admin_role(
    client, monkeypatch, cognito_client,
):
    """#932: the admin-role path stays org-admin-only — a team_admin
    granting org_admin/team_admin via enable-login → 403."""
    from db.session import get_db
    from db.models import AdminRole, User
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("ta@test.com", "session"))
    with get_db() as db:
        db.add(AdminRole(email="ta@test.com", role="team_admin",
                         team_id="t1"))
        db.add(User(email="someone@test.com", team_id="t1"))
    r = client.post("/api/admin-roles/someone@test.com/enable-login",
                    json={"role": "team_admin", "team_id": "t1"})
    assert r.status_code == 403
    assert cognito_client.created == []
