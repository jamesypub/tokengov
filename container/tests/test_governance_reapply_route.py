"""Route tests for the governance-drift banner backend:
- GET /api/governance/drift now carries role_type per row (so the UI
  can branch the IDC vs non-IDC remedy).
- POST /api/governance/reapply/{identity_key} re-runs the SHARED
  reconcile_principal for one drifted principal, returns the honest
  apply state, org-admin scoped, 404 on unknown.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

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


def _seed_drift(role_arn, direction="governed_no_deny",
                identity_key="dev@corp.com"):
    from db.session import get_db
    from db.models import GovernanceDrift
    with get_db() as db:
        db.add(GovernanceDrift(
            identity_key=identity_key, email=identity_key,
            role_arn=role_arn, direction=direction,
            expected="managed", actual="drift", detail="deny missing",
            sweep_at=datetime.now(timezone.utc)))


def _seed_user(identity_key="dev@corp.com", role_type="iam",
               arn="arn:aws:iam::123456789012:role/AppRole"):
    from db.session import get_db
    from db.models import User
    with get_db() as db:
        db.add(User(
            email=identity_key, identity_key=identity_key,
            status="active", principal_type="assumed_role",
            role_type=role_type, principal_arn=arn, governed=True))


def test_drift_row_carries_role_type_iam(admin_client):
    _seed_drift("arn:aws:iam::123456789012:role/AppRole")
    r = admin_client.get("/api/governance/drift")
    assert r.status_code == 200
    assert r.json()["drift"][0]["role_type"] == "iam"


def test_drift_row_role_type_idc_from_reserved_sso_arn(admin_client):
    _seed_drift("arn:aws:iam::1:role/aws-reserved/sso.amazonaws.com/"
                "AWSReservedSSO_Dev_abc")
    r = admin_client.get("/api/governance/drift")
    assert r.json()["drift"][0]["role_type"] == "idc"


def test_reapply_calls_shared_reconcile_and_returns_state(admin_client):
    _seed_user()
    with patch("worker.jobs.deny_reconciler.reconcile_principal",
               return_value={"state": "enforced", "enforced": True,
                             "denied": True}) as rp:
        r = admin_client.post("/api/governance/reapply/dev@corp.com")
    assert r.status_code == 200, r.text
    rp.assert_called_once()                  # the SHARED writer
    body = r.json()
    assert body["identity_key"] == "dev@corp.com"
    assert body["apply"]["enforced"] is True


def test_reapply_404_on_unknown_principal(admin_client):
    r = admin_client.post("/api/governance/reapply/nobody@corp.com")
    assert r.status_code == 404


def test_reapply_never_500s_on_reconcile_error(admin_client):
    _seed_user()
    with patch("worker.jobs.deny_reconciler.reconcile_principal",
               side_effect=RuntimeError("throttled")):
        r = admin_client.post("/api/governance/reapply/dev@corp.com")
    assert r.status_code == 200, r.text
    assert r.json()["apply"]["state"] == "failed"
    assert r.json()["apply"]["enforced"] is False


def test_reapply_org_admin_scoped(pg_url, clean_db, monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("member@test.com", "session"))
    _seed_user()
    from api.main import app
    with TestClient(app) as c:
        r = c.post("/api/governance/reapply/dev@corp.com")
    assert r.status_code == 403


# ── clean sweep clears the banner (clean-slate model) ─────────────────
# The exact gap that shipped: a clean sweep left prior drift rows in
# place, so the banner never cleared no matter how many clean re-runs.
# Clean-slate: every sweep deletes all governance_drift rows then
# inserts only its findings, so a clean run empties the table → count 0.

def _run_clean_sweep(monkeypatch):
    """Run the REAL governance_drift_check with no drifted principals
    (empty user set → zero findings). It deletes all prior drift rows,
    inserts nothing, and stamps last_drift_sweep_at."""
    import worker.jobs.governance_drift_check as job
    from unittest.mock import MagicMock
    monkeypatch.setattr(job.boto3, "client", lambda *a, **k: MagicMock())
    return job.run()


def test_clean_sweep_clears_drift_count(admin_client, monkeypatch):
    # A prior dirty sweep's rows are on record …
    _seed_drift("arn:aws:iam::123456789012:role/AppRole")
    assert admin_client.get(
        "/api/governance/drift-count").json()["count"] == 1

    # … then a clean sweep runs and wipes them (delete-all + insert 0).
    assert _run_clean_sweep(monkeypatch)["drift"] == 0

    # Banner clears — WITHOUT any manual row deletion (the regression).
    r = admin_client.get("/api/governance/drift-count")
    assert r.json()["count"] == 0, r.text
    # And it still shows a fresh "last checked" from the marker.
    assert r.json()["sweep_at"] is not None


def test_clean_sweep_empties_drift_list(admin_client, monkeypatch):
    _seed_drift("arn:aws:iam::123456789012:role/AppRole")
    assert len(admin_client.get(
        "/api/governance/drift").json()["drift"]) == 1
    _run_clean_sweep(monkeypatch)
    body = admin_client.get("/api/governance/drift").json()
    assert body["drift"] == []
    assert body["sweep_at"] is not None   # last-checked still present


def test_last_checked_from_marker_when_table_empty(admin_client, monkeypatch):
    # No drift rows at all, but a completed clean sweep must still
    # surface a "last checked" time (from last_drift_sweep_at, since an
    # empty table carries none).
    _run_clean_sweep(monkeypatch)
    r = admin_client.get("/api/governance/drift-count")
    assert r.json()["count"] == 0
    assert r.json()["sweep_at"] is not None
