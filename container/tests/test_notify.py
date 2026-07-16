"""
Tests for worker/notify.py — the SMTP email sender + webhook
announcement + app_url derivation. Neither send must ever raise:
unconfigured / no-recipient / a transport exception all return a soft
{"sent": False, "reason": ...}.

The SMTP/webhook config normally comes from the DB; here we monkeypatch
the two config readers (get_smtp_config / get_webhook_url) to return
canned values so these tests need no Postgres.
"""
from __future__ import annotations

import smtplib

from worker import notify


def _smtp_cfg(**over):
    cfg = {
        "host": "smtp.test", "port": 587, "username": "",
        "password": "", "from": "alerts@test.com",
        "tls": "starttls",
    }
    cfg.update(over)
    return cfg


# ── send_alert ───────────────────────────────────────────────────────


def test_send_alert_no_config(monkeypatch):
    """Host/from unset → soft sent:False, never raises.
    get_smtp_config is imported inside send_alert; patch the source."""
    monkeypatch.setattr(
        notify, "_read_smtp_config",
        lambda: _smtp_cfg(host="", **{"from": ""}))
    res = notify.send_alert("u@test.com", "subj", "body")
    assert res["sent"] is False
    assert res["reason"] == "SMTP not configured"


def test_send_alert_no_recipient():
    """Empty recipient → soft sent:False (no DB touch)."""
    res = notify.send_alert("", "subj", "body")
    assert res == {"sent": False, "reason": "no recipient"}


def test_send_alert_exception_caught(monkeypatch):
    """An SMTP exception is caught → soft sent:False with the
    exception type in the reason; the reconciler must not break."""
    monkeypatch.setattr(
        notify, "_read_smtp_config", lambda: _smtp_cfg())

    class _Boom(Exception):
        pass

    def _bad(*a, **kw):
        raise _Boom("connect refused")

    monkeypatch.setattr(smtplib, "SMTP", _bad)
    res = notify.send_alert("u@test.com", "subj", "body")
    assert res["sent"] is False
    assert "_Boom" in res["reason"]


class _FakeSMTP:
    """Records the message + which TLS path was taken."""
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        self.quit_called = False
        _FakeSMTP.instances.append(self)

    def starttls(self):
        self.started_tls = True

    def login(self, user, pw):
        self.logged_in = (user, pw)

    def send_message(self, msg):
        self.sent = msg

    def quit(self):
        self.quit_called = True


def test_send_alert_success_starttls(monkeypatch):
    """starttls mode: plain SMTP + starttls() + send_message."""
    monkeypatch.setattr(
        notify, "_read_smtp_config",
        lambda: _smtp_cfg(tls="starttls", username="u",
                          password="p"))
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    res = notify.send_alert("u@test.com", "the subj", "the body")
    assert res == {"sent": True, "to": "u@test.com"}
    inst = _FakeSMTP.instances[-1]
    assert inst.started_tls is True
    assert inst.logged_in == ("u", "p")
    assert inst.sent["To"] == "u@test.com"
    assert inst.sent["From"] == "alerts@test.com"
    assert inst.sent["Subject"] == "the subj"
    assert inst.sent.get_content().strip() == "the body"


def test_send_alert_success_tls(monkeypatch):
    """tls mode: SMTP_SSL, no starttls()."""
    monkeypatch.setattr(
        notify, "_read_smtp_config", lambda: _smtp_cfg(tls="tls"))
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    res = notify.send_alert("u@test.com", "subj", "body")
    assert res == {"sent": True, "to": "u@test.com"}
    inst = _FakeSMTP.instances[-1]
    assert inst.started_tls is False  # SSL, no starttls
    assert inst.sent is not None


# ── send_webhook ──────────────────────────────────────────────────────


def test_send_webhook_no_config(monkeypatch):
    """No URL → soft sent:False."""
    monkeypatch.setattr(notify, "_read_webhook_url", lambda: "")
    res = notify.send_webhook("hello")
    assert res == {"sent": False, "reason": "webhook not configured"}


class _FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self.status


def test_send_webhook_success(monkeypatch):
    """A 200 from the webhook → sent:True; JSON {"text": ...} body."""
    import urllib.request
    monkeypatch.setattr(
        notify, "_read_webhook_url",
        lambda: "https://hook.test/x")
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["method"] = req.get_method()
        return _FakeResp(200)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    res = notify.send_webhook("over cap")
    assert res == {"sent": True}
    assert captured["method"] == "POST"
    import json
    assert json.loads(captured["data"]) == {"text": "over cap"}


def test_send_webhook_error_caught(monkeypatch):
    """A transport exception is caught → soft sent:False."""
    import urllib.request
    monkeypatch.setattr(
        notify, "_read_webhook_url",
        lambda: "https://hook.test/x")

    def _boom(req, timeout=None):
        raise OSError("connection reset")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    res = notify.send_webhook("x")
    assert res["sent"] is False
    assert "OSError" in res["reason"]


def test_send_webhook_non_2xx(monkeypatch):
    """A non-2xx status → soft sent:False with the status."""
    import urllib.request
    monkeypatch.setattr(
        notify, "_read_webhook_url",
        lambda: "https://hook.test/x")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(500))
    res = notify.send_webhook("x")
    assert res["sent"] is False
    assert "500" in res["reason"]


# ── app_url (unchanged behavior) ──────────────────────────────────────


def test_app_url_explicit_wins(monkeypatch):
    monkeypatch.setenv("TG_APP_URL", "https://admin.example.com")
    monkeypatch.setenv(
        "TG_OIDC_REDIRECT_URI",
        "https://other.example.com/auth/callback")
    assert notify.app_url() == "https://admin.example.com"


def test_app_url_derived_from_redirect(monkeypatch):
    monkeypatch.delenv("TG_APP_URL", raising=False)
    monkeypatch.setenv(
        "TG_OIDC_REDIRECT_URI",
        "https://tg.example.com/auth/callback")
    assert notify.app_url() == "https://tg.example.com"


def test_app_url_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("TG_APP_URL", raising=False)
    monkeypatch.delenv("TG_OIDC_REDIRECT_URI", raising=False)
    assert notify.app_url() is None
