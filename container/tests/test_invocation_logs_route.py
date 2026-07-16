"""GET/PUT /api/settings/invocation-logs (slice 2): the region catalog
round-trip + the Bedrock-API apply wiring (enable/disable), org_admin
gated. Patches the bedrock client + account id — no live AWS; uses the
shared conftest Postgres testcontainer.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def admin_client(pg_url, clean_db, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"))
    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        db.add(AdminRole(email="admin@test.com", role="org_admin"))
    from api.main import app
    # Deterministic account id + a bedrock client reporting "no config"
    # (so enable applies cleanly).
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    import api.routes.settings as s
    br = MagicMock()
    br.get_model_invocation_logging_configuration.return_value = {}
    monkeypatch.setattr(s, "_bedrock_client", lambda region: br)
    with TestClient(app) as c:
        c._bedrock = br
        yield c


def test_get_defaults_empty(admin_client):
    r = admin_client.get("/api/settings/invocation-logs")
    assert r.status_code == 200
    body = r.json()
    assert body["regions"] == []
    assert body["updated_at"] is None


def test_put_roundtrips_with_derived_bucket_and_applies(admin_client):
    r = admin_client.put(
        "/api/settings/invocation-logs",
        json={"regions": [
            {"region": "us-east-1", "enabled": True, "text_on": True}]})
    assert r.status_code == 200, r.text
    body = r.json()
    # bucket is DERIVED server-side, never trusted from the client
    assert body["regions"][0]["bucket"] == \
        "tg-bedrock-invlogs-us-east-1-123456789012"
    # full S3 path is server-built (single source of truth for the UI)
    assert body["regions"][0]["s3_uri"] == \
        "s3://tg-bedrock-invlogs-us-east-1-123456789012"
    # applied via the Bedrock API — enabled (no prior config)
    assert body["apply"] == [{"region": "us-east-1",
                              "outcome": "enabled"}]
    admin_client._bedrock.\
        put_model_invocation_logging_configuration.assert_called_once()
    # persisted + readable — the GET carries the same per-region path
    got = admin_client.get("/api/settings/invocation-logs").json()
    assert [e["region"] for e in got["regions"]] == ["us-east-1"]
    assert got["regions"][0]["s3_uri"] == \
        "s3://tg-bedrock-invlogs-us-east-1-123456789012"
    assert got["updated_at"] is not None


def test_put_rejects_invalid_region(admin_client):
    r = admin_client.put(
        "/api/settings/invocation-logs",
        json={"regions": [{"region": "not-a-region"}]})
    assert r.status_code == 400


def test_put_missing_regions_field_400(admin_client):
    r = admin_client.put("/api/settings/invocation-logs", json={})
    assert r.status_code == 400


def test_enable_does_not_clobber_existing_foreign_config(admin_client):
    # Bedrock already has a config pointing elsewhere → left intact.
    admin_client._bedrock.\
        get_model_invocation_logging_configuration.return_value = {
            "loggingConfig": {"s3Config": {"bucketName": "customer-own"}}}
    r = admin_client.put(
        "/api/settings/invocation-logs",
        json={"regions": [{"region": "us-east-1", "enabled": True}]})
    assert r.status_code == 200, r.text
    assert r.json()["apply"][0]["outcome"] == "already_enabled"
    admin_client._bedrock.\
        put_model_invocation_logging_configuration.assert_not_called()


def test_non_admin_forbidden(pg_url, clean_db, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("member@test.com", "session"))
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/settings/invocation-logs")
    assert r.status_code == 403
