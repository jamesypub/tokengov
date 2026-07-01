"""
Cognito SAML apply/delete — the read-modify-write guard.

UpdateUserPoolClient is a full replace, not a merge: any field not
re-sent is wiped. A SAML apply that set only SupportedIdentityProviders
nulled CallbackURLs / AllowedOAuthFlows / AllowedOAuthScopes / LogoutURLs
and flipped AllowedOAuthFlowsUserPoolClient to false → Cognito had no
registered callback → login failed with redirect_mismatch. These tests
drive the real apply_saml_provider / delete_saml_provider against a fake
cognito-idp client and assert the existing OAuth config is re-sent
intact, with only the IdP list changing. (The unit test the original
build had only checked "IdP present" — it could not catch the wipe; this
asserts the preserved fields, the actual contract.)
"""
from __future__ import annotations
import pytest

import api.cognito_saml as cs


# The OAuth config a real deployed app client carries — exactly what the
# SAML apply must preserve.
BASE_CLIENT = {
    "ClientName": "tg-app",
    "CallbackURLs": ["https://tg.example.com/auth/callback"],
    "LogoutURLs": ["https://tg.example.com/auth/logout"],
    "AllowedOAuthFlows": ["code"],
    "AllowedOAuthScopes": ["openid", "email", "profile"],
    "AllowedOAuthFlowsUserPoolClient": True,
    "ExplicitAuthFlows": ["ALLOW_REFRESH_TOKEN_AUTH"],
    "SupportedIdentityProviders": ["COGNITO"],
    "PreventUserExistenceErrors": "ENABLED",
    "EnableTokenRevocation": True,
}


class FakeCognito:
    """Minimal cognito-idp stub recording update_user_pool_client kwargs."""

    def __init__(self, client=None, providers_present=None):
        self._client = dict(client or BASE_CLIENT)
        self._providers = set(providers_present or [])
        self.update_calls = []
        self.idp_calls = []  # create/update_identity_provider kwargs

    def describe_user_pool_client(self, UserPoolId, ClientId):
        return {"UserPoolClient": dict(self._client)}

    def update_user_pool_client(self, **kwargs):
        self.update_calls.append(kwargs)
        # Mirror Cognito's full-replace: the stored client becomes
        # exactly what was sent (so a dropped field would null out).
        self._client = {
            k: v for k, v in kwargs.items()
            if k not in ("UserPoolId", "ClientId")}
        return {"UserPoolClient": dict(self._client)}

    def describe_identity_provider(self, UserPoolId, ProviderName):
        if ProviderName in self._providers:
            return {"IdentityProvider": {"ProviderName": ProviderName}}
        err = Exception("not found")
        err.response = {"Error": {"Code": "ResourceNotFoundException"}}
        raise err

    def create_identity_provider(self, **kwargs):
        self.idp_calls.append(kwargs)
        self._providers.add(kwargs["ProviderName"])

    def update_identity_provider(self, **kwargs):
        self.idp_calls.append(kwargs)
        self._providers.add(kwargs["ProviderName"])

    def delete_identity_provider(self, UserPoolId, ProviderName):
        self._providers.discard(ProviderName)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("TG_COGNITO_USER_POOL_ID", "us-east-1_pool")
    monkeypatch.setenv("TG_OIDC_CLIENT_ID", "clientid123")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    fc = FakeCognito()
    monkeypatch.setattr(cs, "_client", lambda: fc)
    return fc


def _assert_oauth_preserved(call):
    """Every OAuth field from BASE_CLIENT is re-sent unchanged."""
    assert call["CallbackURLs"] == BASE_CLIENT["CallbackURLs"]
    assert call["LogoutURLs"] == BASE_CLIENT["LogoutURLs"]
    assert call["AllowedOAuthFlows"] == BASE_CLIENT["AllowedOAuthFlows"]
    assert call["AllowedOAuthScopes"] == BASE_CLIENT["AllowedOAuthScopes"]
    assert call["AllowedOAuthFlowsUserPoolClient"] is True
    assert call["ExplicitAuthFlows"] == BASE_CLIENT["ExplicitAuthFlows"]
    assert call["PreventUserExistenceErrors"] == "ENABLED"
    assert call["EnableTokenRevocation"] is True


def test_apply_preserves_oauth_config(fake):
    cs.apply_saml_provider(
        provider_name="HpeIdc",
        email_attribute="email",
        metadata_url="https://idp/metadata")
    assert len(fake.update_calls) == 1
    call = fake.update_calls[0]
    # The IdP was added (COGNITO kept) …
    assert set(call["SupportedIdentityProviders"]) == {"COGNITO", "HpeIdc"}
    # … and NOTHING else was wiped.
    _assert_oauth_preserved(call)


def test_apply_signout_sets_request_signing_algorithm(fake):
    # Single-logout needs Cognito to SIGN the SAML LogoutRequest it sends
    # to the IdP; without RequestSigningAlgorithm the IdP (AWS IDC) won't
    # run its SLO-response leg and the browser is stranded on the IdP
    # screen instead of returning to the app /login. AWS documents
    # rsa-sha256 for an SLO-capable SAML IdP.
    cs.apply_saml_provider(
        provider_name="HpeIdc",
        email_attribute="email",
        metadata_url="https://idp/metadata",
        idp_signout=True)
    assert len(fake.idp_calls) == 1
    details = fake.idp_calls[0]["ProviderDetails"]
    assert details["IDPSignout"] == "true"
    assert details["RequestSigningAlgorithm"] == "rsa-sha256"


