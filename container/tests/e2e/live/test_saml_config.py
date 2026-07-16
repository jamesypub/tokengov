"""Live SAML-config drift guard: a Settings-Save leaves the real
Cognito identity provider with request-signing OFF and SLO unchanged.

The #1371 fix: for the AWS IDC SAML provider, Cognito's
RequestSigningAlgorithm must be absent/empty (a real value signs the
SP-initiated AuthnRequest, which IDC rejects → SAML login breaks). The
#1439 fix OMITS the key (real Cognito rejects ""). SLO (IDPSignout) is
a DIFFERENT thing: an admin-controlled toggle (#1373 kept it
admin-controlled, NOT forced off) — cognito_saml.apply_saml_provider
sets IDPSignout to whatever the admin chose. So the drift invariant is:
request-signing forced OFF, and a no-op Save does NOT flip the admin's
SLO setting. A unit test covers the API layer; this adds the missing
LIVE assertion — a Settings-Save against a real SAML-configured target
must not re-introduce request-signing (or flip SLO) on the actual
Cognito provider (the only place the regression would bite).

This is live-only (needs a real Cognito pool): it skips cleanly and
credential-free on the no_aws CI tier. Honest-skip on a target with no
SAML provider (a Cognito-only stack). No product code — test-only.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def _pool_id_from_registration(saml: dict) -> str | None:
    """Extract the Cognito user-pool id from the API response's
    registration.sp_entity_id (urn:amazon:cognito:sp:<pool_id>) — so the
    test never hardcodes a target's pool id (AC: no hardcoded demo0
    identifiers)."""
    ent = (saml.get("registration") or {}).get("sp_entity_id") or ""
    # urn:amazon:cognito:sp:us-east-1_XXXX → the trailing pool id.
    return ent.rsplit(":", 1)[-1] if ent.startswith("urn:") else None


def test_settings_save_keeps_saml_provider_drift_free(
        live_base, live_client, aws_session):
    """A no-op PUT /api/settings/saml leaves the LIVE Cognito provider
    with no effective RequestSigningAlgorithm (forced off, #1371) and
    IDPSignout unchanged from what the Save sent (SLO is admin-
    controlled, #1373 — not forced off).

    Reads the pool id + provider name from the API response (no
    hardcoded identifiers), reads the real provider via
    cognito-idp describe-identity-provider, and asserts the drift-free
    end-state cognito_saml applies. Honest-skip when the target has no
    SAML provider configured."""
    # 1. Snapshot the current SAML config.
    r = live_client.get("/api/settings/saml")
    r.raise_for_status()
    saml = r.json()

    provider_name = saml.get("provider_name")
    if not provider_name:
        pytest.skip(
            "target has no SAML provider configured "
            "(Cognito-only stack) — nothing to assert drift on.")

    pool_id = _pool_id_from_registration(saml)
    if not pool_id:
        pytest.skip(
            "could not resolve the Cognito pool id from the API "
            "response (registration.sp_entity_id missing) — cannot "
            "read the live provider.")

    # 2. No-op Save: PUT the SAME connection fields back. This drives
    #    apply_saml_provider → cognito update_identity_provider, which
    #    is where a request-signing regression would re-appear. Only the
    #    connection fields the PUT recognizes are sent; a no-op leaves
    #    the provider unchanged (no teardown needed).
    body = {"provider_name": provider_name}
    for k in ("metadata_url", "metadata_xml", "email_attribute"):
        if saml.get(k):
            body[k] = saml[k]
    # Preserve the current SLO setting on the round-trip: SLO (IDPSignout)
    # is an admin-controlled toggle, NOT a forced-off invariant (#1373
    # kept it admin-controlled — only request-signing is forced off,
    # #1371). Send the current value so we can assert a no-op Save
    # doesn't FLIP it, regardless of what the admin chose on this target.
    sent_idp_signout = bool(saml.get("idp_signout", False))
    body["idp_signout"] = sent_idp_signout
    r = live_client.put("/api/settings/saml", json_body=body)
    r.raise_for_status()

    # 3. Read the REAL Cognito provider (not the API's view — the whole
    #    point is drift in the live provider).
    cognito = aws_session.client("cognito-idp")
    desc = cognito.describe_identity_provider(
        UserPoolId=pool_id, ProviderName=provider_name)
    details = desc["IdentityProvider"]["ProviderDetails"]

    # 4a. No EFFECTIVE request signing — the real #1371 invariant. "Off"
    #     = the key absent/empty (the #1439 fix OMITS it; real Cognito
    #     rejects ""), so absent OR empty both count as off; a non-empty
    #     algorithm (e.g. rsa-sha256) is the login-breaking regression.
    rsa = details.get("RequestSigningAlgorithm", "")
    assert not rsa, (
        "RequestSigningAlgorithm is set on the live provider "
        f"({rsa!r}) after a Settings-Save — this signs the SP-initiated "
        "AuthnRequest and breaks IDC SAML login (#1371). It must be "
        "absent/empty.")

    # 4b. SLO (IDPSignout) ROUND-TRIPS the value the Save sent — a no-op
    #     Save must not flip it. It is admin-controlled (#1373), NOT
    #     forced off, so a target with SLO on (e.g. stage) is valid; the
    #     invariant is "Save doesn't change it", not "always false".
    #     Cognito stores the flag as the string "true"/"false".
    want_signout = "true" if sent_idp_signout else "false"
    assert details.get("IDPSignout") == want_signout, (
        f"IDPSignout must round-trip the sent value ({want_signout!r}) "
        f"after a no-op Save — a Save must not flip SLO — got "
        f"{details.get('IDPSignout')!r} (#1373; SLO is admin-controlled).")
