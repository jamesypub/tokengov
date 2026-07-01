"""
Smoke tests — verify the FastAPI app starts and basic routes respond.
Uses testcontainers to spin up a real Postgres.
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def pg():
    with PostgresContainer("postgres:16") as pg:
        yield pg


@pytest.fixture(scope="session")
def client(pg, monkeypatch_session):
    import os
    os.environ["DATABASE_URL"] = pg.get_connection_url()
    # If db.session was already imported earlier in the test
    # session (e.g. by test_auth_dispatch.py importing api.auth),
    # `engine` is bound to the localhost fallback. Rebind it to
    # the testcontainer before the app touches the DB.
    import db.session as _dbs
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    _dbs.DATABASE_URL = pg.get_connection_url()
    _dbs.engine = create_engine(
        _dbs.DATABASE_URL, pool_pre_ping=True)
    _dbs.SessionLocal = sessionmaker(
        bind=_dbs.engine, autocommit=False, autoflush=False)
    # Patch out auth — return a fixed admin email for all test
    # requests via the dispatcher. #547: use monkeypatch (auto-
    # reverts at session teardown) instead of a raw rebind, which
    # would leak the stub into later tests.
    # #576: the legacy _validate_sigv4 stub is gone (desktop path
    # deleted); stubbing _validate_request alone covers all routes.
    import api.auth as auth_mod
    monkeypatch_session.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"))

    from api.main import app
    from db.session import engine
    from db.models import Base
    Base.metadata.create_all(bind=engine)

    # Seed an org_admin role so routes pass auth
    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        if not db.query(AdminRole).filter(AdminRole.email == "admin@test.com").first():
            db.add(AdminRole(email="admin@test.com", role="org_admin"))

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def monkeypatch_session():
    """Session-scoped monkeypatch for fixture use."""
    import _pytest.monkeypatch
    mp = _pytest.monkeypatch.MonkeyPatch()
    yield mp
    mp.undo()


def test_version(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    assert "version" in r.json()


# #791: the version-keyed UAT dedup/trigger breaks when a non-dev
# environment serves the unstamped "dev". /api/version flags it.

def test_version_stamped_value_is_returned(client, monkeypatch):
    monkeypatch.setenv("TG_VERSION", "stage-abc1234")
    monkeypatch.setenv("TG_ENVIRONMENT", "stage")
    body = client.get("/api/version").json()
    assert body["version"] == "stage-abc1234"
    # a real stamp is never flagged unstamped.
    assert "unstamped" not in body


def test_version_unstamped_flagged_on_non_dev_env(client, monkeypatch):
    # stage/prod serving "dev" = the regression #791 is about.
    monkeypatch.setenv("TG_VERSION", "dev")
    monkeypatch.setenv("TG_ENVIRONMENT", "stage")
    body = client.get("/api/version").json()
    assert body["version"] == "dev"
    assert body.get("unstamped") is True


def test_version_dev_env_not_flagged(client, monkeypatch):
    # a genuine dev environment serving "dev" is expected, not a bug.
    monkeypatch.setenv("TG_VERSION", "dev")
    monkeypatch.setenv("TG_ENVIRONMENT", "dev")
    body = client.get("/api/version").json()
    assert body["version"] == "dev"
    assert "unstamped" not in body


def test_list_users_empty(client):
    r = client.get("/api/users")
    assert r.status_code == 200
    assert "users" in r.json()


def test_create_and_get_user(client):
    r = client.post("/api/users", json={"email": "alice@test.com"})
    assert r.status_code == 200
    assert r.json()["email"] == "alice@test.com"

    r = client.get("/api/users/alice@test.com")
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_governance_drift_count_empty(client):
    """#649: 0 (and sweep_at=None) before any sweep has run."""
    r = client.get("/api/governance/drift-count")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 0
    assert body["sweep_at"] is None