def test_apply_no_signout_omits_request_signing_algorithm(fake):
    # With single-logout off there's no signed LogoutRequest, so the
    # signing algorithm must not be sent — keep the provider details
    # minimal (Cognito rejects RequestSigningAlgorithm without SLO intent).
    cs.apply_saml_provider(
        provider_name="HpeIdc",
        email_attribute="email",
        metadata_url="https://idp/metadata",
        idp_signout=False)
    assert len(fake.idp_calls) == 1
    details = fake.idp_calls[0]["ProviderDetails"]
    assert details["IDPSignout"] == "false"
    assert "RequestSigningAlgorithm" not in details


def test_delete_preserves_oauth_config(fake):
    # Start with the IdP already on the client.
    fake._client = dict(BASE_CLIENT,
                        SupportedIdentityProviders=["COGNITO", "HpeIdc"])
    fake._providers = {"HpeIdc"}
    cs.delete_saml_provider("HpeIdc")
    assert len(fake.update_calls) == 1
    call = fake.update_calls[0]
    # IdP removed, COGNITO password path kept …
    assert call["SupportedIdentityProviders"] == ["COGNITO"]
    # … OAuth config intact (revert can't wipe login either).
    _assert_oauth_preserved(call)


def test_apply_idempotent_no_update_when_present(fake):
    # IdP + COGNITO already supported → no update call at all (so it
    # can't even accidentally re-replace).
    fake._client = dict(BASE_CLIENT,
                        SupportedIdentityProviders=["COGNITO", "HpeIdc"])
    fake._providers = {"HpeIdc"}
    cs.apply_saml_provider(
        provider_name="HpeIdc",
        email_attribute="email",
        metadata_url="https://idp/metadata")
    assert fake.update_calls == []


def test_apply_adds_login_to_logout_urls(fake, monkeypatch):
    # idp_signout returns the user to the app's /login via a Cognito
    # hosted-logout URL, but Cognito only honors a logout_uri that's in
    # the client's LogoutURLs allowlist. The apply must register the
    # derived /login (origin of TG_OIDC_REDIRECT_URI) — preserving the
    # existing LogoutURLs entries.
    monkeypatch.setenv(
        "TG_OIDC_REDIRECT_URI",
        "https://tg.example.com/auth/callback")
    cs.apply_saml_provider(
        provider_name="HpeIdc",
        email_attribute="email",
        metadata_url="https://idp/metadata")
    call = fake.update_calls[0]
    # Existing entry preserved AND /login added (union, idempotent).
    assert "https://tg.example.com/auth/logout" in call["LogoutURLs"]
    assert "https://tg.example.com/login" in call["LogoutURLs"]


def test_apply_login_logout_url_idempotent(fake, monkeypatch):
    # /login already present → not duplicated, and (with the IdP +
    # COGNITO also already present) no update call at all.
    monkeypatch.setenv(
        "TG_OIDC_REDIRECT_URI",
        "https://tg.example.com/auth/callback")
    fake._client = dict(
        BASE_CLIENT,
        SupportedIdentityProviders=["COGNITO", "HpeIdc"],
        LogoutURLs=["https://tg.example.com/auth/logout",
                    "https://tg.example.com/login"])
    fake._providers = {"HpeIdc"}
    cs.apply_saml_provider(
        provider_name="HpeIdc",
        email_attribute="email",
        metadata_url="https://idp/metadata")
    # Nothing left to change → no replace at all (no dup either).
    assert fake.update_calls == []


def test_apply_adds_login_when_idps_already_present(fake, monkeypatch):
    # The IdP + COGNITO are already supported, but /login is NOT yet a
    # LogoutURL: the apply must STILL fire an update to register it
    # (the LogoutURL union is its own change trigger, independent of
    # the SupportedIdentityProviders delta).
    monkeypatch.setenv(
        "TG_OIDC_REDIRECT_URI",
        "https://tg.example.com/auth/callback")
    fake._client = dict(
        BASE_CLIENT,
        SupportedIdentityProviders=["COGNITO", "HpeIdc"])
    fake._providers = {"HpeIdc"}
    cs.apply_saml_provider(
        provider_name="HpeIdc",
        email_attribute="email",
        metadata_url="https://idp/metadata")
    assert len(fake.update_calls) == 1
    call = fake.update_calls[0]
    assert "https://tg.example.com/login" in call["LogoutURLs"]
    # IdP set unchanged (it was already complete).
    assert set(call["SupportedIdentityProviders"]) == {"COGNITO", "HpeIdc"}


def test_login_return_uri_derivation(monkeypatch):
    # Origin of the redirect_uri + /login; None when unconfigured or
    # malformed (no scheme/host).
    monkeypatch.setenv(
        "TG_OIDC_REDIRECT_URI",
        "https://tg.example.com/auth/callback")
    assert cs.login_return_uri() == "https://tg.example.com/login"
    monkeypatch.delenv("TG_OIDC_REDIRECT_URI", raising=False)
    assert cs.login_return_uri() is None
    monkeypatch.setenv("TG_OIDC_REDIRECT_URI", "not-a-url")
    assert cs.login_return_uri() is None


def test_update_skips_none_fields(monkeypatch):
    # A field absent/None in the read-back is simply not sent (no
    # explicit null), so we never send None for an unset optional.
    fc = FakeCognito(client={
        "CallbackURLs": ["https://x/cb"],
        "AllowedOAuthFlowsUserPoolClient": True,
        "SupportedIdentityProviders": ["COGNITO"],
        "AccessTokenValidity": None,   # unset optional
    })
    cs._update_supported_idps(
        fc, "pool", "client", fc._client, ["COGNITO", "New"])
    call = fc.update_calls[0]
    assert "AccessTokenValidity" not in call
    assert call["CallbackURLs"] == ["https://x/cb"]
    assert call["SupportedIdentityProviders"] == ["COGNITO", "New"]
