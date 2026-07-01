"""
Notification delivery tests — validate that a spend-cap alert is
actually SENT and that its CONTENT is correct, end to end, on every
PR. Closes the delivery leg that the API-layer UAT could only mark
BLOCKED (no mail-capture target).

These drive the *real* artifacts:
  - `notify.send_alert` against a real Mailpit container (SMTP in,
    capture REST API out) — not a mock of smtplib.
  - `notify.send_webhook` against a real in-test HTTP server.
  - the reconciler's real fan-out `_send_spend_alerts` →
    `_alert_recipients` (user + team admin) + per-event webhook.

Mailpit is a TEST DEPENDENCY ONLY (the session-scoped `mailpit`
fixture in conftest.py); it must never reach the shipped runtime.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from db.session import get_db
from db.models import User, Team, AdminRole
from db.notify_config import set_smtp_config, set_webhook_url
from worker import notify
from worker.jobs import deny_reconciler


# ── helpers ──────────────────────────────────────────────────────────


def _mailpit_base(mailpit) -> str:
    """The Mailpit HTTP API base URL for the running container."""
    host = mailpit.get_container_host_ip()
    port = mailpit.get_exposed_port(8025)
    return f"http://{host}:{port}"


def _smtp_hostport(mailpit) -> tuple[str, int]:
    host = mailpit.get_container_host_ip()
    port = int(mailpit.get_exposed_port(1025))
    return host, port


def _configure_smtp(mailpit) -> None:
    """Point the SMTP transport at Mailpit (no auth, no TLS — Mailpit
    accepts plaintext on 1025)."""
    host, port = _smtp_hostport(mailpit)
    with get_db() as db:
        set_smtp_config(
            db,
            smtp_host=host,
            smtp_port=port,
            smtp_from="alerts@test.local",
            smtp_tls="none",
        )


def _messages_to(mailpit, address: str) -> list[dict]:
    """All captured Mailpit messages addressed to `address` (matches
    on the To list). Reads the list endpoint."""
    base = _mailpit_base(mailpit)
    r = httpx.get(f"{base}/api/v1/messages", timeout=10)
    r.raise_for_status()
    out = []
    for m in r.json().get("messages", []):
        tos = [t.get("Address", "") for t in (m.get("To") or [])]
        if address in tos:
            out.append(m)
    return out


def _message_detail(mailpit, msg_id: str) -> dict:
    base = _mailpit_base(mailpit)
    r = httpx.get(f"{base}/api/v1/message/{msg_id}", timeout=10)
    r.raise_for_status()
    return r.json()


def _seed_user_with_team_admin(runtag: str) -> tuple[str, str]:
    """Seed a team, a user on it, and a team_admin for that team.
    Returns (user_email, admin_email), both `+runtag`-subaddressed so
    captured mail from one test/run never matches another's."""
    user_email = f"dev+{runtag}@test.local"
    admin_email = f"admin+{runtag}@test.local"
    with get_db() as db:
        db.add(Team(team_id=f"team-{runtag}", name=f"Team {runtag}"))
        db.add(User(
            email=user_email,
            status="active",
            cap_usd=10.0,
            team_id=f"team-{runtag}",
        ))
        db.add(AdminRole(
            email=admin_email,
            role="team_admin",
            team_id=f"team-{runtag}",
        ))
        db.flush()
    return user_email, admin_email


def _send_alerts_for(user_email: str, events, **kw) -> int:
    """Load the user and drive the reconciler fan-out within ONE
    session, so the User stays bound (the reconciler reads attributes
    off it). Returns the email count."""
    with get_db() as db:
        user = db.query(User).filter(User.email == user_email).first()
        return deny_reconciler._send_spend_alerts(
            db, user, events, **kw)


# ── webhook capture server ───────────────────────────────────────────


