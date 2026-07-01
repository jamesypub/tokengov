"""
Apply runtime SAML config to the live Cognito user pool.

This is the side-effecting half of the externalized-SSO feature: it
turns the config stored in ``admin_config`` (see
``db.auth_config``) into a Cognito ``UserPoolIdentityProvider`` and
wires it onto the app client — all against an **existing** pool, no
redeploy. Cognito SAML IdPs are fully manageable at runtime via the
``cognito-idp`` API:

  - ``create/update/delete-identity-provider --provider-type SAML``
    with ``ProviderDetails`` (``MetadataURL`` *or* ``MetadataFile``,
    ``IDPSignout``) and ``--attribute-mapping email=<saml-attr>``.
  - the IdP is added to the app client's
    ``SupportedIdentityProviders``.
  - a metadata **URL** auto-refreshes (~6h) — preferred over uploaded
    XML.

The trust shape: the SAML SP is **Cognito**, not tg — tg is an OIDC
client of the pool behind it. So the values to register on the IdP /
IAM Identity Center side are *Cognito's*: the ACS URL
(``https://<domain>.auth.<region>.amazoncognito.com/saml2/idpresponse``)
and the SP entity id (``urn:amazon:cognito:sp:<pool-id>``). Cognito does
not publish an SP-metadata document, so the admin enters those two
values manually (we surface them via :func:`registration_values`).

Pool id comes from ``TG_COGNITO_USER_POOL_ID`` and the app client id
from ``TG_OIDC_CLIENT_ID`` (both tg-cognito-pool / container-stack
outputs). The app task role's ``cognito-idp:*IdentityProvider`` /
``*UserPoolClient`` grant is scoped to this pool's ARN (in CFN).

All boto3 errors are surfaced as :class:`CognitoApplyError` so the
route returns a clean 4xx/5xx with the Cognito reason (e.g. a bad
metadata URL), never an uncaught 500.
"""
from __future__ import annotations

import os
from typing import Optional


class CognitoApplyError(Exception):
    """A Cognito API call failed (or the pool isn't configured).
    Carries a human-readable reason for the HTTP layer to surface."""


def _pool_id() -> str:
    pool_id = os.environ.get("TG_COGNITO_USER_POOL_ID", "").strip()
    if not pool_id:
        raise CognitoApplyError(
            "Cognito user pool not configured "
            "(TG_COGNITO_USER_POOL_ID is unset) — SAML login "
            "requires a Cognito pool deployment")
    return pool_id


def _app_client_id() -> str:
    cid = os.environ.get("TG_OIDC_CLIENT_ID", "").strip()
    if not cid:
        raise CognitoApplyError(
            "Cognito app client not configured "
            "(TG_OIDC_CLIENT_ID is unset)")
    return cid


def _region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


def login_return_uri() -> Optional[str]:
    """The app's ``/login`` URL, derived from the OIDC redirect_uri's
    origin (``…/auth/callback`` → ``…/login``). This is both the
    post-logout landing the SPA navigates to AND the value that must
    be a registered ``LogoutURL`` on the app client, or Cognito
    rejects the requested return and drops the user on its own hosted
    page. Single source of truth so the apply-time registration and
    the logout handler agree on the exact string. ``None`` when the
    redirect_uri isn't configured."""
    from urllib.parse import urlsplit, urlunsplit
    redirect = os.environ.get("TG_OIDC_REDIRECT_URI", "").strip()
    if not redirect:
        return None
    parts = urlsplit(redirect)
    if not (parts.scheme and parts.netloc):
        return None
    return urlunsplit(
        (parts.scheme, parts.netloc, "/login", "", ""))


def _client():
    import boto3
    return boto3.client("cognito-idp", region_name=_region())


