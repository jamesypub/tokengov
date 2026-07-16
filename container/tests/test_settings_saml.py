"""
Runtime SAML / SSO login config externalized to Settings.

Covers the admin_config-backed config helpers (db.auth_config), the
GET/PUT/DELETE /settings/saml endpoints (label-only vs connection
apply), the /auth/providers + login precedence (DB over env), and the
env→DB first-boot seed. The Cognito side-effects (api.cognito_saml) are
mocked — no live AWS in CI; the real-artifact apply is the stage smoke
in the ticket's acceptance.
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient


# ── DB helper unit tests (no app) ────────────────────────────────────


def test_button_label_defaults_and_roundtrips(pg_url, clean_db):
    from db.session import get_db
    from db.auth_config import (
        get_sso_button_label, get_sso_button_label_raw,
        set_sso_button_label, DEFAULT_SSO_BUTTON_LABEL,
    )
    with get_db() as db:
        # Unset → default; raw is None (so callers can fall back to env).
        assert get_sso_button_label(db) == DEFAULT_SSO_BUTTON_LABEL
        assert get_sso_button_label_raw(db) is None
        set_sso_button_label(db, "Sign in with Acme")
        assert get_sso_button_label(db) == "Sign in with Acme"
        assert get_sso_button_label_raw(db) == "Sign in with Acme"
        # Blank resets to default (Login button can never go empty).
        set_sso_button_label(db, "   ")
        assert get_sso_button_label(db) == DEFAULT_SSO_BUTTON_LABEL


def test_set_saml_config_validation(pg_url, clean_db):
    from db.session import get_db
    from db.auth_config import set_saml_config, get_saml_config
    with get_db() as db:
        with pytest.raises(ValueError):
            set_saml_config(db, provider_name="",
                            metadata_url="https://x/m")
        with pytest.raises(ValueError):  # whitespace in name
            set_saml_config(db, provider_name="My Okta",
                            metadata_url="https://x/m")
        with pytest.raises(ValueError):  # neither url nor xml
            set_saml_config(db, provider_name="MyOkta")
        with pytest.raises(ValueError):  # both
            set_saml_config(db, provider_name="MyOkta",
                            metadata_url="https://x/m",
                            metadata_xml="<xml/>")
        with pytest.raises(ValueError):  # bad url scheme
            set_saml_config(db, provider_name="MyOkta",
                            metadata_url="ftp://x/m")
        # Happy path
        cfg = set_saml_config(
            db, provider_name="MyOkta",
            metadata_url="https://idp/metadata",
            email_attribute="emailAddress", idp_signout=True)
        assert cfg["configured"] is True
        assert cfg["provider_name"] == "MyOkta"
        assert cfg["metadata_url"] == "https://idp/metadata"
        assert cfg["email_attribute"] == "emailAddress"
        assert cfg["idp_signout"] is True
        # round-trip
        assert get_saml_config(db)["provider_name"] == "MyOkta"


def test_clear_saml_config(pg_url, clean_db):
    from db.session import get_db
    from db.auth_config import (
        set_saml_config, clear_saml_config, get_saml_config,
        saml_provider_name,
    )
    with get_db() as db:
        set_saml_config(db, provider_name="MyOkta",
                        metadata_url="https://idp/m")
        assert saml_provider_name(db) == "MyOkta"
        clear_saml_config(db)
        assert saml_provider_name(db) is None
        assert get_saml_config(db)["configured"] is False


def test_seed_saml_from_env_insert_if_absent(
    pg_url, clean_db, monkeypatch
):
    from db.session import get_db
    from db.auth_config import (
        seed_saml_config_from_env, get_saml_config,
        get_sso_button_label, set_saml_config,
    )
    monkeypatch.setenv("TG_OIDC_OKTA_PROVIDER_NAME", "EnvOkta")
    monkeypatch.setenv(
        "TG_OIDC_OKTA_METADATA_URL", "https://env/metadata")
    monkeypatch.setenv("TG_OIDC_OKTA_DISPLAY_NAME", "Env SSO")
    with get_db() as db:
        seed_saml_config_from_env(db)
        cfg = get_saml_config(db)
        assert cfg["provider_name"] == "EnvOkta"
        assert cfg["metadata_url"] == "https://env/metadata"
        assert get_sso_button_label(db) == "Env SSO"
    # Re-seed must NOT overwrite a later admin change (DB wins).
    with get_db() as db:
        set_saml_config(db, provider_name="RuntimeOkta",
                        metadata_url="https://runtime/m")
    with get_db() as db:
        seed_saml_config_from_env(db)
        assert get_saml_config(db)["provider_name"] == "RuntimeOkta"


# ── HTTP surface ─────────────────────────────────────────────────────


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


@pytest.fixture
def mock_cognito(monkeypatch):
    """Record Cognito apply/delete calls without touching AWS."""
    calls = {"apply": [], "delete": [], "status": 0}
    import api.cognito_saml as cs

    def _apply(**kw):
        calls["apply"].append(kw)

    def _delete(name):
        calls["delete"].append(name)

    def _status(name):
        calls["status"] += 1
        return {"present": True, "on_app_client": True, "error": None}

    def _reg(attr="email"):
        return {"sp_entity_id": "urn:amazon:cognito:sp:pool-123",
                "acs_url": "https://d.auth.us-east-1.amazoncognito.com"
                           "/saml2/idpresponse",
                "email_attribute": attr, "acs_url_error": None}

    monkeypatch.setattr(cs, "apply_saml_provider", _apply)
    monkeypatch.setattr(cs, "delete_saml_provider", _delete)
    monkeypatch.setattr(cs, "provider_live_status", _status)
    monkeypatch.setattr(cs, "registration_values", _reg)
    return calls


def test_get_saml_empty(admin_client, mock_cognito):
    r = admin_client.get("/api/settings/saml")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["sso_button_label"] == "Login with Your SSO"
    # No provider → no live status lookup attempted.
    assert mock_cognito["status"] == 0


def test_put_label_only_no_cognito_call(admin_client, mock_cognito):
    """A label-only PUT persists and triggers NO Cognito apply."""
    r = admin_client.put(
        "/api/settings/saml",
        json={"sso_button_label": "Log in with HPE SSO"})
    assert r.status_code == 200
    assert r.json()["sso_button_label"] == "Log in with HPE SSO"
    assert mock_cognito["apply"] == []
    # persists across a fresh GET
    assert admin_client.get("/api/settings/saml").json()[
        "sso_button_label"] == "Log in with HPE SSO"


def test_put_saml_applies_to_cognito(admin_client, mock_cognito):
    r = admin_client.put(
        "/api/settings/saml",
        json={"provider_name": "HpeIdc",
              "metadata_url": "https://idc/metadata",
              "email_attribute": "email",
              "sso_button_label": "Login with HPE"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["provider_name"] == "HpeIdc"
    assert body["sso_button_label"] == "Login with HPE"
    # Cognito apply was invoked with the persisted config.
    assert len(mock_cognito["apply"]) == 1
    assert mock_cognito["apply"][0]["provider_name"] == "HpeIdc"
    assert mock_cognito["apply"][0]["metadata_url"] == \
        "https://idc/metadata"


def test_put_saml_bad_input_400(admin_client, mock_cognito):
    """Validation error → 400, and Cognito is never called."""
    r = admin_client.put(
        "/api/settings/saml",
        json={"provider_name": "Bad Name",
              "metadata_url": "https://x/m"})
    assert r.status_code == 400
    assert mock_cognito["apply"] == []


def test_put_saml_cognito_error_surfaces_400(
    admin_client, monkeypatch
):
    """A Cognito apply failure surfaces as a 400 with the reason,
    not a 500 (bad metadata URL is operator-fixable)."""
    import api.cognito_saml as cs
    monkeypatch.setattr(cs, "provider_live_status",
                        lambda n: {"present": False,
                                   "on_app_client": False,
                                   "error": None})
    monkeypatch.setattr(cs, "registration_values",
                        lambda attr="email": {})

    def _boom(**kw):
        raise cs.CognitoApplyError("InvalidParameter: bad metadata")
    monkeypatch.setattr(cs, "apply_saml_provider", _boom)

    r = admin_client.put(
        "/api/settings/saml",
        json={"provider_name": "HpeIdc",
              "metadata_url": "https://idc/bad"})
    assert r.status_code == 400
    assert "bad metadata" in r.text


def test_delete_reverts_to_cognito_only(admin_client, mock_cognito):
    admin_client.put(
        "/api/settings/saml",
        json={"provider_name": "HpeIdc",
              "metadata_url": "https://idc/metadata"})
    r = admin_client.delete("/api/settings/saml")
    assert r.status_code == 200
    assert r.json()["configured"] is False
    assert mock_cognito["delete"] == ["HpeIdc"]


def test_saml_endpoints_require_org_admin(
    pg_url, clean_db, monkeypatch
):
    """A non-admin caller is rejected on the SAML endpoints."""
    import api.auth as auth_mod
    monkeypatch.setattr(
        auth_mod, "_validate_request",
        lambda req, db: ("member@test.com", "session"))
    from api.main import app
    with TestClient(app) as c:
        assert c.get("/api/settings/saml").status_code in (401, 403)
        assert c.put("/api/settings/saml",
                     json={"sso_button_label": "x"}).status_code \
            in (401, 403)