def test_governance_drift_count_reflects_latest_sweep(client):
    """#649: count = rows in the LATEST sweep; an older sweep's
    rows don't inflate it."""
    from datetime import datetime, timezone, timedelta
    from db.session import get_db
    from db.models import GovernanceDrift
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=1)
    with get_db() as db:
        # older sweep: 2 rows (must NOT count)
        for e in ("x@t.com", "y@t.com"):
            db.add(GovernanceDrift(
                sweep_at=old, identity_key=e, email=e,
                direction="governed_no_deny",
                expected="managed", actual="deny-not-attached"))
        # latest sweep: 1 row
        db.add(GovernanceDrift(
            sweep_at=now, identity_key="z@t.com", email="z@t.com",
            direction="governed_no_deny",
            expected="managed", actual="deny-not-attached"))
    r = client.get("/api/governance/drift-count")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1   # only the latest sweep

    r = client.get("/api/governance/drift")
    assert r.status_code == 200, r.text
    rows = r.json()["drift"]
    assert len(rows) == 1
    assert rows[0]["identity_key"] == "z@t.com"


def test_governance_drift_requires_org_admin(client):
    """#649: both endpoints are org-admin scoped."""
    import api.auth as auth_mod
    original = auth_mod._validate_request
    auth_mod._validate_request = (
        lambda req, db: ("nobody@test.com", "sigv4"))
    try:
        assert client.get(
            "/api/governance/drift-count").status_code == 403
        assert client.get(
            "/api/governance/drift").status_code == 403
    finally:
        auth_mod._validate_request = original


