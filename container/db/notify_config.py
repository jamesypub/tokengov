"""
Notification transport config — a thin org_config-style helper over
the `admin_config` kv store for the generic SMTP email transport and
the optional Slack/webhook announcement.

Replaces the earlier hardcoded SES-SDK path: instead of an AWS-only
sender, an org admin configures any SMTP relay (incl. SES-as-SMTP) in
the Settings UI, plus an optional incoming-webhook URL for a one-line
Slack/Teams announcement. The SMTP password and the webhook URL are
BEARER SECRETS, so they are encrypted at rest via db.crypto; everything
else (host, port, username, from, tls mode) is plaintext config.

Keys (admin_config.key):
  smtp_host, smtp_port, smtp_username,
  smtp_password      — ENCRYPTED at rest
  smtp_from
  smtp_tls           — enum none|starttls|tls (default starttls)
  alert_webhook_url  — ENCRYPTED at rest
"""
from __future__ import annotations
import os

from sqlalchemy.orm import Session

from db.models import AdminConfig
from db import crypto

SMTP_HOST_KEY     = "smtp_host"
SMTP_PORT_KEY     = "smtp_port"
SMTP_USERNAME_KEY = "smtp_username"
SMTP_PASSWORD_KEY = "smtp_password"      # encrypted
SMTP_FROM_KEY     = "smtp_from"
SMTP_TLS_KEY      = "smtp_tls"
WEBHOOK_URL_KEY   = "alert_webhook_url"  # encrypted

VALID_TLS = ("none", "starttls", "tls")
DEFAULT_TLS = "starttls"


def _get(db: Session, key: str, default=None):
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == key)
        .first()
    )
    return row.value if (row and row.value is not None) else default


def _set(db: Session, key: str, value: str) -> None:
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == key)
        .first()
    )
    if row:
        row.value = value
    else:
        db.add(AdminConfig(key=key, value=value))
    db.flush()


def get_smtp_config(db: Session) -> dict:
    """The SMTP transport config (password DECRYPTED). DB is the
    primary source; a legacy env var is read only as a fallback when a
    value is unset, so a pre-UI install still has a sane default. The
    tls mode coerces any unrecognized stored value to the default."""
    host = _get(db, SMTP_HOST_KEY) or os.environ.get("SMTP_HOST", "")
    port_raw = _get(db, SMTP_PORT_KEY) or os.environ.get("SMTP_PORT", "")
    try:
        port = int(port_raw) if port_raw else 587
    except (TypeError, ValueError):
        port = 587
    username = (
        _get(db, SMTP_USERNAME_KEY)
        or os.environ.get("SMTP_USERNAME", "")
    )
    password = crypto.decrypt(_get(db, SMTP_PASSWORD_KEY) or "")
    frm = _get(db, SMTP_FROM_KEY) or os.environ.get("SMTP_FROM", "")
    tls = (_get(db, SMTP_TLS_KEY) or DEFAULT_TLS).strip().lower()
    if tls not in VALID_TLS:
        tls = DEFAULT_TLS
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "from": frm,
        "tls": tls,
    }


def set_smtp_config(db: Session, **fields) -> None:
    """Write only the provided keys. The password is encrypted before
    storage. `smtp_tls` must be in {none,starttls,tls}; `smtp_port`
    must be an int in 1..65535 if provided. Raises ValueError on a bad
    value."""
    if "smtp_host" in fields:
        _set(db, SMTP_HOST_KEY, str(fields["smtp_host"] or ""))
    if "smtp_username" in fields:
        _set(db, SMTP_USERNAME_KEY, str(fields["smtp_username"] or ""))
    if "smtp_from" in fields:
        _set(db, SMTP_FROM_KEY, str(fields["smtp_from"] or ""))
    if "smtp_tls" in fields:
        tls = str(fields["smtp_tls"] or "").strip().lower()
        if tls not in VALID_TLS:
            raise ValueError(
                "smtp_tls must be one of none|starttls|tls")
        _set(db, SMTP_TLS_KEY, tls)
    if "smtp_port" in fields:
        try:
            port = int(fields["smtp_port"])
        except (TypeError, ValueError):
            raise ValueError("smtp_port must be an integer 1..65535")
        if port < 1 or port > 65535:
            raise ValueError("smtp_port must be an integer 1..65535")
        _set(db, SMTP_PORT_KEY, str(port))
    if "smtp_password" in fields:
        pw = str(fields["smtp_password"] or "")
        _set(db, SMTP_PASSWORD_KEY, crypto.encrypt(pw))


def get_webhook_url(db: Session) -> str:
    """The decrypted alert webhook URL ("" when unset)."""
    return crypto.decrypt(_get(db, WEBHOOK_URL_KEY) or "")


def set_webhook_url(db: Session, url: str) -> None:
    """Store the alert webhook URL encrypted at rest."""
    _set(db, WEBHOOK_URL_KEY, crypto.encrypt(str(url or "")))


def smtp_configured(db: Session) -> bool:
    """True when both an SMTP host and a From address are set — the
    minimum to actually send."""
    cfg = get_smtp_config(db)
    return bool(cfg["host"] and cfg["from"])


def webhook_configured(db: Session) -> bool:
    """True when an alert webhook URL is set."""
    return bool(get_webhook_url(db))