def _provider_details(
    *,
    metadata_url: Optional[str],
    metadata_xml: Optional[str],
    idp_signout: bool,
) -> dict:
    details: dict = {"IDPSignout": "true" if idp_signout else "false"}
    if idp_signout:
        # Single-logout requires Cognito to sign the SAML LogoutRequest it
        # sends to the IdP with the user-pool signing certificate. Without
        # RequestSigningAlgorithm set, the LogoutRequest is unsigned, the
        # IdP (AWS IDC) won't run its SLO-response leg (POST a
        # LogoutResponse back to <domain>/saml2/logout), and the browser is
        # stranded on the IdP sign-in screen instead of being redirected to
        # the logout_uri (the app /login). AWS documents rsa-sha256 as the
        # ProviderDetails value for an SLO-capable SAML IdP.
        details["RequestSigningAlgorithm"] = "rsa-sha256"
    if metadata_url:
        details["MetadataURL"] = metadata_url
    elif metadata_xml:
        details["MetadataFile"] = metadata_xml
    else:  # pragma: no cover — guarded by set_saml_config
        raise CognitoApplyError(
            "one of metadata_url / metadata_xml is required")
    return details


def apply_saml_provider(
    *,
    provider_name: str,
    email_attribute: str,
    metadata_url: Optional[str] = None,
    metadata_xml: Optional[str] = None,
    idp_signout: bool = False,
) -> None:
    """Create-or-update the SAML IdP on the pool and add it to the
    app client's supported providers. Idempotent: re-applying the
    same config is a no-op-equivalent update. Raises
    :class:`CognitoApplyError` (with the Cognito reason) on failure
    — the route maps that to a 4xx so a bad metadata URL surfaces as
    a message, not a 500."""
    pool_id = _pool_id()
    client_id = _app_client_id()
    cognito = _client()
    details = _provider_details(
        metadata_url=metadata_url,
        metadata_xml=metadata_xml,
        idp_signout=idp_signout,
    )
    attr_map = {"email": email_attribute or "email"}

    try:
        existing = cognito.describe_identity_provider(
            UserPoolId=pool_id, ProviderName=provider_name)
    except Exception as e:  # noqa: BLE001 — normalize all to our type
        if _is_not_found(e):
            existing = None
        else:
            raise CognitoApplyError(_reason(e)) from e

    try:
        if existing is None:
            cognito.create_identity_provider(
                UserPoolId=pool_id,
                ProviderName=provider_name,
                ProviderType="SAML",
                ProviderDetails=details,
                AttributeMapping=attr_map,
            )
        else:
            cognito.update_identity_provider(
                UserPoolId=pool_id,
                ProviderName=provider_name,
                ProviderDetails=details,
                AttributeMapping=attr_map,
            )
    except Exception as e:  # noqa: BLE001
        raise CognitoApplyError(_reason(e)) from e

    _add_to_app_client(cognito, pool_id, client_id, provider_name)


# UpdateUserPoolClient is a FULL REPLACE, not a merge — any field not
# re-sent is wiped. So to change SupportedIdentityProviders we must
# read the current client and re-send every existing OAuth/auth field
# unchanged, or Cognito nulls CallbackURLs / AllowedOAuthFlows /
# AllowedOAuthScopes / LogoutURLs and flips AllowedOAuthFlowsUserPool-
# Client to false → no registered callback → login fails with
# redirect_mismatch. These are every settable field the read-back
# carries that we preserve verbatim (read-modify-write).
_PRESERVED_CLIENT_FIELDS = (
    "ClientName",
    "RefreshTokenValidity",
    "AccessTokenValidity",
    "IdTokenValidity",
    "TokenValidityUnits",
    "ReadAttributes",
    "WriteAttributes",
    "ExplicitAuthFlows",
    "CallbackURLs",
    "LogoutURLs",
    "DefaultRedirectURI",
    "AllowedOAuthFlows",
    "AllowedOAuthScopes",
    "AllowedOAuthFlowsUserPoolClient",
    "AnalyticsConfiguration",
    "PreventUserExistenceErrors",
    "EnableTokenRevocation",
    "EnablePropagateAdditionalUserContextData",
    "AuthSessionValidity",
)