def test_models_catalog(client):
    r = client.get("/api/models/catalog")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "models" in body
    models = body["models"]
    # Active Bedrock-supported set as of 2026-05-28
    # (Opus 4.8 added on Bedrock launch).
    expected_ids = {
        "us.anthropic.claude-opus-4-8",
        "us.anthropic.claude-opus-4-7",
        "us.anthropic.claude-opus-4-6-v1",
        "us.anthropic.claude-opus-4-5-20251101-v1:0",
        "us.anthropic.claude-opus-4-1-20250805-v1:0",
        "us.anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    }
    got_ids = {m["model_id"] for m in models}
    assert got_ids == expected_ids, got_ids
    # Every entry has the four price fields, all positive numbers.
    for m in models:
        for f in (
            "input_per_1m", "output_per_1m",
            "cache_write_per_1m", "cache_read_per_1m",
        ):
            v = m.get(f)
            assert isinstance(v, (int, float)) and v > 0, (m, f, v)
    # No deprecated entries.
    assert "us.anthropic.claude-3-5-haiku-20241022-v1:0" not in got_ids


def test_default_policy(client):
    r = client.get("/api/policy/default")
    assert r.status_code == 200

    r = client.put("/api/policy/default", json={"monthly_cap_usd": 50.0})
    assert r.status_code == 200
    assert r.json()["monthly_cap_usd"] == 50.0


def test_admin_config_org_default_quota(client):
    """GET /api/admin/config exposes org_default_quota_usd;
    PUT persists a new value (#269)."""
    r = client.get("/api/admin/config")
    assert r.status_code == 200
    assert "org_default_quota_usd" in r.json()

    r = client.put(
        "/api/admin/config",
        json={"org_default_quota_usd": 1234.5},
    )
    assert r.status_code == 200, r.text
    assert r.json()["org_default_quota_usd"] == 1234.5

    r = client.get("/api/admin/config")
    assert r.json()["org_default_quota_usd"] == 1234.5

    # Negative is rejected.
    r = client.put(
        "/api/admin/config",
        json={"org_default_quota_usd": -1},
    )
    assert r.status_code == 400


def test_users_include_effective_quota_usd(client):
    """list + get expose effective_quota_usd: explicit cap_usd
    if set, else the org default (#269)."""
    client.put(
        "/api/admin/config",
        json={"org_default_quota_usd": 800.0},
    )
    # User with no cap → effective == org default.
    r = client.post(
        "/api/users",
        json={"email": "noquota@test.com"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["effective_quota_usd"] == 800.0

    # User with explicit cap → unaffected by org default.
    client.post("/api/users", json={"email": "explicit@test.com"})
    r = client.put(
        "/api/users/explicit@test.com/cap",
        json={"cap_usd": 12.5},
    )
    assert r.json()["effective_quota_usd"] == 12.5

    r = client.get("/api/users")
    by_email = {u["email"]: u for u in r.json()["users"]}
    assert by_email["noquota@test.com"]["effective_quota_usd"] == 800.0
    assert by_email["explicit@test.com"]["effective_quota_usd"] == 12.5

    r = client.get("/api/users/noquota@test.com")
    assert r.json()["effective_quota_usd"] == 800.0


def test_teams_crud(client):
    r = client.post("/api/teams", json={"name": "Engineering"})
    assert r.status_code == 200
    team_id = r.json()["team_id"]

    r = client.get("/api/teams")
    assert any(t["team_id"] == team_id for t in r.json()["teams"])

    r = client.delete(f"/api/teams/{team_id}")
    assert r.status_code == 200


def test_team_budget_persists_and_aggregates_spend(client):
    """#337: budget_usd round-trips on POST/PUT; GET /teams
    returns budget_usd + spend_usd aggregated from members'
    CurUserSpend rows for the current month."""
    from datetime import datetime, timezone
    from db.session import get_db
    from db.models import CurUserSpend, User

    # 1. Create with budget.
    r = client.post(
        "/api/teams",
        json={"name": "B-Test", "budget_usd": 500},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    team_id = body["team_id"]
    assert body["budget_usd"] == 500
    assert body.get("spend_usd", 0) == 0.0

    # 2. PUT can clear and PUT can update.
    r = client.put(
        f"/api/teams/{team_id}",
        json={"budget_usd": None},
    )
    assert r.status_code == 200
    assert r.json()["budget_usd"] is None
    r = client.put(
        f"/api/teams/{team_id}",
        json={"budget_usd": 250.5},
    )
    assert r.status_code == 200
    assert r.json()["budget_usd"] == 250.5

    # 3. Negative is rejected.
    r = client.put(
        f"/api/teams/{team_id}",
        json={"budget_usd": -1},
    )
    assert r.status_code == 400

    # 4. Aggregate spend pulls from members' CurUserSpend rows
    #    via User.team_id (primary team only — no splitting).
    usage_hour = datetime.now(timezone.utc).date()  # #643
    with get_db() as db:
        u = db.query(User).filter(
            User.email == "alice@test.com").first()
        if u:
            u.team_id = team_id
        else:
            db.add(User(
                email="alice@test.com",
                status="active",
                team_id=team_id,
            ))
        db.add(CurUserSpend(
            email="alice@test.com",
            usage_hour=usage_hour,
            model_id="us.anthropic.claude-sonnet-4-6",
            input_tokens=0, output_tokens=0,
            total_tokens=0,
            spend_usd=12.34,
        ))
    r = client.get("/api/teams")
    assert r.status_code == 200
    by_id = {t["team_id"]: t for t in r.json()["teams"]}
    assert by_id[team_id]["budget_usd"] == 250.5
    assert by_id[team_id]["spend_usd"] == 12.34


def test_jobs_empty(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    # /api/jobs returns {"runs": [...]} — the route surfaces the
    # job_runs table, not a list of registered jobs.
    assert "runs" in r.json()


def test_analytics_queries_workgroup_missing_returns_flag(
    client, monkeypatch
):
    """#181: when tg-cur-athena (optional) isn't deployed,
    /api/analytics/queries must return cur_not_configured=true
    instead of a 500. The SPA renders a friendly "CUR not
    configured" panel from this flag."""
    import api.routes.analytics as a
    from botocore.exceptions import ClientError

    err = ClientError(
        {
            "Error": {
                "Code": "InvalidRequestException",
                "Message": (
                    "WorkGroup is not found. "
                    "WorkGroup: tg-cur-analytics"
                ),
            }
        },
        "ListNamedQueries",
    )

    class FakePaginator:
        def paginate(self, **_):
            raise err

    class FakeAthena:
        def get_paginator(self, _name):
            return FakePaginator()

    # #590: routes reach Athena via
    # get_aws_session().client("athena") (the api's own task-role
    # creds — no assume-hop). Stub the session to return our
    # FakeAthena directly.
    fake_session = MagicMock()
    fake_session.client.return_value = FakeAthena()
    monkeypatch.setattr(
        a, "get_aws_session", lambda: fake_session,
    )

    r = client.get("/api/analytics/queries")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queries"] == []
    assert body["cur_not_configured"] is True
    assert body["workgroup"] == "tg-cur-analytics"


def test_analytics_run_table_not_found_uses_env_db_name(
    client, monkeypatch
):
    """Issue #98 / #120: when CUR data hasn't landed yet,
    the 503 body must name the DB + table resolved from
    ATHENA_DATABASE + CUR_TABLE_NAME, NOT a hardcoded
    literal that drifts away from whatever the stack
    actually deployed."""
    import api.routes.analytics as a
    monkeypatch.setattr(a, "ATHENA_DATABASE", "tg_cur")
    monkeypatch.setattr(a, "CUR_TABLE_NAME", "custom_tbl")
    monkeypatch.setattr(
        a, "ATHENA_RESULTS_BUCKET", "s3://x/")

    class FakeAthena:
        def get_named_query(self, NamedQueryId):
            return {"NamedQuery": {
                "QueryString":
                    'SELECT 1 FROM "tg_cur"."custom_tbl"',
            }}

        def start_query_execution(self, **_):
            return {"QueryExecutionId": "exec-1"}

        def get_query_execution(self, QueryExecutionId):
            return {"QueryExecution": {"Status": {
                "State": "FAILED",
                "StateChangeReason":
                    "TABLE_NOT_FOUND: line 6:6: Table "
                    "'awsdatacatalog.tg_cur.custom_tbl' "
                    "does not exist",
            }}}

    # #590: route uses get_aws_session() (task-role creds)
    fake_session = MagicMock()
    fake_session.client.return_value = FakeAthena()
    monkeypatch.setattr(
        a, "get_aws_session", lambda: fake_session,
    )

    r = client.post(
        "/api/analytics/run",
        json={"query_id": "q-1", "refresh": True},
    )
    assert r.status_code == 503
    body = r.json()
    detail = body.get("detail", "")
    assert "tg_cur.custom_tbl" in detail, detail
    assert "cc_cur.data" not in detail, detail


# ── #354 auto-pricing API ───────────────────────────────

def _seed_proposed_pricing(model_id, rates):
    from db.session import get_db
    from db.models import ModelPricing
    from datetime import datetime, timezone
    with get_db() as db:
        existing = (
            db.query(ModelPricing)
            .filter(ModelPricing.model_id == model_id)
            .first()
        )
        if existing:
            existing.status = "proposed"
            existing.input_per_1m = rates["input_per_1m"]
            existing.output_per_1m = rates["output_per_1m"]
            existing.cache_write_per_1m = rates["cache_write_per_1m"]
            existing.cache_read_per_1m = rates["cache_read_per_1m"]
            existing.proposed_at = datetime.now(timezone.utc)
        else:
            db.add(ModelPricing(
                model_id=model_id,
                input_per_1m=rates["input_per_1m"],
                output_per_1m=rates["output_per_1m"],
                cache_write_per_1m=rates["cache_write_per_1m"],
                cache_read_per_1m=rates["cache_read_per_1m"],
                status="proposed",
                source="aws_pricing_api",
                proposed_at=datetime.now(timezone.utc),
            ))


# ── #346 service-account-caps API ───────────────────────

def test_service_account_cap_crud(client):
    payload = {
        "budget_usd": 100.0,
        "period": "month",
        "mode": "alert_only",
        "alert_threshold_pct": 80,
        "owner_emails": "owner@test.com",
    }
    r = client.put(
        "/api/service-account-caps/role:MyEcsTaskRole",
        json=payload,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["identity_key"] == "role:MyEcsTaskRole"
    assert body["budget_usd"] == 100.0
    assert body["mode"] == "alert_only"

    # Round-trip
    r = client.get(
        "/api/service-account-caps/role:MyEcsTaskRole"
    )
    assert r.status_code == 200
    assert r.json()["budget_usd"] == 100.0

    # List
    r = client.get("/api/service-account-caps")
    assert r.status_code == 200
    keys = [c["identity_key"] for c in r.json()["caps"]]
    assert "role:MyEcsTaskRole" in keys

    # Delete
    r = client.delete(
        "/api/service-account-caps/role:MyEcsTaskRole"
    )
    assert r.status_code == 200


def test_service_account_cap_validation(client):
    # Bad period
    r = client.put(
        "/api/service-account-caps/role:foo",
        json={
            "budget_usd": 10.0,
            "period": "fortnight",
            "mode": "alert_only",
            "owner_emails": "a@test.com",
        },
    )
    assert r.status_code == 400

    # Bad mode
    r = client.put(
        "/api/service-account-caps/role:foo",
        json={
            "budget_usd": 10.0,
            "period": "month",
            "mode": "block_only",
            "owner_emails": "a@test.com",
        },
    )
    assert r.status_code == 400

    # Negative budget
    r = client.put(
        "/api/service-account-caps/role:foo",
        json={
            "budget_usd": -1.0,
            "period": "month",
            "mode": "alert_only",
            "owner_emails": "a@test.com",
        },
    )
    assert r.status_code == 400

    # Missing owner_emails when mode != disabled
    r = client.put(
        "/api/service-account-caps/role:foo",
        json={
            "budget_usd": 10.0,
            "period": "month",
            "mode": "alert_only",
            "owner_emails": "",
        },
    )
    assert r.status_code == 400


def test_service_account_manual_unblock(client):
    """POST /unblock clears blocked_at; the next monitor
    tick removes the inline deny."""
    from db.session import get_db
    from db.models import ServiceAccountCap
    from datetime import datetime, timezone
    with get_db() as db:
        db.add(ServiceAccountCap(
            identity_key="role:UnblockMe",
            budget_usd=10.0,
            period="month",
            mode="alert_and_block",
            owner_emails="o@test.com",
            blocked_at=datetime.now(timezone.utc),
            created_by="admin@test.com",
        ))
    r = client.post(
        "/api/service-account-caps/unblock?identity_key=role:UnblockMe"
    )
    assert r.status_code == 200, r.text
    assert r.json()["blocked"] is False


def test_service_account_alerts_listing(client):
    from db.session import get_db
    from db.models import (
        ServiceAccountAlert, ServiceAccountCap,
    )
    with get_db() as db:
        db.add(ServiceAccountCap(
            identity_key="role:AlertMe",
            budget_usd=10.0,
            period="month",
            mode="alert_only",
            owner_emails="o@test.com",
            created_by="admin@test.com",
        ))
        db.add(ServiceAccountAlert(
            identity_key="role:AlertMe",
            kind="threshold",
            pct_of_budget=82.5,
            period_key="2026-05",
        ))
    r = client.get(
        "/api/service-account-caps/alerts?identity_key=role:AlertMe"
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["alerts"]) == 1
    assert body["alerts"][0]["kind"] == "threshold"


# ── #726: CUR health endpoint ────────────────────────────
def _set_cur_probe(monkeypatch, columns, rows, err):
    import api.routes.cur as cur
    monkeypatch.setattr(cur, "_probe", lambda: (columns, rows, err))


def _set_manifest(monkeypatch, columns):
    # #749: stub the S3-manifest column-set probe. `columns=None`
    # simulates an unreadable manifest (fall back to present-but-empty).
    import api.routes.cur as cur
    monkeypatch.setattr(cur, "_manifest_columns", lambda: columns)


def test_cur_health_absent(client, monkeypatch):
    from api.routes import cur
    _set_cur_probe(monkeypatch, [], [], cur.ABSENT)
    r = client.get("/api/cur/health")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "absent"
    assert "unavailable" in r.json()["detail"].lower()


def test_cur_health_columns_missing(client, monkeypatch):
    from api.routes import cur
    _set_cur_probe(monkeypatch, [], [], cur.COLUMNS_MISSING)
    assert client.get(
        "/api/cur/health").json()["status"] == "columns_missing"


def test_cur_health_principal_blank(client, monkeypatch):
    # #749: column delivered (present in the manifest) but empty in
    # Glue → present-but-empty, "wait for delivery" copy.
    cols = ["line_item_iam_principal", "line_item_unblended_cost"]
    rows = [["", "1.50"], ["", "2.00"]]
    _set_cur_probe(monkeypatch, cols, rows, None)
    # 21-col manifest INCLUDING line_item_iam_principal.
    _set_manifest(monkeypatch, [
        "line_item_iam_principal", "line_item_unblended_cost",
        *[f"col_{i}" for i in range(19)],
    ])
    r = client.get("/api/cur/health")
    assert r.json()["status"] == "principal_blank"
    assert "attribution" in r.json()["detail"].lower()


def test_cur_health_principal_absent_from_export(client, monkeypatch):
    # #749: column NULL in Glue AND missing from the delivered
    # Parquet (manifest lacks it) → distinct re-create-the-export
    # state, not present-but-empty.
    cols = ["line_item_iam_principal", "line_item_unblended_cost"]
    rows = [["", "1.50"], ["", "2.00"]]
    _set_cur_probe(monkeypatch, cols, rows, None)
    # 20-col manifest WITHOUT line_item_iam_principal.
    _set_manifest(monkeypatch, [
        "line_item_unblended_cost",
        *[f"col_{i}" for i in range(19)],
    ])
    r = client.get("/api/cur/health")
    assert r.json()["status"] == "principal_absent_from_export"
    assert "re-created" in r.json()["detail"].lower()


def test_cur_health_blank_manifest_unreadable_falls_back(
        client, monkeypatch):
    # #749: manifest can't be read (None) → never a false
    # absent_from_export; fall back to present-but-empty.
    cols = ["line_item_iam_principal", "line_item_unblended_cost"]
    rows = [["", "1.50"]]
    _set_cur_probe(monkeypatch, cols, rows, None)
    _set_manifest(monkeypatch, None)
    r = client.get("/api/cur/health")
    assert r.json()["status"] == "principal_blank"


def test_cur_health_healthy(client, monkeypatch):
    cols = ["line_item_iam_principal", "line_item_unblended_cost"]
    rows = [["arn:aws:iam::1:role/r", "1.50"]]
    _set_cur_probe(monkeypatch, cols, rows, None)
    r = client.get("/api/cur/health")
    assert r.json()["status"] == "healthy"
    assert r.json()["detail"] is None


def test_cur_health_requires_org_admin(client, monkeypatch):
    from api.routes import cur
    _set_cur_probe(monkeypatch, [], [], cur.HEALTHY)
    import api.auth as auth_mod
    original = auth_mod._validate_request
    auth_mod._validate_request = (
        lambda req, db: ("nobody@test.com", "sigv4"))
    try:
        assert client.get("/api/cur/health").status_code == 403
    finally:
        auth_mod._validate_request = original


# ── #737: CUR data-through (spend freshness watermark) ───
def test_cur_data_through_shape(client):
    """Returns a data_through key — null when no CUR spend has
    landed, else an ISO timestamp. (Order-independent: the
    session DB may carry rows seeded by sibling tests.)"""
    r = client.get("/api/cur/data-through")
    assert r.status_code == 200, r.text
    dt = r.json()["data_through"]
    assert dt is None or isinstance(dt, str)


def test_cur_data_through_returns_latest_usage_hour(client):
    """Returns the freshest usage_hour across cur_user_spend."""
    from datetime import datetime, timezone, timedelta
    from db.session import get_db
    from db.models import CurUserSpend
    now = datetime.now(timezone.utc).replace(microsecond=0)
    older = now - timedelta(hours=5)
    with get_db() as db:
        db.add(CurUserSpend(
            email="a@t.com", usage_hour=older, region="us-east-1",
            model_id="m", spend_usd=1.0))
        db.add(CurUserSpend(
            email="b@t.com", usage_hour=now, region="us-east-1",
            model_id="m", spend_usd=2.0))
    r = client.get("/api/cur/data-through")
    assert r.status_code == 200, r.text
    # freshest of the two
    assert r.json()["data_through"].startswith(
        now.isoformat()[:13])


def test_cur_data_through_requires_org_admin(client):
    import api.auth as auth_mod
    original = auth_mod._validate_request
    auth_mod._validate_request = (
        lambda req, db: ("nobody@test.com", "sigv4"))
    try:
        assert client.get(
            "/api/cur/data-through").status_code == 403
    finally:
        auth_mod._validate_request = original
