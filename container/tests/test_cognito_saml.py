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
        # provider name -> its stored ProviderDetails. Accepts a set of
        # names (empty details) or a {name: details} map so a test can
        # seed a STALE rsa-sha256 provider.
        if isinstance(providers_present, dict):
            self._providers = {k: dict(v) for k, v in
                               providers_present.items()}
        else:
            self._providers = {n: {} for n in (providers_present or [])}
        self.update_calls = []
        self.idp_calls = []  # create/update_identity_provider kwargs
        self.deleted = []

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

    @staticmethod
    def _reject_bad_signing(details):
        # Mirror real Cognito: RequestSigningAlgorithm is an enum whose
        # ONLY member is rsa-sha256 — "" (or any other value) 400s with
        # InvalidParameterException. This is what the mock previously
        # accepted, hiding the #1438 bug.
        rsa = (details or {}).get("RequestSigningAlgorithm")
        if rsa is not None and rsa != "rsa-sha256":
            err = Exception(
                f"InvalidParameterException: Value '{rsa}' at "
                "'providerConfiguration.samlConfig."
                "requestSigningAlgorithm' failed to satisfy constraint: "
                "Member must satisfy enum value set: [rsa-sha256]")
            err.response = {
                "Error": {"Code": "InvalidParameterException"}}
            raise err

    def describe_identity_provider(self, UserPoolId, ProviderName):
        if ProviderName in self._providers:
            return {"IdentityProvider": {
                "ProviderName": ProviderName,
                "ProviderDetails": dict(self._providers[ProviderName])}}
        err = Exception("not found")
        err.response = {"Error": {"Code": "ResourceNotFoundException"}}
        raise err

    def create_identity_provider(self, **kwargs):
        self._reject_bad_signing(kwargs.get("ProviderDetails"))
        self.idp_calls.append(kwargs)
        self._providers[kwargs["ProviderName"]] = dict(
            kwargs.get("ProviderDetails") or {})

    def update_identity_provider(self, **kwargs):
        self._reject_bad_signing(kwargs.get("ProviderDetails"))
        self.idp_calls.append(kwargs)
        # Mirror the MERGE: sent keys overlay the stored details.
        cur = self._providers.setdefault(kwargs["ProviderName"], {})
        cur.update(kwargs.get("ProviderDetails") or {})

    def delete_identity_provider(self, UserPoolId, ProviderName):
        self.deleted.append(ProviderName)
        self._providers.pop(ProviderName, None)


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


def test_apply_signout_still_disables_request_signing(fake):
    # Even with single-logout requested, RequestSigningAlgorithm must NOT
    # be rsa-sha256: it is a pool-provider-level flag with no per-message
    # toggle, so signing the SLO LogoutRequest also signs the SP-initiated
    # login AuthnRequest, which AWS IDC rejects → "Federate 403" and login
    # never completes. Working login beats clean logout: signing stays off
    # (the key is OMITTED — "off" = absent; real Cognito 400s on "" since
    # the enum's only member is rsa-sha256, #1438), IDPSignout still
    # tracks the request. SLO degrades as the documented tradeoff.
    cs.apply_saml_provider(
        provider_name="HpeIdc",
        email_attribute="email",
        metadata_url="https://idp/metadata",
        idp_signout=True)
    assert len(fake.idp_calls) == 1
    details = fake.idp_calls[0]["ProviderDetails"]
    assert details["IDPSignout"] == "true"
    # Absent, NOT "" (real Cognito rejects "" — #1438).
    assert "RequestSigningAlgorithm" not in details


def test_apply_omits_request_signing_on_plain_update(fake):
    # An existing UNSIGNED provider (no stale rsa-sha256): a Save is a
    # plain update that simply omits RequestSigningAlgorithm — no ""
    # (which real Cognito 400s), no delete/recreate.
    fake._providers = {"HpeIdc": {"IDPSignout": "false"}}
    cs.apply_saml_provider(
        provider_name="HpeIdc",
        email_attribute="email",
        metadata_url="https://idp/metadata",
        idp_signout=False)
    assert fake.deleted == []           # no recreate needed
    assert len(fake.idp_calls) == 1
    assert "ProviderName" in fake.idp_calls[0]  # update_identity_provider
    details = fake.idp_calls[0]["ProviderDetails"]
    assert details["IDPSignout"] == "false"
    assert "RequestSigningAlgorithm" not in details


def test_apply_clears_stale_request_signing_via_recreate(fake):
    # A provider left SIGNED by the old buggy apply (rsa-sha256): the
    # merge won't clear it by omission and Cognito rejects "", so the fix
    # DELETE+RECREATEs (a create starts from empty details → unsigned),
    # unbreaking IDC login. Assert the provider is recreated with no
    # RequestSigningAlgorithm.
    fake._providers = {
        "HpeIdc": {"IDPSignout": "false",
                   "RequestSigningAlgorithm": "rsa-sha256"}}
    cs.apply_saml_provider(
        provider_name="HpeIdc",
        email_attribute="email",
        metadata_url="https://idp/metadata",
        idp_signout=False)
    # The stale provider was deleted then recreated.
    assert fake.deleted == ["HpeIdc"]
    assert len(fake.idp_calls) == 1
    assert fake.idp_calls[0].get("ProviderType") == "SAML"  # create
    details = fake.idp_calls[0]["ProviderDetails"]
    assert details["IDPSignout"] == "false"
    assert "RequestSigningAlgorithm" not in details
    # The live provider ends unsigned (the recreate dropped the stale value).
    assert "RequestSigningAlgorithm" not in fake._providers["HpeIdc"]


def test_delete_preserves_oauth_config(fake):
    # Start with the IdP already on the client.
    fake._client = dict(BASE_CLIENT,
                        SupportedIdentityProviders=["COGNITO", "HpeIdc"])
    fake._providers = {"HpeIdc": {}}
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
    fake._providers = {"HpeIdc": {}}
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
    fake._providers = {"HpeIdc": {}}
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
    fake._providers = {"HpeIdc": {}}
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
