"""
notify — shared notification sender for worker- and api-side alerts.

Two optional, independent transports, both configured by an org admin
in the Settings UI (stored in the admin_config kv store, secrets
encrypted at rest):

  - send_alert(to, subject, body) — a plain-text email over a generic
    SMTP relay (any provider, incl. SES-as-SMTP). Replaces the earlier
    AWS-only SES SDK path so the product isn't bound to one vendor.
  - send_webhook(message) — a one-line announcement POSTed to a
    Slack/Teams/generic incoming-webhook URL.

Sending must NEVER raise: a failed notification cannot be allowed to
break the reconciler (governance is the priority). Every path returns
a small dict — {"sent": True, ...} on success, {"sent": False,
"reason": ...} otherwise — so callers can log a soft warning instead
of aborting.
"""
from __future__ import annotations
import json
import logging
import os

log = logging.getLogger("worker.notify")


def _read_smtp_config() -> dict:
    """Open the DB and return the SMTP transport config. Factored out
    so callers (and tests) have ONE patch point that doesn't require a
    live DB — tests monkeypatch this to a canned dict."""
    from db.session import get_db
    from db.notify_config import get_smtp_config
    with get_db() as db:
        return get_smtp_config(db)


def _read_webhook_url() -> str:
    """Open the DB and return the decrypted alert webhook URL. ONE
    patch point (tests monkeypatch this to a canned value)."""
    from db.session import get_db
    from db.notify_config import get_webhook_url
    with get_db() as db:
        return get_webhook_url(db)


def send_alert(to: str, subject: str, body: str) -> dict:
    """Send a plain-text email to `to` over the configured SMTP relay.
    SMTP config (host/port/user/password/from/tls) lives in the DB
    (admin_config). If host/from is unset → soft
    {"sent": False, "reason": "SMTP not configured"}; no recipient →
    {"sent": False, "reason": "no recipient"}. Returns
    {"sent": True, "to": to} on success. NEVER raises — any error is
    returned as a soft sent:False with the exception in the reason."""
    if not to:
        return {"sent": False, "reason": "no recipient"}
    try:
        import smtplib
        from email.message import EmailMessage
        cfg = _read_smtp_config()
        if not cfg["host"] or not cfg["from"]:
            return {"sent": False, "reason": "SMTP not configured"}

        msg = EmailMessage()
        msg["From"] = cfg["from"]
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        tls = cfg["tls"]
        host, port = cfg["host"], cfg["port"]
        if tls == "tls":
            server = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            server = smtplib.SMTP(host, port, timeout=10)
        try:
            if tls == "starttls":
                server.starttls()
            if cfg["username"]:
                server.login(cfg["username"], cfg["password"])
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
        return {"sent": True, "to": to}
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "reason": f"{type(e).__name__}: {e}"}


def send_webhook(message: str) -> dict:
    """POST a one-line announcement to the configured alert webhook
    URL (Slack/Teams/generic incoming webhook), as JSON
    {"text": message}. URL lives in the DB (admin_config, encrypted).
    No URL → soft {"sent": False, "reason": "webhook not configured"}.
    Returns {"sent": True} on a 2xx. NEVER raises.

    SSRF note: the URL is org-admin-supplied (trusted) and stdlib
    urllib follows http redirects by default; we accept that for v1
    since only an org admin can set the URL. A future hardening could
    pin the host / refuse redirects."""
    try:
        import urllib.request
        url = _read_webhook_url()
        if not url:
            return {"sent": False, "reason": "webhook not configured"}
        data = json.dumps({"text": message}).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            status = getattr(r, "status", None) or r.getcode()
            if 200 <= int(status) < 300:
                return {"sent": True}
            return {"sent": False, "reason": f"HTTP {status}"}
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "reason": f"{type(e).__name__}: {e}"}


def app_url() -> str | None:
    """The admin UI's base URL for the login-link line in alert
    emails. Prefer an explicit TG_APP_URL; else derive the origin
    (scheme + host, no path) of the OIDC redirect_uri so a federated
    install needs no extra env. None when neither is configured —
    callers omit the link line gracefully."""
    explicit = os.environ.get("TG_APP_URL", "").strip()
    if explicit:
        return explicit
    from urllib.parse import urlsplit, urlunsplit
    redirect = os.environ.get("TG_OIDC_REDIRECT_URI", "").strip()
    if not redirect:
        return None
    parts = urlsplit(redirect)
    if not (parts.scheme and parts.netloc):
        return None
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
