"""
User detail page contract (#277).

Pins cap_source + pct_used on the /api/users response shape:
  - cap_source flips correctly between user_override /
    org_default / none as the per-user cap_usd and the
    org_default_quota_usd change.
  - pct_used is a clean ratio (or null when undefined),
    not silently 0 / NaN / Infinity.
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(pg_url, clean_db):
    """Reuses the session-scoped pg_url so the testcontainer
    survives across the file. clean_db truncates between tests."""
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
            db.add(AdminRole(
                email="admin@test.com", role="org_admin",
            ))
    with TestClient(app) as c:
        yield c


def _seed_user(email, cap):
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(email=email, status="active", cap_usd=cap))


def _seed_metric(email, spend):
    from db.session import get_db
    from db.models import CurUserSpend
    usage_hour = datetime.now(timezone.utc).date()  # #643
    with get_db() as db:
        db.add(CurUserSpend(
            email=email, usage_hour=usage_hour, model_id="m1",
            input_tokens=0, output_tokens=0,
            total_tokens=0, spend_usd=spend,
        ))


# ────────────────────────────────────────────────
# cap_source
# ────────────────────────────────────────────────

def test_cap_source_user_override_when_cap_set(client):
    _seed_user("explicit@test.com", cap=42.0)
    r = client.get("/api/users/explicit@test.com")
    assert r.status_code == 200
    body = r.json()
    assert body["cap_usd"] == 42.0
    assert body["cap_source"] == "user_override"


def test_cap_source_org_default_when_cap_null(client):
    """User with no per-user cap inherits the org default —
    cap_source should report org_default so the UI can
    label the Cap stat card accordingly (#277 sub-task D)."""
    # The org default is the built-in 1000 (no admin_config
    # row seeded; helper falls back to ORG_DEFAULT_QUOTA_USD).
    _seed_user("inherit@test.com", cap=None)
    r = client.get("/api/users/inherit@test.com")
    body = r.json()
    assert body["cap_usd"] is None
    assert body["cap_source"] == "org_default"
    assert body["effective_quota_usd"] > 0


def test_cap_source_none_when_no_cap_anywhere(client):
    """If both per-user cap and org_default are 0/null, the
    user is genuinely uncapped — cap_source must say so
    rather than silently labeling them inherited."""
    # Force org_default_quota_usd to 0 explicitly. Helper
    # treats stored 0.0 as "set to zero" (no fall-through to
    # the legacy QuotaPolicy DEFAULT or the 1000 floor).
    r = client.put(
        "/api/admin/config",
        json={"org_default_quota_usd": 0},
    )
    assert r.status_code == 200, r.text
    _seed_user("uncapped@test.com", cap=None)
    r = client.get("/api/users/uncapped@test.com")
    body = r.json()
    assert body["cap_source"] == "none"
    assert body["effective_quota_usd"] == 0


# ────────────────────────────────────────────────
# pct_used
# ────────────────────────────────────────────────

def test_pct_used_computed_when_cap_set(client):
    _seed_user("user@test.com", cap=10.0)
    _seed_metric("user@test.com", spend=2.5)
    r = client.get("/api/users/user@test.com")
    body = r.json()
    # 2.5 / 10 = 25.0% exactly.
    assert body["pct_used"] == 25.0
    assert body["mtd_spend_usd"] == 2.5


def test_pct_used_null_when_cap_null(client):
    """pct_used must be null (not 0, not NaN, not Infinity)
    when the user has no cap_usd — the UI's `Used` card
    renders '—' for the null case."""
    _seed_user("nocap@test.com", cap=None)
    _seed_metric("nocap@test.com", spend=2.5)
    r = client.get("/api/users/nocap@test.com")
    body = r.json()
    assert body["pct_used"] is None


def test_pct_used_null_when_cap_zero(client):
    """Explicit per-user cap=0 is a divide-by-zero trap.
    Must surface as null so the UI doesn't render '∞%' or
    crash."""
    _seed_user("zero@test.com", cap=0.0)
    _seed_metric("zero@test.com", spend=1.0)
    r = client.get("/api/users/zero@test.com")
    body = r.json()
    assert body["pct_used"] is None


def test_pct_used_rounds_to_one_decimal(client):
    _seed_user("round@test.com", cap=3.0)
    _seed_metric("round@test.com", spend=1.0)  # 33.333...%
    r = client.get("/api/users/round@test.com")
    assert r.json()["pct_used"] == 33.3


# ────────────────────────────────────────────────
# list endpoint (sub-task A applies here too)
# ────────────────────────────────────────────────

def test_list_users_includes_cap_source_and_pct_used(client):
    _seed_user("a@test.com", cap=10.0)
    _seed_user("b@test.com", cap=None)
    r = client.get("/api/users")
    rows = {u["email"]: u for u in r.json()["users"]}
    for email in ("a@test.com", "b@test.com"):
        assert "cap_source" in rows[email]
        assert "pct_used" in rows[email]
    assert rows["a@test.com"]["cap_source"] == "user_override"
    assert rows["b@test.com"]["cap_source"] == "org_default"


def test_list_users_includes_mtd_spend(client):
    """#428: the list endpoint must report mtd_spend_usd, not
    default it to 0. Regression for the list-vs-detail aggregate
    gap where /api/users/{email} computed spend but /api/users
    did not — so the Users page showed $0 for everyone."""
    _seed_user("spender@test.com", cap=10.0)
    _seed_metric("spender@test.com", spend=1.0)
    _seed_user("broke@test.com", cap=10.0)
    # Detail endpoint (known-correct) is the reference.
    detail = client.get("/api/users/spender@test.com").json()
    assert detail["mtd_spend_usd"] == 1.0
    rows = {
        u["email"]: u
        for u in client.get("/api/users").json()["users"]
    }
    assert rows["spender@test.com"]["mtd_spend_usd"] == 1.0
    # A user with no metrics still reports 0.0, not null.
    assert rows["broke@test.com"]["mtd_spend_usd"] == 0.0


# ── #345 principal-shape surface ──────────────────────

def _seed_principal(email, identity_key, principal_type,
                    principal_arn):
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        u = db.query(User).filter(User.email == email).first()
        if not u:
            u = User(email=email, status="active")
            db.add(u)
        u.identity_key = identity_key
        u.principal_type = principal_type
        u.principal_arn = principal_arn


def test_managed_flag_when_assumed_role_is_token_consumer(
    client,
):
    """#345: a human reaching Bedrock through
    tg-consumer (the default managed role) renders
    `managed=true`."""
    _seed_principal(
        "alice@test.com",
        identity_key="alice@test.com",
        principal_type="assumed_role",
        principal_arn=(
            "arn:aws:iam::123:role/tg-consumer"
        ),
    )
    r = client.get("/api/users/alice@test.com")
    body = r.json()
    assert body["managed"] is True
    assert body["is_service"] is False
    assert body["principal_type"] == "assumed_role"
    assert body["identity_key"] == "alice@test.com"
    assert body["email"] == "alice@test.com"


def test_unmanaged_flag_when_assumed_role_is_other(client):
    """#345: a human reaching Bedrock through a non-TG role
    renders `managed=false` so the UI flags them under the
    'Unmanaged' chip."""
    _seed_principal(
        "bypass@test.com",
        identity_key="bypass@test.com",
        principal_type="assumed_role",
        principal_arn=(
            "arn:aws:iam::123:role/AcmeEng"
        ),
    )
    r = client.get("/api/users/bypass@test.com")
    body = r.json()
    assert body["managed"] is False
    assert body["is_service"] is False


def test_service_row_returns_email_null(client):
    """#345: machine principals carry no email; the API
    surface returns email=None and is_service=true so the
    UI renders the role as the identity."""
    _seed_principal(
        # synthetic email = identity_key for service rows
        # (the JIT path uses identity_key as the row PK).
        "role:MyEcsTaskRole",
        identity_key="role:MyEcsTaskRole",
        principal_type="service",
        principal_arn=(
            "arn:aws:iam::123:role/MyEcsTaskRole"
        ),
    )
    r = client.get("/api/users/role:MyEcsTaskRole")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] is None
    assert body["is_service"] is True
    assert body["managed"] is False
    assert body["identity_key"] == "role:MyEcsTaskRole"
    assert body["principal_type"] == "service"


