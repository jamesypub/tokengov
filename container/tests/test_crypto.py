"""
Tests for db/crypto.py — the Fernet encrypt/decrypt helper. No DB or
AWS needed: we pin a key via TG_FERNET_KEY and reset the module's
cached Fernet instance between cases.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from db import crypto


@pytest.fixture
def keyed(monkeypatch):
    """Pin a valid Fernet key + drop the cached instance so each test
    starts clean. (SM is unreachable in CI → the chain falls through
    to TG_FERNET_KEY.)"""
    monkeypatch.setenv("TG_FERNET_KEY", Fernet.generate_key().decode())
    crypto._reset()
    yield
    crypto._reset()


def test_round_trip(keyed):
    token = crypto.encrypt("hunter2")
    assert token != "hunter2"  # ciphertext, not plaintext
    assert crypto.decrypt(token) == "hunter2"


def test_empty_string(keyed):
    assert crypto.encrypt("") == ""
    assert crypto.decrypt("") == ""


def test_is_encrypted(keyed):
    token = crypto.encrypt("secret")
    assert crypto.is_encrypted(token) is True
    assert crypto.is_encrypted("just-plaintext") is False
    assert crypto.is_encrypted("") is False


def test_decrypt_plaintext_is_graceful(keyed):
    """A non-token (e.g. a value migrated in before encryption
    shipped) decrypts to itself rather than raising."""
    assert crypto.decrypt("legacy-plaintext-url") == \
        "legacy-plaintext-url"


def test_derived_key_fallback(monkeypatch):
    """No SM + no TG_FERNET_KEY → derive a stable key from
    DB_PASSWORD so local dev round-trips without setup."""
    monkeypatch.delenv("TG_FERNET_KEY", raising=False)
    monkeypatch.setenv("DB_PASSWORD", "local-dev-pw")
    crypto._reset()
    try:
        token = crypto.encrypt("abc")
        assert crypto.decrypt(token) == "abc"
    finally:
        crypto._reset()