class _CaptureServer:
    """A throwaway local HTTP server (stdlib, no new dep) that records
    the JSON bodies POSTed to it — used to assert send_webhook fires
    with the right payload."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        captured = self.posts

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    captured.append(json.loads(raw))
                except Exception:  # noqa: BLE001
                    captured.append({"_raw": raw.decode("utf-8", "replace")})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):  # silence
                pass

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_port}/hook"

    def __enter__(self) -> "_CaptureServer":
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


# ── tests ────────────────────────────────────────────────────────────


def test_send_alert_delivers_and_content_is_correct(clean_db, mailpit):
    """send_alert → Mailpit, read the message back over the capture
    API, and assert To / Subject / body content. Proves BOTH that the
    mail was sent AND that the content is right."""
    _configure_smtp(mailpit)
    to = "alice+content@test.local"
    subject = "Your Bedrock access is paused (spend cap reached)"
    body = (
        "You're over your monthly Bedrock spend cap, so your Bedrock "
        "access has been paused.\n\nSpend: $12.00 of $10.00 (~120%).\n")

    res = notify.send_alert(to, subject, body)
    assert res["sent"] is True, res

    msgs = _messages_to(mailpit, to)
    assert len(msgs) == 1, f"expected 1 message to {to}, got {msgs}"
    detail = _message_detail(mailpit, msgs[0]["ID"])
    assert detail["From"]["Address"] == "alerts@test.local"
    assert any(t["Address"] == to for t in detail["To"])
    assert detail["Subject"] == subject
    # Body content (Mailpit normalizes to CRLF) — assert on the
    # meaningful substrings, not byte-exact line endings.
    assert "over your monthly Bedrock spend cap" in detail["Text"]
    assert "$12.00 of $10.00" in detail["Text"]


def test_reconciler_fanout_emails_user_and_team_admin(clean_db, mailpit):
    """Drive the real reconciler fan-out (_send_spend_alerts →
    _alert_recipients) for a `blocked` event and assert BOTH the user
    and their team admin get a correctly-addressed, correctly-bodied
    email."""
    _configure_smtp(mailpit)
    user_email, admin_email = _seed_user_with_team_admin("block")

    sent = _send_alerts_for(
        user_email, ["blocked"],
        effective_spend=12.0, cap=10.0, enforce_on_estimate=False)
    # One email to the user + one to the team admin.
    assert sent == 2, sent

    user_msgs = _messages_to(mailpit, user_email)
    admin_msgs = _messages_to(mailpit, admin_email)
    assert len(user_msgs) == 1, user_msgs
    assert len(admin_msgs) == 1, admin_msgs

    user_detail = _message_detail(mailpit, user_msgs[0]["ID"])
    assert "paused" in user_detail["Subject"].lower()
    assert "access has been paused" in user_detail["Text"]

    admin_detail = _message_detail(mailpit, admin_msgs[0]["ID"])
    # The admin email names the user it's about.
    assert user_email in admin_detail["Subject"]
    assert user_email in admin_detail["Text"]


def test_reconciler_fanout_unblock_event(clean_db, mailpit):
    """Block AND unblock both produce mail — here the `unblocked`
    event, asserting the restored-access content reaches the user."""
    _configure_smtp(mailpit)
    user_email, admin_email = _seed_user_with_team_admin("unblock")

    sent = _send_alerts_for(
        user_email, ["unblocked"],
        effective_spend=4.0, cap=10.0, enforce_on_estimate=False)
    assert sent == 2, sent

    user_detail = _message_detail(
        mailpit, _messages_to(mailpit, user_email)[0]["ID"])
    assert "restored" in user_detail["Subject"].lower()
    assert "restored" in user_detail["Text"].lower()


def test_send_webhook_posts_expected_text():
    """send_webhook POSTs {"text": <message>} to the configured URL —
    asserted against a real in-test capture server."""
    with _CaptureServer() as server:
        with get_db() as db:
            set_webhook_url(db, server.url)
        message = "alice@test.local was blocked — over Bedrock spend cap"
        res = notify.send_webhook(message)
        assert res["sent"] is True, res
        assert len(server.posts) == 1, server.posts
        assert server.posts[0] == {"text": message}


def test_reconciler_fanout_fires_webhook_once_per_event(clean_db):
    """The reconciler fires ONE webhook announcement per event (a
    channel post is shared, not per-recipient). Drive a blocked event
    for a user+admin and assert exactly one POST."""
    with _CaptureServer() as server:
        with get_db() as db:
            set_webhook_url(db, server.url)
        # No SMTP configured here → email is a soft skip; webhook is
        # the surface under test.
        user_email, _ = _seed_user_with_team_admin("hook")
        _send_alerts_for(
            user_email, ["blocked"],
            effective_spend=12.0, cap=10.0, enforce_on_estimate=False)
        assert len(server.posts) == 1, server.posts
        assert user_email in server.posts[0]["text"]
        assert "blocked" in server.posts[0]["text"].lower()


def test_unconfigured_is_soft_skip_and_does_not_raise(clean_db):
    """Fail-soft guard (regression for the never-raise contract): with
    neither SMTP nor a webhook configured, send_alert/send_webhook
    return sent:false and the reconciler fan-out does not raise."""
    # admin_config truncated by clean_db → nothing configured.
    assert notify.send_alert(
        "x@test.local", "s", "b") == {
            "sent": False, "reason": "SMTP not configured"}
    assert notify.send_webhook("hi") == {
        "sent": False, "reason": "webhook not configured"}

    user_email, _ = _seed_user_with_team_admin("soft")
    # Must NOT raise even though both transports are unconfigured.
    sent = _send_alerts_for(
        user_email, ["blocked"],
        effective_spend=12.0, cap=10.0, enforce_on_estimate=False)
    assert sent == 0