def test_managed_role_env_var_override(monkeypatch):
    """TG_MANAGED_ROLE_NAMES overrides the default managed-role set
    used by _is_managed.

    This used to `importlib.reload(api.routes.users)`, which swapped a
    FRESH module into sys.modules and LEFT it there — so the
    already-mounted FastAPI app kept calling the old route functions
    while other suites patched the new module object, making
    test_idc_enforcement_route flaky when this ran first. The env var
    is only read at import to build the module-level _MANAGED_ROLE_NAMES
    set; override that set directly (monkeypatch auto-restores it) — no
    reload, no cross-suite module leak."""
    import api.routes.users as ur
    monkeypatch.setattr(
        ur, "_MANAGED_ROLE_NAMES", {"AcmeBedrock", "foo"})
    from db.models import User
    u = User(
        email="dev@test.com",
        identity_key="dev@test.com",
        principal_type="assumed_role",
        principal_arn="arn:aws:iam::123:role/AcmeBedrock",
    )
    assert ur._is_managed(u) is True


# ── #625 deny-only governance foundation ──────────────

def _seed_principal_model(identity_key, model_id, count=1):
    from db.session import get_db
    from db.models import PrincipalModel
    with get_db() as db:
        db.add(PrincipalModel(
            identity_key=identity_key,
            model_id=model_id,
            invocations_count=count,
        ))


