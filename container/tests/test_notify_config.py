"""
Tests for db/notify_config.py — the SMTP + webhook config helpers over
the admin_config kv store. Secrets (SMTP password, webhook URL) must be
ciphertext at rest and decrypt back on read. Uses the shared Postgres
testcontainer (clean_db) + a pinned Fernet key.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from db.session import get_db
from db.models import AdminConfig
from db import crypto
from db import notify_config as nc


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("TG_FERNET_KEY", Fernet.generate_key().decode())
    crypto._reset()
    yield
    crypto._reset()


def test_smtp_round_trip(clean_db, keyed):
    with get_db() as db:
        nc.set_smtp_config(
            db, smtp_host="smtp.relay", smtp_port=465,
            smtp_username="u", smtp_password="s3cret",
            smtp_from="alerts@org.test", smtp_tls="tls")
    with get_db() as db:
        cfg = nc.get_smtp_config(db)
    assert cfg["host"] == "smtp.relay"
    assert cfg["port"] == 465
    assert cfg["username"] == "u"
    assert cfg["password"] == "s3cret"   # decrypts back
    assert cfg["from"] == "alerts@org.test"
    assert cfg["tls"] == "tls"


def test_smtp_password_ciphertext_at_rest(clean_db, keyed):
    with get_db() as db:
        nc.set_smtp_config(
            db, smtp_host="h", smtp_from="f@x",
            smtp_password="plainpw")
    with get_db() as db:
        row = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == nc.SMTP_PASSWORD_KEY)
            .first()
        )
        stored = row.value
    assert stored != "plainpw"
    assert crypto.is_encrypted(stored) is True


def test_smtp_tls_validation(clean_db, keyed):
    with get_db() as db:
        with pytest.raises(ValueError):
            nc.set_smtp_config(db, smtp_tls="bogus")


def test_smtp_port_validation(clean_db, keyed):
    with get_db() as db:
        with pytest.raises(ValueError):
            nc.set_smtp_config(db, smtp_port=99999)
        with pytest.raises(ValueError):
            nc.set_smtp_config(db, smtp_port="not-a-port")


def test_partial_write_keeps_other_keys(clean_db, keyed):
    with get_db() as db:
        nc.set_smtp_config(
            db, smtp_host="h1", smtp_from="f@x", smtp_port=587)
    with get_db() as db:
        nc.set_smtp_config(db, smtp_host="h2")  # only host
    with get_db() as db:
        cfg = nc.get_smtp_config(db)
    assert cfg["host"] == "h2"
    assert cfg["port"] == 587   # untouched
    assert cfg["from"] == "f@x"


def test_webhook_round_trip_and_ciphertext(clean_db, keyed):
    url = "https://hooks.slack.test/services/T/B/xxx"
    with get_db() as db:
        nc.set_webhook_url(db, url)
    with get_db() as db:
        assert nc.get_webhook_url(db) == url
        row = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == nc.WEBHOOK_URL_KEY)
            .first()
        )
        assert row.value != url
        assert crypto.is_encrypted(row.value) is True


def test_configured_booleans(clean_db, keyed):
    with get_db() as db:
        assert nc.smtp_configured(db) is False
        assert nc.webhook_configured(db) is False
    with get_db() as db:
        nc.set_smtp_config(db, smtp_host="h", smtp_from="f@x")
        nc.set_webhook_url(db, "https://hook.test/x")
    with get_db() as db:
        assert nc.smtp_configured(db) is True
        assert nc.webhook_configured(db) is True
