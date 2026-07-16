"""
Unit tests for `api.oidc` (#130).

These tests run without a live Okta tenant. They cover:
  - PKCE pair generation produces valid S256 challenge
  - authorize_url builds the right query string
  - id_token verification accepts valid tokens
  - id_token verification rejects bad-issuer / bad-audience /
    expired / tampered tokens
"""
from __future__ import annotations

import json as _json
import time

import pytest

from api import oidc as oidc_mod


pytest.importorskip("authlib")
from authlib.jose import jwt, JsonWebKey  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture
def cfg():
    return oidc_mod.OIDCConfig(
        issuer="https://test.okta.com",
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://tg.example/auth/callback",
        authorization_endpoint=(
            "https://test.okta.com/oauth2/v1/authorize"),
        token_endpoint="https://test.okta.com/oauth2/v1/token",
        jwks_uri="https://test.okta.com/oauth2/v1/keys",
    )


@pytest.fixture
def keypair():
    """Generate a one-shot RSA key for signing test tokens.
    The corresponding JWKS public key is what verify_id_token
    validates against."""
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    private_pem = key.as_pem(is_private=True)
    public_jwk = _json.loads(key.as_json())
    public_jwk.pop("d", None)  # strip private bits
    public_jwk.pop("p", None)
    public_jwk.pop("q", None)
    public_jwk.pop("dp", None)
    public_jwk.pop("dq", None)
    public_jwk.pop("qi", None)
    public_jwk["kid"] = "test-kid"
    public_jwk["use"] = "sig"
    public_jwk["alg"] = "RS256"
    return private_pem, {"keys": [public_jwk]}


def _sign(claims: dict, private_pem: bytes) -> str:
    header = {"alg": "RS256", "kid": "test-kid"}
    return jwt.encode(header, claims, private_pem).decode()


# ── Tests ─────────────────────────────────────────────────────

def test_pkce_pair_is_s256():
    import base64, hashlib
    v, c = oidc_mod.new_pkce_pair()
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert c == expect
    assert len(v) >= 43  # PKCE spec minimum


def test_authorize_url_has_required_params(cfg):
    url = oidc_mod.authorize_url(cfg, "STATE", "CHALLENGE")
    assert url.startswith(cfg.authorization_endpoint + "?")
    for needle in (
        "response_type=code",
        f"client_id={cfg.client_id}",
        "code_challenge=CHALLENGE",
        "code_challenge_method=S256",
        "state=STATE",
        "scope=openid+email+profile",
    ):
        assert needle in url, needle
    # Default — no identity_provider param.
    assert "identity_provider=" not in url


def test_authorize_url_passes_identity_provider(cfg):
    url = oidc_mod.authorize_url(
        cfg, "S", "C", identity_provider="Okta")
    assert "identity_provider=Okta" in url


def test_verify_id_token_accepts_valid(cfg, keypair):
    private, jwks = keypair
    now = int(time.time())
    claims = {
        "iss": cfg.issuer,
        "aud": cfg.client_id,
        "sub": "u1",
        "email": "alice@example.com",
        "iat": now - 5,
        "exp": now + 300,
    }
    token = _sign(claims, private)
    out = oidc_mod.verify_id_token(token, cfg, jwks, now=now)
    assert out["email"] == "alice@example.com"


def test_verify_id_token_rejects_bad_issuer(cfg, keypair):
    private, jwks = keypair
    now = int(time.time())
    bad = {
        "iss": "https://attacker.example",
        "aud": cfg.client_id,
        "sub": "u1", "email": "x@y.com",
        "iat": now, "exp": now + 300,
    }
    token = _sign(bad, private)
    with pytest.raises(oidc_mod.IdTokenError):
        oidc_mod.verify_id_token(token, cfg, jwks, now=now)


def test_verify_id_token_rejects_bad_audience(cfg, keypair):
    private, jwks = keypair
    now = int(time.time())
    bad = {
        "iss": cfg.issuer, "aud": "wrong-client",
        "sub": "u1", "email": "x@y.com",
        "iat": now, "exp": now + 300,
    }
    token = _sign(bad, private)
    with pytest.raises(oidc_mod.IdTokenError):
        oidc_mod.verify_id_token(token, cfg, jwks, now=now)


def test_verify_id_token_rejects_expired(cfg, keypair):
    private, jwks = keypair
    now = int(time.time())
    bad = {
        "iss": cfg.issuer, "aud": cfg.client_id,
        "sub": "u1", "email": "x@y.com",
        "iat": now - 7200, "exp": now - 3600,
    }
    token = _sign(bad, private)
    with pytest.raises(oidc_mod.IdTokenError):
        oidc_mod.verify_id_token(token, cfg, jwks, now=now)


def test_verify_id_token_rejects_tampered_signature(
        cfg, keypair):
    private, jwks = keypair
    now = int(time.time())
    good = {
        "iss": cfg.issuer, "aud": cfg.client_id,
        "sub": "u1", "email": "x@y.com",
        "iat": now, "exp": now + 300,
    }
    token = _sign(good, private)
    parts = token.split(".")
    # Flip a byte in the signature segment.
    parts[2] = parts[2][:-2] + ("AA" if parts[2][-2:]
                                 != "AA" else "BB")
    tampered = ".".join(parts)
    with pytest.raises(oidc_mod.IdTokenError):
        oidc_mod.verify_id_token(tampered, cfg, jwks, now=now)


def test_exchange_code_posts_form(cfg):
    captured = {}
    def fake_post(url, form):
        captured["url"] = url
        captured["form"] = form
        return {"id_token": "x", "access_token": "y"}
    out = oidc_mod.exchange_code(
        cfg, "CODE", "VERIFIER", fake_post)
    assert captured["url"] == cfg.token_endpoint
    assert captured["form"]["grant_type"] == "authorization_code"
    assert captured["form"]["code"] == "CODE"
    assert captured["form"]["code_verifier"] == "VERIFIER"
    assert captured["form"]["client_id"] == cfg.client_id
    assert out["id_token"] == "x"


def test_config_from_env_returns_none_when_unset(monkeypatch):
    for k in ("TG_OIDC_ISSUER", "TG_OIDC_CLIENT_ID",
              "TG_OIDC_CLIENT_SECRET", "TG_OIDC_REDIRECT_URI"):
        monkeypatch.delenv(k, raising=False)
    assert oidc_mod.config_from_env() is None


def test_config_from_env_loads_when_set(monkeypatch):
    monkeypatch.setenv("TG_OIDC_ISSUER", "https://x.okta.com")
    monkeypatch.setenv("TG_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("TG_OIDC_CLIENT_SECRET", "csec")
    monkeypatch.setenv(
        "TG_OIDC_REDIRECT_URI", "https://tg.example/auth/callback")
    cfg = oidc_mod.config_from_env()
    assert cfg is not None
    assert cfg.issuer == "https://x.okta.com"
    assert cfg.client_id == "cid"