def test_governed_defaults_false_and_role_type_iam(client):
    """#625: a freshly-seeded principal reports governed=false
    and role_type defaults to 'iam' when unset."""
    _seed_user("plain@test.com", cap=None)
    body = client.get("/api/users/plain@test.com").json()
    assert body["governed"] is False
    assert body["role_type"] == "iam"
    assert body["display_name"] is None
    assert body["models"] == []


def test_role_type_idc_surfaced(client):
    """#625: an IDC permission-set principal reports
    role_type='idc' so the UI can disable Manage."""
    from db.session import get_db
    from db.models import User
    _seed_user("idc@test.com", cap=None)
    with get_db() as db:
        u = db.query(User).filter(
            User.email == "idc@test.com").first()
        u.role_type = "idc"
        u.principal_type = "assumed_role"
    body = client.get("/api/users/idc@test.com").json()
    assert body["role_type"] == "idc"


def test_governed_flag_surfaced_when_set(client):
    from db.session import get_db
    from db.models import User
    _seed_user("gov@test.com", cap=None)
    with get_db() as db:
        u = db.query(User).filter(
            User.email == "gov@test.com").first()
        u.governed = True
    body = client.get("/api/users/gov@test.com").json()
    assert body["governed"] is True


def test_governance_updated_at_surfaced(client):
    """The user detail exposes governance_updated_at — the apply-status
    UI compares it to the last deny_reconciler run to show pending vs
    enforced (govern/block/unblock enforce on the ~5-min tick, not
    instantly). A seeded user has a non-null timestamp."""
    _seed_user("ts@test.com", cap=None)
    body = client.get("/api/users/ts@test.com").json()
    assert "governance_updated_at" in body
    assert body["governance_updated_at"]  # non-null ISO string