def _update_supported_idps(
    cognito, pool_id: str, client_id: str, client: dict,
    supported: list,
) -> None:
    """Re-send the FULL app-client config with only
    SupportedIdentityProviders changed. `client` is the
    UserPoolClient dict from describe_user_pool_client. Preserves
    every existing OAuth/auth field so a SAML apply can't wipe the
    callback/flow/scope settings (the redirect_mismatch bug)."""
    kwargs = dict(UserPoolId=pool_id, ClientId=client_id)
    for field in _PRESERVED_CLIENT_FIELDS:
        if field in client and client[field] is not None:
            kwargs[field] = client[field]
    kwargs["SupportedIdentityProviders"] = supported
    try:
        cognito.update_user_pool_client(**kwargs)
    except Exception as e:  # noqa: BLE001
        raise CognitoApplyError(_reason(e)) from e


def _add_to_app_client(
    cognito, pool_id: str, client_id: str, provider_name: str,
) -> None:
    """Ensure the IdP is on the app client's
    SupportedIdentityProviders (preserving COGNITO + any others so
    the bootstrap password path stays available) AND that the app's
    /login is a registered LogoutURL. Read-modify-write: every other
    OAuth field is re-sent unchanged.

    The LogoutURL union is what makes idp_signout land back on tg: the
    logout handler hands the SPA a Cognito hosted-logout URL whose
    logout_uri is the app's /login, but Cognito only honors a
    logout_uri that's in the client's LogoutURLs allowlist — otherwise
    it ignores the requested return and drops the user on its own
    hosted page. The read-modify-write below PRESERVES the existing
    LogoutURLs verbatim but never added /login; register it here,
    idempotently, during the same apply."""
    try:
        desc = cognito.describe_user_pool_client(
            UserPoolId=pool_id, ClientId=client_id)
    except Exception as e:  # noqa: BLE001
        raise CognitoApplyError(_reason(e)) from e
    client = desc.get("UserPoolClient", {})
    supported = list(
        client.get("SupportedIdentityProviders") or [])
    changed = False
    if "COGNITO" not in supported:
        # Never drop the password/bootstrap path (lockout guard).
        supported.append("COGNITO")
        changed = True
    if provider_name not in supported:
        supported.append(provider_name)
        changed = True

    # Ensure the post-logout /login is an allowlisted LogoutURL so the
    # idp_signout return is honored. Mutate the client dict in place so
    # the augmented list rides through _update_supported_idps via
    # _PRESERVED_CLIENT_FIELDS (LogoutURLs). Idempotent — only the
    # absence of /login flips `changed`.
    login_uri = login_return_uri()
    if login_uri:
        logout_urls = list(client.get("LogoutURLs") or [])
        if login_uri not in logout_urls:
            logout_urls.append(login_uri)
            client["LogoutURLs"] = logout_urls
            changed = True

    if not changed:
        return
    _update_supported_idps(
        cognito, pool_id, client_id, client, supported)


def delete_saml_provider(provider_name: str) -> None:
    """Remove the IdP from the app client and delete the provider —
    reverting to Cognito-only login. Tolerant of a missing provider
    (already-gone is success). Raises :class:`CognitoApplyError` on
    a real failure."""
    pool_id = _pool_id()
    client_id = _app_client_id()
    cognito = _client()

    # Drop from the app client first so an in-flight login can't pick
    # a provider we're about to delete. Read-modify-write: re-send the
    # full client config with only the provider removed, so reverting
    # SSO can't wipe the OAuth callback/flow/scope settings either.
    try:
        desc = cognito.describe_user_pool_client(
            UserPoolId=pool_id, ClientId=client_id)
        client = desc.get("UserPoolClient", {})
        supported = [
            p for p in (client.get("SupportedIdentityProviders") or [])
            if p != provider_name
        ]
        if "COGNITO" not in supported:
            supported.append("COGNITO")
        _update_supported_idps(
            cognito, pool_id, client_id, client, supported)
    except CognitoApplyError:
        raise
    except Exception as e:  # noqa: BLE001
        if not _is_not_found(e):
            raise CognitoApplyError(_reason(e)) from e

    try:
        cognito.delete_identity_provider(
            UserPoolId=pool_id, ProviderName=provider_name)
    except Exception as e:  # noqa: BLE001
        if not _is_not_found(e):
            raise CognitoApplyError(_reason(e)) from e


