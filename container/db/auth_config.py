"""
Runtime SAML / SSO login config.

Externalizes the federated-login setup that used to be baked into
CFN/env at install time (`cfn/tg-cognito-pool.yaml` Okta* params,
`TG_OIDC_OKTA_PROVIDER_NAME` / `TG_OIDC_OKTA_DISPLAY_NAME`) into the
`admin_config` kv store, so an org-admin can wire a customer's IdP —
or just rename the login button — from Settings with **no redeploy**.

Stored keys (all in `admin_config`):

  - ``sso_button_label``    — federated login-button text; defaults to
    ``Login with Your SSO`` (generic, not customer-specific). A
    label-only change never touches Cognito.
  - ``saml_provider_name``  — the Cognito IdP name; this is the
    ``identity_provider=`` value the authorize endpoint receives, so it
    must match what ``/auth/login`` sends.
  - ``saml_metadata_url``   — IdP SAML metadata URL (preferred: Cognito
    auto-refreshes it ~6h). Mutually exclusive with the XML below.
  - ``saml_metadata_xml``   — uploaded IdP metadata XML (fallback when
    no URL is available; no auto-refresh).
  - ``saml_email_attribute``— the SAML assertion attribute mapped to
    Cognito's ``email`` (the claim tg matches users on). Defaults to
    ``email``.
  - ``saml_idp_signout``    — bool; enable SAML single-logout.

The protocol is **SAML 2.0 only** for this ticket (AWS IAM Identity
Center integrates a third-party web app as a SAML 2.0 customer-managed
application; OIDC-to-external-IdP is a deferred follow-up — Cognito
already covers the OIDC-shaped case). No client secret is stored: SAML
uses the IdP's *public* metadata.

This module is pure config-as-data (read/write `admin_config`). The
side-effecting apply to the live Cognito pool lives in
``api/cognito_saml.py``; the HTTP surface is in
``api/routes/settings.py``. ``auth_mode`` is **derived** from whether a
SAML provider name is configured — it is not a separate stored key, so
the two can never disagree. ``db.org_config.tg_owns_directory`` keys off
the same derivation (an external SAML IdP owns the directory), so the
existing ``/auth/providers`` switch keeps working with no env var.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from sqlalchemy.orm import Session

from db.models import AdminConfig

SSO_BUTTON_LABEL_KEY   = "sso_button_label"
SAML_PROVIDER_NAME_KEY = "saml_provider_name"
SAML_METADATA_URL_KEY  = "saml_metadata_url"
SAML_METADATA_XML_KEY  = "saml_metadata_xml"
SAML_EMAIL_ATTR_KEY    = "saml_email_attribute"
SAML_IDP_SIGNOUT_KEY   = "saml_idp_signout"

# Generic by design — not customer-specific. An admin renames it from
# Settings; the default reads cleanly on any deployment.
DEFAULT_SSO_BUTTON_LABEL = "Login with Your SSO"
DEFAULT_SAML_EMAIL_ATTR   = "email"


def _get(db: Session, key: str) -> Optional[str]:
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == key)
        .first()
    )
    return row.value if row else None


def _set(db: Session, key: str, value: Optional[str]) -> None:
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == key)
        .first()
    )
    if value is None:
        if row is not None:
            db.delete(row)
        return
    if row:
        row.value = value
    else:
        db.add(AdminConfig(key=key, value=value))


# ── Button label ─────────────────────────────────────────────────────


def get_sso_button_label(db: Session) -> str:
    """The federated login-button text. Defaults to the generic
    ``Login with Your SSO`` when unset."""
    val = _get(db, SSO_BUTTON_LABEL_KEY)
    if val is not None and val.strip():
        return val.strip()
    return DEFAULT_SSO_BUTTON_LABEL


def get_sso_button_label_raw(db: Session) -> Optional[str]:
    """The stored label, or None when no row is set. Lets a caller
    fall back to a legacy env display-name before applying the
    default (preserves per-tenant env labeling for installs not yet
    migrated to the runtime label)."""
    val = _get(db, SSO_BUTTON_LABEL_KEY)
    if val is not None and val.strip():
        return val.strip()
    return None


def set_sso_button_label(db: Session, label: str) -> str:
    """Persist the SSO button label. A blank/whitespace label
    resets to the default (so the field can never go empty on the
    Login page). Label-only change — never touches Cognito."""
    cleaned = (label or "").strip()
    if not cleaned:
        cleaned = DEFAULT_SSO_BUTTON_LABEL
    _set(db, SSO_BUTTON_LABEL_KEY, cleaned)
    db.flush()
    return cleaned


# ── SAML config ──────────────────────────────────────────────────────


def get_saml_config(db: Session) -> dict:
    """The stored SAML connection config. ``configured`` is True
    only when a provider name is present (the minimum needed for
    ``/auth/login?identity_provider=<name>`` to route to the IdP)."""
    name = _get(db, SAML_PROVIDER_NAME_KEY)
    name = name.strip() if name else ""
    email_attr = _get(db, SAML_EMAIL_ATTR_KEY)
    signout = _get(db, SAML_IDP_SIGNOUT_KEY)
    return {
        "configured": bool(name),
        "provider_name": name or None,
        "metadata_url": (_get(db, SAML_METADATA_URL_KEY) or None),
        # XML can be large; surface only a presence flag, not the blob.
        "has_metadata_xml": bool(_get(db, SAML_METADATA_XML_KEY)),
        "email_attribute": (
            (email_attr.strip() if email_attr else "")
            or DEFAULT_SAML_EMAIL_ATTR),
        "idp_signout": (str(signout).strip().lower() == "true"
                        if signout is not None else False),
    }


def get_saml_metadata_xml(db: Session) -> Optional[str]:
    """The raw uploaded metadata XML (used by the Cognito apply
    path), or None when the URL form is in use."""
    return _get(db, SAML_METADATA_XML_KEY)


def set_saml_config(
    db: Session,
    *,
    provider_name: str,
    metadata_url: Optional[str] = None,
    metadata_xml: Optional[str] = None,
    email_attribute: Optional[str] = None,
    idp_signout: bool = False,
) -> dict:
    """Persist the SAML connection. Exactly one of ``metadata_url``
    / ``metadata_xml`` must be provided. Raises ValueError on bad
    input; the caller (settings route) maps that to a 400. Does NOT
    apply to Cognito — that's the route's job after this returns."""
    name = (provider_name or "").strip()
    if not name:
        raise ValueError("provider_name is required")
    # Cognito provider names: letters/digits and a small punctuation
    # set, no spaces (it becomes a URL query value).
    if any(c.isspace() for c in name):
        raise ValueError("provider_name must not contain whitespace")

    url = (metadata_url or "").strip()
    xml = metadata_xml if (metadata_xml and metadata_xml.strip()) else None
    if url and xml:
        raise ValueError(
            "provide either metadata_url or metadata_xml, not both")
    if not url and not xml:
        raise ValueError(
            "one of metadata_url or metadata_xml is required")
    if url and not (url.startswith("https://")
                    or url.startswith("http://")):
        raise ValueError("metadata_url must be an http(s) URL")

    attr = (email_attribute or "").strip() or DEFAULT_SAML_EMAIL_ATTR

    _set(db, SAML_PROVIDER_NAME_KEY, name)
    # Keep the two metadata forms mutually exclusive in storage too.
    _set(db, SAML_METADATA_URL_KEY, url or None)
    _set(db, SAML_METADATA_XML_KEY, xml)
    _set(db, SAML_EMAIL_ATTR_KEY, attr)
    _set(db, SAML_IDP_SIGNOUT_KEY,
         "true" if idp_signout else "false")
    db.flush()
    return get_saml_config(db)


def clear_saml_config(db: Session) -> None:
    """Remove the stored SAML connection (revert to Cognito-only).
    Leaves the button label alone — it's harmless when no IdP is
    wired and the admin may want to keep it for a re-enable."""
    for key in (
        SAML_PROVIDER_NAME_KEY,
        SAML_METADATA_URL_KEY,
        SAML_METADATA_XML_KEY,
        SAML_EMAIL_ATTR_KEY,
        SAML_IDP_SIGNOUT_KEY,
    ):
        _set(db, key, None)
    db.flush()


def saml_provider_name(db: Session) -> Optional[str]:
    """The configured Cognito IdP name, or None. Used by
    ``/auth/login`` / ``/auth/providers`` to route the federated
    button (DB-first; env is the fallback in auth_routes)."""
    name = _get(db, SAML_PROVIDER_NAME_KEY)
    name = name.strip() if name else ""
    return name or None


# ── env → DB seed (first boot) ───────────────────────────────────────


def seed_saml_config_from_env(db: Session) -> None:
    """One-time seed on bootstrap/migration, mirroring
    ``seed_tg_owns_directory``. When the install was
    configured via the build-time Okta env vars / CFN params and no
    runtime SAML config exists yet, seed the DB from them so an
    existing federated deployment sees its current setup pre-populated
    and editable — and runtime config is authoritative thereafter.

    Insert-if-absent: never overwrites an admin's later change. The
    button label seeds from ``TG_OIDC_OKTA_DISPLAY_NAME`` only when
    that env names a real display value; otherwise the default applies
    lazily via :func:`get_sso_button_label` (we don't write a row).
    """
    # Seed the SAML connection only if a provider name env is present
    # AND no runtime provider name exists.
    env_provider = os.environ.get(
        "TG_OIDC_OKTA_PROVIDER_NAME", "").strip()
    if env_provider and _get(db, SAML_PROVIDER_NAME_KEY) is None:
        env_meta = os.environ.get(
            "TG_OIDC_OKTA_METADATA_URL", "").strip()
        env_attr = os.environ.get(
            "TG_OIDC_OKTA_EMAIL_ATTRIBUTE", "").strip()
        _set(db, SAML_PROVIDER_NAME_KEY, env_provider)
        if env_meta:
            _set(db, SAML_METADATA_URL_KEY, env_meta)
        _set(db, SAML_EMAIL_ATTR_KEY,
             env_attr or DEFAULT_SAML_EMAIL_ATTR)
        _set(db, SAML_IDP_SIGNOUT_KEY, "false")

    # Seed the button label from the display-name env, insert-if-absent.
    env_label = os.environ.get(
        "TG_OIDC_OKTA_DISPLAY_NAME", "").strip()
    if env_label and _get(db, SSO_BUTTON_LABEL_KEY) is None:
        _set(db, SSO_BUTTON_LABEL_KEY, env_label)

    db.flush()