def test_observed_models_surfaced_on_detail_and_list(client):
    """#625: per-principal observed models appear (sorted) on
    both the detail and list endpoints."""
    _seed_user("modeluser@test.com", cap=None)
    _seed_principal_model(
        "modeluser@test.com", "us.anthropic.claude-sonnet-4-6")
    _seed_principal_model(
        "modeluser@test.com",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    detail = client.get(
        "/api/users/modeluser@test.com").json()
    assert detail["models"] == [
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-sonnet-4-6",
    ]
    rows = {
        u["email"]: u
        for u in client.get("/api/users").json()["users"]
    }
    assert rows["modeluser@test.com"]["models"] == \
        detail["models"]


def test_patch_sets_and_clears_display_name(client):
    """#625: PATCH /users/{email} sets then clears
    display_name; the ARN-derived caller (email/identity_key/
    principal_arn) is never touched."""
    from db.session import get_db
    from db.models import User
    _seed_principal(
        "caller@test.com",
        identity_key="caller@test.com",
        principal_type="assumed_role",
        principal_arn="arn:aws:iam::123:role/tg-consumer",
    )
    # Set.
    r = client.patch(
        "/api/users/caller@test.com",
        json={"display_name": "  Acme Dev  "},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == "Acme Dev"  # trimmed
    # Caller fields untouched & read-only.
    assert body["email"] == "caller@test.com"
    assert body["identity_key"] == "caller@test.com"
    assert body["principal_arn"] == \
        "arn:aws:iam::123:role/tg-consumer"
    # Persisted.
    with get_db() as db:
        u = db.query(User).filter(
            User.email == "caller@test.com").first()
        assert u.display_name == "Acme Dev"
        assert u.identity_key == "caller@test.com"
        assert u.principal_arn == \
            "arn:aws:iam::123:role/tg-consumer"
    # Clear (null).
    r = client.patch(
        "/api/users/caller@test.com",
        json={"display_name": None},
    )
    assert r.status_code == 200
    assert r.json()["display_name"] is None
    # Clear via empty string also nulls it.
    client.patch(
        "/api/users/caller@test.com",
        json={"display_name": "x"})
    r = client.patch(
        "/api/users/caller@test.com",
        json={"display_name": ""})
    assert r.json()["display_name"] is None


def test_patch_rejects_non_display_name_fields(client):
    """#625: the PATCH endpoint must refuse any field other
    than display_name so a caller can't smuggle a caller /
    principal edit through it."""
    _seed_principal(
        "guard@test.com",
        identity_key="guard@test.com",
        principal_type="assumed_role",
        principal_arn="arn:aws:iam::123:role/tg-consumer",
    )
    r = client.patch(
        "/api/users/guard@test.com",
        json={"principal_arn": "arn:aws:iam::123:role/evil"},
    )
    assert r.status_code == 400
    # And the real principal_arn is unchanged.
    body = client.get("/api/users/guard@test.com").json()
    assert body["principal_arn"] == \
        "arn:aws:iam::123:role/tg-consumer"


# ── PATCH bedrock_key_user (set / clear / 409 / authz) ──

def test_patch_sets_and_clears_bedrock_key_user(client):
    """PATCH sets then clears bedrock_key_user; the row surfaces it and
    a full-ARN / user/ form normalizes to the bare IAM-user name."""
    from db.session import get_db
    from db.models import User
    _seed_principal(
        "keyd@test.com", identity_key="keyd@test.com",
        principal_type="assumed_role",
        principal_arn="arn:aws:iam::123:role/tg-consumer")
    # Set (full ARN form) → stored as the bare name.
    r = client.patch(
        "/api/users/keyd@test.com",
        json={"bedrock_key_user":
              "arn:aws:iam::123:user/MantleApiKey-uhbhn79a"})
    assert r.status_code == 200, r.text
    assert r.json()["bedrock_key_user"] == "MantleApiKey-uhbhn79a"
    with get_db() as db:
        u = db.query(User).filter(
            User.email == "keyd@test.com").first()
        assert u.bedrock_key_user == "MantleApiKey-uhbhn79a"
    # Clear (null).
    r = client.patch(
        "/api/users/keyd@test.com",
        json={"bedrock_key_user": None})
    assert r.status_code == 200
    assert r.json()["bedrock_key_user"] is None
    # Clear via empty string too.
    client.patch("/api/users/keyd@test.com",
                 json={"bedrock_key_user": "x"})
    r = client.patch("/api/users/keyd@test.com",
                     json={"bedrock_key_user": ""})
    assert r.json()["bedrock_key_user"] is None


def test_patch_bedrock_key_user_409_on_collision(client):
    """Setting a key already mapped to ANOTHER user → 409 (would
    mis-attribute that key's spend); re-setting the SAME user's own
    value is idempotent (no false 409)."""
    for e in ("alice@test.com", "bob@test.com"):
        _seed_principal(
            e, identity_key=e, principal_type="assumed_role",
            principal_arn="arn:aws:iam::123:role/tg-consumer")
    r = client.patch("/api/users/alice@test.com",
                     json={"bedrock_key_user": "MantleApiKey-x"})
    assert r.status_code == 200, r.text
    # bob claims the same key → 409.
    r = client.patch("/api/users/bob@test.com",
                     json={"bedrock_key_user": "MantleApiKey-x"})
    assert r.status_code == 409, r.text
    assert "alice@test.com" in r.json()["detail"]
    # alice re-setting her own same value → still 200 (idempotent).
    r = client.patch("/api/users/alice@test.com",
                     json={"bedrock_key_user": "MantleApiKey-x"})
    assert r.status_code == 200, r.text


def test_patch_bedrock_key_user_is_org_admin_only(pg_url, clean_db):
    """bedrock_key_user drives fleet-wide attribution → org-admin ONLY.
    A member (even editing their OWN row) is 403'd, unlike display_name
    which is self-service."""
    import api.auth as auth_mod
    auth_mod._validate_request = (
        lambda req, db: ("member@test.com", "session"))
    from api.main import app
    from db.session import get_db
    from db.models import AdminRole, User
    with get_db() as db:
        db.add(AdminRole(email="member@test.com", role="member"))
        db.add(User(email="member@test.com", identity_key="member@test.com",
                    status="active"))
    with TestClient(app) as c:
        r = c.patch("/api/users/member@test.com",
                    json={"bedrock_key_user": "MantleApiKey-self"})
        assert r.status_code == 403, r.text


def test_patch_display_name_on_service_row_by_identity_key(
    client,
):
    """#625: service rows (email=None) are addressable by
    identity_key for the display_name PATCH too."""
    _seed_principal(
        "role:MyEcsTaskRole",
        identity_key="role:MyEcsTaskRole",
        principal_type="service",
        principal_arn="arn:aws:iam::123:role/MyEcsTaskRole",
    )
    r = client.patch(
        "/api/users/role:MyEcsTaskRole",
        json={"display_name": "Nightly batch"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == "Nightly batch"
    assert body["email"] is None
    assert body["is_service"] is True


def test_get_user_by_identity_key_falls_back_to_email(client):
    """#345 back-compat: legacy SPA bundles call
    GET /api/users/{email}; the route now also resolves by
    identity_key for service rows. Both shapes must work."""
    _seed_principal(
        "carol@test.com",
        identity_key="carol@test.com",
        principal_type="assumed_role",
        principal_arn=(
            "arn:aws:iam::123:role/tg-consumer"
        ),
    )
    by_email = client.get("/api/users/carol@test.com")
    assert by_email.status_code == 200
    by_id = client.get("/api/users/carol@test.com")
    assert by_id.status_code == 200
    assert by_email.json()["identity_key"] == \
        by_id.json()["identity_key"]


# ── #627 manage / unmanage + deny attach ──────────────

class _FakeIam:
    """Records attach/detach calls; can raise a ClientError.

    #799: also answers list_attached_role_policies (via a paginator)
    for the admin-role guard. `admin_roles` is the set of role names
    that should report AdministratorAccess attached."""
    def __init__(self, admin_roles=None):
        self.attached = []
        self.detached = []
        self.attach_error = None
        self.detach_error = None
        self.admin_roles = set(admin_roles or [])
        # #1065: tg-consumer's live AssumeRolePolicyDocument, mutated by
        # get_role / update_assume_role_policy for the IDC-trust path.
        self.trust_doc = {"Version": "2012-10-17", "Statement": []}
        self.assume_updates = []   # records each PolicyDocument written

    def attach_role_policy(self, RoleName, PolicyArn):
        if self.attach_error:
            raise self.attach_error
        self.attached.append((RoleName, PolicyArn))

    def detach_role_policy(self, RoleName, PolicyArn):
        if self.detach_error:
            raise self.detach_error
        self.detached.append((RoleName, PolicyArn))

    # #1065: trust-policy surgery for the IDC Govern→tg-consumer wire.
    def get_role(self, RoleName):
        return {"Role": {"AssumeRolePolicyDocument": self.trust_doc}}

    def update_assume_role_policy(self, RoleName, PolicyDocument):
        import json
        self.trust_doc = json.loads(PolicyDocument)
        self.assume_updates.append((RoleName, self.trust_doc))

    def get_paginator(self, name):
        fake = self

        class _Pager:
            def paginate(self, RoleName):
                admin = "arn:aws:iam::aws:policy/AdministratorAccess"
                pols = ([{"PolicyArn": admin}]
                        if RoleName in fake.admin_roles else [])
                return [{"AttachedPolicies": pols}]
        return _Pager()


@pytest.fixture
def fake_iam_users(monkeypatch):
    """Patch the AWS seam so manage/unmanage don't hit real AWS.

    We patch `api.aws_session.get_aws_session` (NOT the users
    module's `_iam_client`) because an earlier test in this file
    reloads `api.routes.users` — the app's mounted routes then
    reference the original module's globals, so patching the
    reloaded module object would miss them. `_iam_client` does
    `from api.aws_session import get_aws_session` at call time,
    so patching that import target works for either module
    object. AWS_ACCOUNT_ID is set so `_deny_policy_arn` builds
    the ARN without an STS call."""
    iam = _FakeIam()

    class _FakeSession:
        def client(self, name):
            return iam

    import api.aws_session as aws_session
    monkeypatch.setattr(
        aws_session, "get_aws_session", lambda: _FakeSession())
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123")
    return iam


def _seed_principal_full(email, identity_key, principal_type,
                         principal_arn, role_type="iam",
                         governed=False, status="active"):
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        u = db.query(User).filter(User.email == email).first()
        if not u:
            u = User(email=email, status="active")
            db.add(u)
        u.identity_key = identity_key
        u.principal_type = principal_type
        u.principal_arn = principal_arn
        u.role_type = role_type
        u.governed = governed
        u.status = status
        # #827: a force_blocked seed should look like one the
        # force-block endpoint produced (force_blocked_at set).
        u.force_blocked_at = (
            datetime.now(timezone.utc)
            if status == "force_blocked" else None)


def test_manage_attaches_and_marks_governed(
    client, fake_iam_users
):
    """#627: manage attaches tg-BedrockQuotaDeny to the role
    and flips governed=true."""
    _seed_principal_full(
        "dev@test.com", "dev@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer")
    r = client.post("/api/users/dev@test.com/manage")
    assert r.status_code == 200, r.text
    assert r.json()["governed"] is True
    assert fake_iam_users.attached == [
        ("tg-consumer",
         "arn:aws:iam::123:policy/tg-BedrockQuotaDeny")]


def test_manage_idc_principal_governs_without_attach(
    client, fake_iam_users
):
    """#1011: an AWSReservedSSO_* (role_type=idc) principal is now
    GOVERNABLE — manage sets governed=true and emits the per-person
    QuotaDeny via the reconciler, but does NOT call AttachRolePolicy
    on the IDC role (a direct attach is wiped on re-provision, #618).
    #1065: instead it adds the dev's SSO role to tg-consumer's trust
    (path-form, ArnLike wildcard) so the deny actually evaluates."""
    _seed_principal_full(
        "idc@test.com", "idc@test.com", "assumed_role",
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_Dev_abc",
        role_type="idc")
    r = client.post("/api/users/idc@test.com/manage")
    assert r.status_code == 200
    # governed flips true...
    assert r.json()["governed"] is True
    body = client.get("/api/users/idc@test.com").json()
    assert body["governed"] is True
    # ...but NO AttachRolePolicy on the AWSReservedSSO_* role.
    assert fake_iam_users.attached == []
    # #1065: tg-consumer's trust now ArnLike-trusts the permission set
    # with a WILDCARD suffix (survives IDC re-provision).
    stmts = fake_iam_users.trust_doc["Statement"]
    arnlikes = [
        s.get("Condition", {}).get("ArnLike", {}).get("aws:PrincipalArn")
        for s in stmts]
    assert ("arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
            "AWSReservedSSO_Dev_*") in arnlikes
    # exactly one tg-govern statement
    assert sum(1 for s in stmts
               if str(s.get("Sid", "")).startswith("TgGovernIdc")) == 1


def test_manage_idc_two_users_one_permset_single_trust(
    client, fake_iam_users
):
    """#1065: two governed users on the SAME permission set collapse to
    ONE trust entry (keyed per permission set, not per suffix/user)."""
    _seed_principal_full(
        "a@test.com", "a@test.com", "assumed_role",
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_Dev_aaa", role_type="idc")
    _seed_principal_full(
        "b@test.com", "b@test.com", "assumed_role",
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_Dev_bbb", role_type="idc")
    client.post("/api/users/a@test.com/manage")
    client.post("/api/users/b@test.com/manage")
    govern = [s for s in fake_iam_users.trust_doc["Statement"]
              if str(s.get("Sid", "")).startswith("TgGovernIdc")]
    assert len(govern) == 1            # one entry for the shared permset


def test_unmanage_idc_principal_no_detach_removes_trust_when_last(
    client, fake_iam_users
):
    """#1011: ungoverning an IDC principal clears governed and never
    calls DetachRolePolicy on the AWSReservedSSO_* role. #1065: it DOES
    remove that permission set's trust from tg-consumer when no other
    governed user shares it."""
    _seed_principal_full(
        "idc2@test.com", "idc2@test.com", "assumed_role",
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_Dev_xyz",
        role_type="idc")
    client.post("/api/users/idc2@test.com/manage")
    assert any(str(s.get("Sid", "")).startswith("TgGovernIdc")
               for s in fake_iam_users.trust_doc["Statement"])
    r = client.post("/api/users/idc2@test.com/unmanage")
    assert r.status_code == 200
    assert r.json()["governed"] is False
    # No detach attempted on the IDC role.
    assert fake_iam_users.detached == []
    # last governed user on the permset → trust removed.
    assert not any(str(s.get("Sid", "")).startswith("TgGovernIdc")
                   for s in fake_iam_users.trust_doc["Statement"])


def test_unmanage_idc_keeps_trust_when_peer_still_governed(
    client, fake_iam_users
):
    """#1065: ungoverning one user on a permission set KEEPS the trust
    while another governed user shares it."""
    _seed_principal_full(
        "p1@test.com", "p1@test.com", "assumed_role",
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_Dev_p1", role_type="idc")
    _seed_principal_full(
        "p2@test.com", "p2@test.com", "assumed_role",
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_Dev_p2", role_type="idc")
    client.post("/api/users/p1@test.com/manage")
    client.post("/api/users/p2@test.com/manage")
    client.post("/api/users/p1@test.com/unmanage")
    # p2 still governed on the same permset → trust stays.
    assert any(str(s.get("Sid", "")).startswith("TgGovernIdc")
               for s in fake_iam_users.trust_doc["Statement"])


# ─── #809 (reverses #799/#804): admin roles are now manageable ───────

@pytest.fixture
def fake_iam_admin_role(monkeypatch):
    """Like fake_iam_users, but the role carries AdministratorAccess.
    #809: this must NO LONGER cause a refusal — denylist semantics
    make a role-wide attach safe."""
    iam = _FakeIam(admin_roles={"tg-consumer"})

    class _FakeSession:
        def client(self, name):
            return iam

    import api.aws_session as aws_session
    monkeypatch.setattr(
        aws_session, "get_aws_session", lambda: _FakeSession())
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123")
    return iam


def test_manage_admin_role_attaches_no_refusal(
    client, fake_iam_admin_role
):
    """#809 (reverses #799): manage on an AdministratorAccess role now
    ENROLLS (200, governed=true) AND attaches the bundled deny — no
    409, no admin guard. Denylist semantics make this safe."""
    _seed_principal_full(
        "boss@test.com", "boss@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer")
    r = client.post("/api/users/boss@test.com/manage")
    assert r.status_code == 200, r.text
    assert r.json()["governed"] is True
    assert fake_iam_admin_role.attached == [
        ("tg-consumer",
         "arn:aws:iam::123:policy/tg-BedrockQuotaDeny")]


def test_force_block_allowed_on_admin_role(client, fake_iam_admin_role):
    """#809 (reverses #799): force-block on an admin role now SUCCEEDS
    — the reconciler attaches the deny to that role (safe under
    denylist semantics). #864: the principal must be GOVERNED first
    (enforcement precondition), so seed governed=True."""
    _seed_principal_full(
        "boss@test.com", "boss@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer", governed=True)
    r = client.post("/api/users/boss@test.com/force-block", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "force_blocked"


def test_force_block_allowed_on_non_admin_role(client, fake_iam_users):
    """A normal (non-admin) GOVERNED role still force-blocks fine."""
    _seed_principal_full(
        "dev@test.com", "dev@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer", governed=True)
    r = client.post("/api/users/dev@test.com/force-block", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "force_blocked"


def test_force_block_refused_when_no_role(client, fake_iam_users):
    """#809: the genuinely-unenforceable guard stays — a principal
    with NO IAM role ARN (iam_user/root) has nothing to attach a deny
    to → force-block refuses (409), not a false success."""
    _seed_principal_full(
        "rooty@test.com", "rooty@test.com", "iam_user", None)
    r = client.post("/api/users/rooty@test.com/force-block", json={})
    assert r.status_code == 409, r.text
    assert "no IAM role" in r.json()["detail"]
    assert client.get(
        "/api/users/rooty@test.com").json()["status"] != "force_blocked"


def test_force_block_refused_when_not_governed(client, fake_iam_users):
    """#864: force-block on a NON-governed principal (with a real role
    ARN) must refuse with 409 — not flip the DB status to a false
    'blocked'. The reconciler gates all enforcement on governed and
    self-heals an ungoverned force_blocked row back to active, so a
    200 here would be a toast-vs-effect lie (same class as #799).
    Manage (enroll) first, then block."""
    _seed_principal_full(
        "ungov@test.com", "ungov@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer", governed=False)
    r = client.post("/api/users/ungov@test.com/force-block", json={})
    assert r.status_code == 409, r.text
    assert "not governed" in r.text.lower()
    # the DB status must NOT have flipped — no false 'blocked'.
    g = client.get("/api/users/ungov@test.com")
    assert g.json()["status"] == "active"
    assert g.json()["force_blocked_at"] is None


def test_manage_idempotent_when_already_attached(
    client, fake_iam_users
):
    """#627: AttachRolePolicy raising EntityAlreadyExists is
    treated as success (idempotent manage)."""
    from botocore.exceptions import ClientError
    fake_iam_users.attach_error = ClientError(
        {"Error": {"Code": "EntityAlreadyExists",
                   "Message": "already attached"}},
        "AttachRolePolicy")
    _seed_principal_full(
        "dev@test.com", "dev@test.com", "assumed_role",
        "arn:aws:iam::123:role/tg-consumer")
    r = client.post("/api/users/dev@test.com/manage")
    assert r.status_code == 200
    assert r.json()["governed"] is True


# ─── #946: record a role ARN on a pre-registered user, govern w/o spend ───

def test_set_principal_arn_then_govern_no_spend(
    client, fake_iam_users
):
    """#946: an admin records a role ARN on a pre-registered
    (principal_arn=null) user, then Govern succeeds and attaches
    the deny — with NO Bedrock spend / CUR row."""
    # Pre-registered: created by the preregister endpoint, no ARN.
    r = client.post(
        "/api/users/preregister",
        json={"email": "pre@test.com", "team_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["principal_arn"] is None
    # Record the role ARN.
    r = client.post(
        "/api/users/pre@test.com/principal-arn",
        json={"principal_arn":
              "arn:aws:iam::123:role/tg-consumer"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["principal_arn"] == \
        "arn:aws:iam::123:role/tg-consumer"
    assert body["principal_type"] == "assumed_role"
    assert body["role_type"] == "iam"
    # Govern now works — no spend row was ever created.
    r = client.post("/api/users/pre@test.com/manage")
    assert r.status_code == 200, r.text
    assert r.json()["governed"] is True
    assert fake_iam_users.attached == [
        ("tg-consumer",
         "arn:aws:iam::123:policy/tg-BedrockQuotaDeny")]


def test_set_principal_arn_rejects_non_role_arn(client):
    """#946: an IAM-user / root ARN is rejected — only a role ARN
    is attachable."""
    client.post("/api/users/preregister",
                json={"email": "u@test.com", "team_id": None})
    r = client.post(
        "/api/users/u@test.com/principal-arn",
        json={"principal_arn": "arn:aws:iam::123:user/bob"})
    assert r.status_code == 400
    assert "role ARN" in r.json()["detail"]
    # principal_arn stays null.
    assert client.get(
        "/api/users/u@test.com").json()["principal_arn"] is None


def test_set_principal_arn_rejects_idc_role(client):
    """#946: an AWSReservedSSO_* role ARN is rejected — IDC roles
    are never tg-governable."""
    client.post("/api/users/preregister",
                json={"email": "i@test.com", "team_id": None})
    r = client.post(
        "/api/users/i@test.com/principal-arn",
        json={"principal_arn":
              "arn:aws:iam::123:role/AWSReservedSSO_Dev_abc"})
    assert r.status_code == 409
    assert "IDC" in r.json()["detail"]


def test_set_principal_arn_rejects_cross_account(
    client, monkeypatch
):
    """#946: an ARN from a different account than the deployment
    is rejected — tg can only attach in its own account."""
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123")
    client.post("/api/users/preregister",
                json={"email": "x@test.com", "team_id": None})
    r = client.post(
        "/api/users/x@test.com/principal-arn",
        json={"principal_arn":
              "arn:aws:iam::123456789012:role/tg-consumer"})
    assert r.status_code == 400
    assert "account" in r.json()["detail"].lower()


def test_set_principal_arn_requires_a_value(client):
    """#946: an empty / missing principal_arn → 400."""
    client.post("/api/users/preregister",
                json={"email": "e@test.com", "team_id": None})
    r = client.post(
        "/api/users/e@test.com/principal-arn",
        json={"principal_arn": "  "})
    assert r.status_code == 400


def test_unmanage_detaches_when_last_on_role(
    client, fake_iam_users
):
    """#627: unmanage detaches the policy when no other
    governed principal remains on the role."""
    _seed_principal_full(
        "solo@test.com", "solo@test.com", "assumed_role",
        "arn:aws:iam::123:role/MyRole", governed=True)
    r = client.post("/api/users/solo@test.com/unmanage")
    assert r.status_code == 200, r.text
    assert r.json()["governed"] is False
    assert r.json()["policy_detached"] is True
    assert fake_iam_users.detached == [
        ("MyRole",
         "arn:aws:iam::123:policy/tg-BedrockQuotaDeny")]


def test_unmanage_keeps_policy_when_others_share_role(
    client, fake_iam_users
):
    """#627: when another governed principal still uses
    the role, unmanage clears only this one's flag and leaves
    the policy attached (don't yank model restriction off the
    others)."""
    _seed_principal_full(
        "a@test.com", "a@test.com", "assumed_role",
        "arn:aws:iam::123:role/Shared", governed=True)
    _seed_principal_full(
        "b@test.com", "b@test.com", "assumed_role",
        "arn:aws:iam::123:role/Shared", governed=True)
    r = client.post("/api/users/a@test.com/unmanage")
    assert r.status_code == 200
    assert r.json()["governed"] is False
    assert r.json()["policy_detached"] is False
    # No detach call — b@ still governed on the role.
    assert fake_iam_users.detached == []
    # b@ untouched.
    assert client.get(
        "/api/users/b@test.com").json()["governed"] is True


# ── #827: unmanage clears a manual force-block ──────────────

def test_unmanage_clears_force_block(client, fake_iam_users):
    """#827: unmanaging a force_blocked principal flips it to
    active + clears force_blocked_at + ungoverns, all in one
    request — so the reconciler won't re-add the deny (which it
    would for any force_blocked user regardless of governed)."""
    _seed_principal_full(
        "fb@test.com", "fb@test.com", "assumed_role",
        "arn:aws:iam::123:role/SoloRole", governed=True,
        status="force_blocked")
    r = client.post("/api/users/fb@test.com/unmanage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["governed"] is False
    assert body["status"] == "active"
    assert body["force_blocked_at"] is None
    # the response flags the lifted force-block for the UI toast
    assert body["unblocked"] is True
    # last on the role → whole policy detached
    assert body["policy_detached"] is True
    # persisted, not just echoed
    after = client.get("/api/users/fb@test.com").json()
    assert after["status"] == "active"
    assert after["force_blocked_at"] is None
    assert after["governed"] is False


def test_unmanage_active_principal_not_flagged_unblocked(
    client, fake_iam_users
):
    """#827: unmanaging an ordinary (active) principal does NOT
    set unblocked — the toast stays plain 'Unmanaged'."""
    _seed_principal_full(
        "act@test.com", "act@test.com", "assumed_role",
        "arn:aws:iam::123:role/SoloRole2", governed=True,
        status="active")
    r = client.post("/api/users/act@test.com/unmanage")
    assert r.status_code == 200
    body = r.json()
    assert body["governed"] is False
    assert body["status"] == "active"
    assert body["unblocked"] is False


# ── #929: member self-read of own data (get_user self-allow) ──

def test_member_can_read_own_row(pg_url, clean_db):
    """#929: a member (authz row, no admin) can GET their OWN /users
    row — the member-view's core query. Was admin-only (403'd them)."""
    import api.auth as auth_mod
    auth_mod._validate_request = (
        lambda req, db: ("member@test.com", "session"))
    from api.main import app
    from db.session import get_db
    from db.models import AdminRole, User
    with get_db() as db:
        db.add(AdminRole(email="member@test.com", role="member"))
        db.add(User(email="member@test.com", status="active",
                    cap_usd=10.0))
    with TestClient(app) as c:
        r = c.get("/api/users/member@test.com")
        assert r.status_code == 200
        assert r.json()["email"] == "member@test.com"


def test_member_cannot_read_other_users_row(pg_url, clean_db):
    """#929 security: a member reading ANOTHER user's row → 403
    (server-side, not just hidden UI)."""
    import api.auth as auth_mod
    auth_mod._validate_request = (
        lambda req, db: ("member@test.com", "session"))
    from api.main import app
    from db.session import get_db
    from db.models import AdminRole, User
    with get_db() as db:
        db.add(AdminRole(email="member@test.com", role="member"))
        db.add(User(email="member@test.com", status="active"))
        db.add(User(email="someone-else@test.com", status="active"))
    with TestClient(app) as c:
        r = c.get("/api/users/someone-else@test.com")
        assert r.status_code == 403