def provider_live_status(provider_name: str) -> dict:
    """Live pool state for the Settings status line: is the IdP
    present on the pool, and is it on the app client's supported
    list? Best-effort — a lookup failure returns
    ``{"present": None, ...}`` with an ``error`` reason rather than
    raising, so a status GET never 500s."""
    try:
        pool_id = _pool_id()
        client_id = _app_client_id()
        cognito = _client()
    except CognitoApplyError as e:
        return {"present": None, "on_app_client": None,
                "error": str(e)}
    present = None
    on_client = None
    try:
        cognito.describe_identity_provider(
            UserPoolId=pool_id, ProviderName=provider_name)
        present = True
    except Exception as e:  # noqa: BLE001
        if _is_not_found(e):
            present = False
        else:
            return {"present": None, "on_app_client": None,
                    "error": _reason(e)}
    try:
        desc = cognito.describe_user_pool_client(
            UserPoolId=pool_id, ClientId=client_id)
        supported = (
            desc.get("UserPoolClient", {})
            .get("SupportedIdentityProviders") or [])
        on_client = provider_name in supported
    except Exception as e:  # noqa: BLE001
        return {"present": present, "on_app_client": None,
                "error": _reason(e)}
    return {"present": present, "on_app_client": on_client,
            "error": None}


def registration_values(email_attribute: str = "email") -> dict:
    """The **Cognito SP** values to register on the IdP / IAM Identity
    Center side (the "config info to take to IDC"). Computed from the
    pool id + region + the pool's hosted-UI domain. The ACS URL needs
    the domain, looked up live; if the domain lookup fails we still
    return the entity id + attribute and an ``acs_url_error`` so the
    UI shows the copyable values it can and explains the rest."""
    out: dict = {
        "sp_entity_id": None,
        "acs_url": None,
        "email_attribute": email_attribute or "email",
        "acs_url_error": None,
    }
    try:
        pool_id = _pool_id()
    except CognitoApplyError as e:
        out["acs_url_error"] = str(e)
        return out
    out["sp_entity_id"] = f"urn:amazon:cognito:sp:{pool_id}"
    domain = _pool_domain(pool_id)
    if domain:
        out["acs_url"] = (
            f"https://{domain}.auth.{_region()}.amazoncognito.com"
            "/saml2/idpresponse")
    else:
        out["acs_url_error"] = (
            "Cognito pool has no hosted-UI domain configured; "
            "set one to obtain the ACS URL")
    return out


def _pool_domain(pool_id: str) -> Optional[str]:
    """The pool's hosted-UI domain prefix (the ``<domain>`` in the
    ACS URL), or None if unset/lookup-failed."""
    try:
        cognito = _client()
        desc = cognito.describe_user_pool(UserPoolId=pool_id)
        domain = desc.get("UserPool", {}).get("Domain")
        return domain or None
    except Exception:  # noqa: BLE001 — best-effort; None is handled
        return None


# ── boto3 error normalization ────────────────────────────────────────


def _is_not_found(e: Exception) -> bool:
    code = getattr(e, "response", {}).get(
        "Error", {}).get("Code", "") if hasattr(e, "response") else ""
    return code in ("ResourceNotFoundException",
                    "NoSuchEntityException")


def _reason(e: Exception) -> str:
    """A concise, surfaceable reason from a botocore error."""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        err = resp.get("Error", {})
        code = err.get("Code", "")
        msg = err.get("Message", "")
        if code or msg:
            return f"{code}: {msg}".strip(": ").strip()
    return f"{type(e).__name__}: {e}"
