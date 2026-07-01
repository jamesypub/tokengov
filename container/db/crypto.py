"""
Shared symmetric encrypt/decrypt for secrets stored at rest in the
admin_config kv store (SMTP password, alert webhook URL — both bearer
secrets that must not sit in the DB in plaintext).

Uses Fernet (cryptography). The Fernet key is resolved by the same
chain the GitHub integration uses for its PAT secret:

  1. AWS Secrets Manager secret `tg/app/fernet-key` — the SecretString
     IS the Fernet key (a urlsafe-base64 32-byte key). Preferred in a
     real deploy.
  2. env var `TG_FERNET_KEY` — the urlsafe-base64 key directly.
  3. Derived from `DB_PASSWORD` (urlsafe-base64 of its sha256 digest)
     so local dev works with zero setup. This is a CONVENIENCE
     fallback — we log a chatty WARNING so a misconfigured deploy is
     never silently encrypting with a derived key. NEVER a hardcoded
     constant key.

The Fernet instance is cached module-level (resolution is done once).
encrypt/decrypt tolerate the empty string (round-trips to "").
"""
from __future__ import annotations
import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

SM_NAME = "tg/app/fernet-key"

_fernet: Fernet | None = None


def _sm_client():
    try:
        import boto3
        return boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Secrets Manager unavailable: %s", e)
        return None


def _resolve_key() -> bytes:
    """Resolve the Fernet key bytes via the SM → env → derived chain.
    Always returns a valid 32-byte urlsafe-base64 key."""
    # 1. Secrets Manager.
    sm = _sm_client()
    if sm is not None:
        try:
            r = sm.get_secret_value(SecretId=SM_NAME)
            raw = (r.get("SecretString") or "").strip()
            if raw:
                return raw.encode()
        except sm.exceptions.ResourceNotFoundException:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Fernet key SM read failed, falling back: %s", e)

    # 2. Explicit env var.
    env_key = os.environ.get("TG_FERNET_KEY", "").strip()
    if env_key:
        return env_key.encode()

    # 3. Derive from DB_PASSWORD so local dev works without setup.
    # Chatty WARNING: a real deploy should set a managed key, not lean
    # on the derived one (it's tied to the DB password lifecycle).
    db_pw = os.environ.get("DB_PASSWORD", "")
    logger.warning(
        "No Fernet key in Secrets Manager (%s) or TG_FERNET_KEY; "
        "deriving an at-rest key from DB_PASSWORD. Set TG_FERNET_KEY "
        "or the %s secret for a stable, managed key.",
        SM_NAME, SM_NAME)
    digest = hashlib.sha256(db_pw.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _instance() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_resolve_key())
    return _fernet


def _reset() -> None:
    """Drop the cached instance — used by tests that swap the key
    env between cases. Not used in production."""
    global _fernet
    _fernet = None


def encrypt(plaintext: str) -> str:
    """Encrypt a string → a Fernet token (str). Empty in → empty out
    (so an unset secret stays an empty string, not a token)."""
    if not plaintext:
        return ""
    return _instance().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a Fernet token → the plaintext string. Empty in →
    empty out. A value that isn't a valid token (e.g. a plaintext
    value migrated in before encryption shipped) is returned
    unchanged so reads stay non-fatal."""
    if not token:
        return ""
    try:
        return _instance().decrypt(token.encode()).decode()
    except InvalidToken:
        # Not a token we wrote — most likely a legacy plaintext value.
        return token


def is_encrypted(token: str) -> bool:
    """True when `token` looks like a Fernet token we can decrypt
    (so a plaintext-migrated value reads as False)."""
    if not token:
        return False
    try:
        _instance().decrypt(token.encode())
        return True
    except InvalidToken:
        return False
    except Exception:  # noqa: BLE001
        return False
