"""
/api/models/catalog is dynamic — static CATALOG ∪ every model
observed in CUR (discovered_models), listed by DISTINCT model_id (us.*
and global.* are SEPARATE entries — owner: NO region/profile-agnostic
de-dupe). A model a principal starts using auto-appears as blockable,
no code change. Empty discovered_models → exactly the static catalog.
Plus the blocked-models GET now returns updated_at (the apply-status
pending/enforced signal).
"""
from __future__ import annotations
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
    with TestClient(app) as c:
        yield c


def _ids(resp):
    return [m["model_id"] for m in resp.json()["models"]]


# ── build_catalog (pure-ish, against the DB) ──────────────

def test_empty_discovered_is_exactly_static_catalog(clean_db):
    from db.session import get_db
    from api.routes.models import build_catalog, CATALOG
    with get_db() as db:
        out = build_catalog(db)
    assert [m["model_id"] for m in out] == [m["model_id"] for m in CATALOG]
    # static entries are flagged discovered=False
    assert all(m["discovered"] is False for m in out)


def test_discovered_model_not_in_catalog_appears(clean_db):
    from db.session import get_db
    from db.models import DiscoveredModel
    from api.routes.models import build_catalog
    new_id = "us.anthropic.claude-opus-4-9"
    with get_db() as db:
        db.add(DiscoveredModel(model_id=new_id))
    with get_db() as db:
        out = build_catalog(db)
    entry = next((m for m in out if m["model_id"] == new_id), None)
    assert entry is not None, "discovered model must appear in the catalog"
    assert entry["discovered"] is True
    # discovered-only entries are still blockable, with null pricing
    assert entry["input_per_1m"] is None
    assert entry["display_name"]  # a derived, non-empty label


def test_us_and_global_are_separate_entries_no_dedupe(clean_db):
    from db.session import get_db
    from db.models import DiscoveredModel
    from api.routes.models import build_catalog
    # us.* is already in CATALOG; global.* of the same model is NOT —
    # it must appear as its OWN distinct entry (no agnostic collapse).
    with get_db() as db:
        db.add(DiscoveredModel(
            model_id="global.anthropic.claude-opus-4-8"))
    with get_db() as db:
        ids = [m["model_id"] for m in build_catalog(db)]
    assert "us.anthropic.claude-opus-4-8" in ids
    assert "global.anthropic.claude-opus-4-8" in ids


def test_discovered_duplicate_of_catalog_id_not_doubled(clean_db):
    from db.session import get_db
    from db.models import DiscoveredModel
    from api.routes.models import build_catalog
    # A discovered row whose id IS already in CATALOG must not duplicate.
    with get_db() as db:
        db.add(DiscoveredModel(model_id="us.anthropic.claude-opus-4-8"))
    with get_db() as db:
        ids = [m["model_id"] for m in build_catalog(db)]
    assert ids.count("us.anthropic.claude-opus-4-8") == 1


# ── API ───────────────────────────────────────────────────

def test_api_catalog_unions_discovered(admin_client):
    from db.session import get_db
    from db.models import DiscoveredModel
    new_id = "us.anthropic.claude-opus-4-9"
    with get_db() as db:
        db.add(DiscoveredModel(model_id=new_id))
    r = admin_client.get("/api/models/catalog")
    assert r.status_code == 200, r.text
    assert new_id in _ids(r)


def test_blocked_models_get_returns_updated_at(admin_client):
    # Before any save → null; after a save → an ISO timestamp (the
    # apply-status pending/enforced signal, reload-durable).
    r = admin_client.get("/api/settings/blocked-models")
    assert r.status_code == 200
    assert r.json()["updated_at"] is None
    admin_client.put(
        "/api/settings/blocked-models",
        json={"blocked_models": ["us.anthropic.claude-opus-4-8"]})
    r = admin_client.get("/api/settings/blocked-models")
    assert r.json()["updated_at"], "updated_at set after a save"
