"""
#746: the org-wide blocked-model list — the model_ids the
reconciler's model DENYLIST Deny blocks. Covers the db/org_config
helpers and the GET/PUT API (/api/settings/blocked-models,
org-admin gated). Reverses #626's allow-list (owner posture
2026-06-07): empty list = allow every model.
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient


_MODEL_A = "us.anthropic.claude-sonnet-4-6"
_MODEL_B = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# ── db/org_config helpers ─────────────────────────────────

def test_get_blocked_models_empty_by_default(clean_db):
    from db.session import get_db
    from db.org_config import get_blocked_models
    with get_db() as db:
        assert get_blocked_models(db) == []


def test_set_then_get_roundtrips(clean_db):
    from db.session import get_db
    from db.org_config import (
        get_blocked_models, set_blocked_models,
    )
    with get_db() as db:
        out = set_blocked_models(db, [_MODEL_A, _MODEL_B])
        assert out == [_MODEL_A, _MODEL_B]
    with get_db() as db:
        assert get_blocked_models(db) == [_MODEL_A, _MODEL_B]


def test_set_dedupes_and_preserves_order(clean_db):
    from db.session import get_db
    from db.org_config import (
        get_blocked_models, set_blocked_models,
    )
    with get_db() as db:
        out = set_blocked_models(
            db, [_MODEL_B, _MODEL_A, _MODEL_B])
        assert out == [_MODEL_B, _MODEL_A]
    with get_db() as db:
        assert get_blocked_models(db) == [_MODEL_B, _MODEL_A]


def test_set_empty_clears(clean_db):
    from db.session import get_db
    from db.org_config import (
        get_blocked_models, set_blocked_models,
    )
    with get_db() as db:
        set_blocked_models(db, [_MODEL_A])
    with get_db() as db:
        set_blocked_models(db, [])
    with get_db() as db:
        assert get_blocked_models(db) == []


def test_set_rejects_non_string_entry(clean_db):
    from db.session import get_db
    from db.org_config import set_blocked_models
    with get_db() as db:
        with pytest.raises(ValueError):
            set_blocked_models(db, [_MODEL_A, 123])


def test_get_tolerates_malformed_stored_value(clean_db):
    """A malformed stored value must not crash the reconciler —
    get() returns [] (treated as 'no block-list' → allow all)."""
    from db.session import get_db
    from db.models import AdminConfig
    from db.org_config import (
        get_blocked_models, BLOCKED_MODELS_KEY,
    )
    with get_db() as db:
        db.add(AdminConfig(
            key=BLOCKED_MODELS_KEY, value="not json{"))
    with get_db() as db:
        assert get_blocked_models(db) == []


def test_stale_approved_models_key_is_ignored(clean_db):
    """#746: the rename uses a NEW kv key — a leftover
    approved_models value from before the posture reversal must
    NOT be read as a block-list (that would invert its meaning)."""
    from db.session import get_db
    from db.models import AdminConfig
    from db.org_config import get_blocked_models
    with get_db() as db:
        db.add(AdminConfig(
            key="approved_models", value='["us.anthropic.x"]'))
    with get_db() as db:
        assert get_blocked_models(db) == []


# ── API: GET/PUT /api/settings/blocked-models ─────────────

@pytest.fixture
def admin_client(pg_url, clean_db, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("admin@test.com", "session"))
    from db.session import get_db
    from db.models import AdminRole
    with get_db() as db:
        db.add(AdminRole(
            email="admin@test.com", role="org_admin"))
    from api.main import app
    with TestClient(app) as c:
        yield c


def test_api_get_defaults_empty(admin_client):
    r = admin_client.get("/api/settings/blocked-models")
    assert r.status_code == 200
    assert r.json()["blocked_models"] == []


def test_api_put_roundtrips(admin_client):
    r = admin_client.put(
        "/api/settings/blocked-models",
        json={"blocked_models": [_MODEL_A, _MODEL_B]})
    assert r.status_code == 200, r.text
    assert r.json()["blocked_models"] == [_MODEL_A, _MODEL_B]
    got = admin_client.get(
        "/api/settings/blocked-models").json()
    assert got["blocked_models"] == [_MODEL_A, _MODEL_B]


def test_api_put_empty_clears(admin_client):
    admin_client.put(
        "/api/settings/blocked-models",
        json={"blocked_models": [_MODEL_A]})
    r = admin_client.put(
        "/api/settings/blocked-models",
        json={"blocked_models": []})
    assert r.json()["blocked_models"] == []


def test_api_put_rejects_missing_field(admin_client):
    r = admin_client.put(
        "/api/settings/blocked-models", json={})
    assert r.status_code == 400


def test_api_put_rejects_non_string_entry(admin_client):
    r = admin_client.put(
        "/api/settings/blocked-models",
        json={"blocked_models": [_MODEL_A, 5]})
    assert r.status_code == 400


def test_admin_config_surfaces_blocked_models(admin_client):
    """#746: GET /api/admin/config surfaces the list read-only
    alongside the other org config."""
    admin_client.put(
        "/api/settings/blocked-models",
        json={"blocked_models": [_MODEL_A]})
    cfg = admin_client.get("/api/admin/config").json()
    assert cfg["blocked_models"] == [_MODEL_A]
