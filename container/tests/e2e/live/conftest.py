"""Live-tier fixtures: talk to a REAL running stack + real AWS.

This tier is the highest-fidelity coverage — it drives the deployed
app over HTTP and, for the enforcement cases, assumes a real IAM role
and calls Bedrock for real. Everything is env-configured; NOTHING that
identifies a customer (account id, principal ARN, model id) is
hardcoded as the only value. The whole tier SKIPS cleanly and
credential-free when E2E_API_BASE is unset or the stack is
unreachable, so `pytest -m "not live"` and a bare `pytest -m live`
both stay green with zero AWS.

Why a separate conftest (not the shared one): keeping the live-only
machinery here means the in-process `e2e` tier's conftest changes
stay to a single clarifying comment — the shared fixtures are
byte-for-byte behaviorally unchanged. These fixtures depend on real
infra, so co-locating them with the tier they serve is the smaller,
clearer change.

Login-mode (E2E_LOGIN_MODE):
  auto        — probe /api/version + /auth/providers + whether the
                test-trust bypass is honored, then pick the strategy.
  creds       — fully unattended: seeded Cognito password login.
  saml-human  — the ONLY interactive mode: pause for a human to drive
                the SAML redirect, then read back the session cookie.

The prod-guard is an explicit opt-in here (unlike the always-on e2e
guard): refuse a prod-looking API_BASE or a prod account unless
E2E_ALLOW_PROD=1 is set deliberately.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.live


# ── env config (no hardcoded customer values as the only value) ──────

def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if (v is not None and v != "") else default


# Non-prod default model — overridable. Mirrors
# test_deny_enforcement_live.py's os.environ.get(..., <default>) shape:
# a Haiku CRIS id is a sensible test default, never a customer-only pin.
DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

API_BASE = _env("E2E_API_BASE")
AWS_PROFILE = _env("E2E_AWS_PROFILE")
TEST_PRINCIPAL_ARN = _env("E2E_TEST_PRINCIPAL_ARN")
TEST_MODEL_ID = _env("E2E_TEST_MODEL_ID", DEFAULT_MODEL_ID)
LOGIN_MODE = (_env("E2E_LOGIN_MODE", "auto") or "auto").lower()
REGION = _env("AWS_REGION", "us-east-1")
CONSUMER_ROLE = _env("E2E_CONSUMER_ROLE_NAME", "tg-consumer")
DENY_POLICY = _env("E2E_DENY_POLICY_NAME", "tg-BedrockQuotaDeny")

# Reserved test session/principal — NEVER a real seeded user. The
# session name becomes the aws:userid suffix a per-person deny matches;
# using a reserved one means a failed revert can never strand a human.
RESERVED_SESSION = _env(
    "E2E_RESERVED_SESSION", "tg-e2e-live@example.com")
# The governable principal the live seed onboards + the tests drive.
# Reserved @example.com so a workflow case never mutates a real person.
SEED_EMAIL = _env("E2E_SEED_EMAIL", "tg-e2e-seed@example.com")

# Known non-prod accounts (dev/stage) — used by the account prod-guard.
# Overridable via E2E_TARGET_ACCOUNT_ID for a fresh test account.
_KNOWN_NONPROD_ACCOUNTS = {
    a for a in (
        _env("E2E_TARGET_ACCOUNT_ID"),
        "123456789012",  # stage
        "123456789012",  # dev
        "123456789012",  # demo2
        "123456789012",  # demo0
    ) if a
}

_TIMEOUT = int(_env("E2E_HTTP_TIMEOUT", "10") or "10")

# Self-signed HTTPS handling — the stage ALB serves a self-signed cert
# (testing.md), so a live run against it must skip cert verification or
# every request fails on the chain and the tier falsely reports the
# stack 'unreachable' + skips. Honor the established TG_TLS_INSECURE=1
# flag (same knob test_browser.py / the docs use). Gated explicitly: a
# real ACM cert must still verify cleanly, so this is opt-in, never
# auto-on for any https:// URL.
_TLS_INSECURE = os.environ.get("TG_TLS_INSECURE") == "1"


def _ssl_ctx():
    """An unverified SSL context when TG_TLS_INSECURE=1, else None (the
    urllib default, which verifies). Only applied to https:// URLs."""
    if not _TLS_INSECURE:
        return None
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ── stack reachability + prod-guard ─────────────────────────────────

def _http(method: str, path: str, headers=None, data=None):
    """Minimal urllib request against the live API. Returns
    (status, body_text). Raises on transport error so callers can
    translate to a clean skip. Honors TG_TLS_INSECURE=1 for a
    self-signed HTTPS target (else default verification applies)."""
    url = f"{API_BASE.rstrip('/')}{path}"
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.data = data
    # Only pass a context for https:// (urlopen rejects context= on
    # http:// URLs); TG_TLS_INSECURE is a no-op on plain http.
    ctx = _ssl_ctx() if url.lower().startswith("https:") else None
    try:
        with urllib.request.urlopen(
                req, timeout=_TIMEOUT, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _stack_up() -> bool:
    if not API_BASE:
        return False
    try:
        status, _ = _http("GET", "/api/version")
        return status == 200
    except Exception:
        return False


def _assert_not_prod() -> None:
    """Opt-in prod-guard (rail): refuse a prod-looking API_BASE unless
    E2E_ALLOW_PROD=1 is set on purpose. The account-level guard runs
    once creds exist (see aws_session)."""
    if os.environ.get("E2E_ALLOW_PROD") == "1":
        return
    if API_BASE and "prod" in API_BASE.lower():
        pytest.skip(
            "refusing to run the live tier against a prod-looking "
            f"E2E_API_BASE ({API_BASE!r}) — set E2E_ALLOW_PROD=1 to "
            "override deliberately.")


@pytest.fixture(scope="session")
def live_base() -> str:
    """The reachable live API base, or a clean skip. This is the tier
    gate: every live test depends on it (directly or transitively), so
    an unset/unreachable stack skips the whole tier credential-free."""
    if not API_BASE:
        pytest.skip(
            "live tier is opt-in: set E2E_API_BASE to a running stack "
            "(e.g. http://localhost:18000) to run -m live.")
    _assert_not_prod()
    if not _stack_up():
        pytest.skip(f"no live stack reachable at {API_BASE}")
    return API_BASE


# ── boto3 / AWS session (credential-free skip when absent) ───────────

@pytest.fixture(scope="session")
def boto3_mod():
    """boto3 is optional infra for this tier — importorskip so a bare
    `pytest -m live` with no boto3 installed skips instead of erroring."""
    return pytest.importorskip("boto3")


@pytest.fixture(scope="session")
def aws_session(boto3_mod):
    """A boto3 Session for the test principal, honoring E2E_AWS_PROFILE
    (falls back to the ambient cred chain / AWS_PROFILE, #116). Skips
    cleanly if no usable credentials resolve, and enforces the account
    prod-guard once the caller identity is known."""
    boto3 = boto3_mod
    kwargs = {"region_name": REGION}
    if AWS_PROFILE:
        kwargs["profile_name"] = AWS_PROFILE
    try:
        session = boto3.session.Session(**kwargs)
        acct = session.client("sts").get_caller_identity()["Account"]
    except Exception as e:  # noqa: BLE001 — any cred/network failure → skip
        pytest.skip(
            f"no usable AWS credentials for the live tier ({e}); set "
            "E2E_AWS_PROFILE or the ambient cred chain to run.")
    if (os.environ.get("E2E_ALLOW_PROD") != "1"
            and _KNOWN_NONPROD_ACCOUNTS
            and acct not in _KNOWN_NONPROD_ACCOUNTS):
        pytest.skip(
            f"refusing to run the live tier against account {acct} "
            f"(not a known dev/stage account {_KNOWN_NONPROD_ACCOUNTS}) "
            "— set E2E_ALLOW_PROD=1 or E2E_TARGET_ACCOUNT_ID to override.")
    session._tg_account_id = acct  # stash for helpers
    return session


@pytest.fixture(scope="session")
def account_id(aws_session) -> str:
    return aws_session._tg_account_id


# ── assume-role helper for the RESERVED test principal ──────────────

@pytest.fixture
def assume_bedrock(aws_session, boto3_mod):
    """Factory: assume the consumer role under the RESERVED session name
    and return a bedrock-runtime client. Mirrors
    test_deny_enforcement_live.py's _assume — the session name becomes
    the aws:userid suffix a per-person deny matches, so a reserved one
    keeps a failed revert from ever stranding a real user."""
    boto3 = boto3_mod
    acct = aws_session._tg_account_id
    role_arn = _env(
        "E2E_CONSUMER_ROLE_ARN",
        f"arn:aws:iam::{acct}:role/{CONSUMER_ROLE}")

    def _assume(session_name: str = RESERVED_SESSION):
        creds = aws_session.client("sts").assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            DurationSeconds=900,
        )["Credentials"]
        return boto3.client(
            "bedrock-runtime", region_name=REGION,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )

    return _assume


# ── login-strategy: auto / creds / saml-human ───────────────────────

def _providers(base: str) -> dict:
    try:
        status, body = _http("GET", "/auth/providers")
        if status == 200:
            import json
            return json.loads(body)
    except Exception:
        pass
    return {}


def _test_trust_honored(base: str) -> bool:
    """Probe whether the stack honors the test-trust bypass.

    Probe /api/whoami (NOT an admin-scoped endpoint) with the bypass
    headers and treat ANY non-401 as honored — the same signal the
    Playwright driver uses (tg-e2e-run.py: 401 == test-trust OFF ==
    prod, anything else == the bypass resolved the identity == a
    dev/stage stack). Earlier this probed /api/teams and required a
    strict 200; but /api/teams is require_org_admin, so a probe email
    that isn't the org admin gets 403 on a real test-trust stage — the
    probe then falsely reports 'not honored' and auto-detect drops to
    saml-human, skipping the workflow tests unattended. whoami depends
    only on get_caller_auth (any authenticated caller → 200), so it
    isolates 'is the bypass accepted' from 'is this persona an admin'.
    """
    headers = {
        "Authorization": "AWS4-HMAC-SHA256 testbypass",
        "X-Tg-Test-Email": os.environ.get(
            "E2E_ADMIN_EMAIL", "tg-org-admin@example.com"),
    }
    try:
        status, _ = _http("GET", "/api/whoami", headers=headers)
        # 401 = bypass rejected (test-trust OFF / prod). Any other
        # status means the bypass resolved a caller → test-trust on.
        return status != 401
    except Exception:
        return False


def _bypass_headers(email: str) -> dict:
    return {
        "Authorization": "AWS4-HMAC-SHA256 testbypass",
        "X-Tg-Test-Email": email,
    }


def _is_org_admin(email: str) -> bool:
    """True if `email` resolves to org_admin via the test-trust whoami
    bypass (whoami returns {"org_admin": bool})."""
    try:
        status, body = _http(
            "GET", "/api/whoami", headers=_bypass_headers(email))
        if status != 200:
            return False
        import json
        return bool(json.loads(body).get("org_admin"))
    except Exception:
        return False


def _discover_org_admin(default_email: str) -> str | None:
    """Resolve a real org_admin email on a test-trust target.

    The workflow tests drive org_admin-scoped endpoints (POST
    /api/jobs/run, PUT /api/settings/blocked-models), so the test-trust
    persona MUST be an org_admin — otherwise they 403. The default
    (tg-org-admin@example.com) is a plain member on some seeds (e.g.
    stage), so don't assume it: (1) honor an explicit E2E_ADMIN_EMAIL,
    (2) else if the default IS org_admin here, use it, (3) else DISCOVER
    one from /api/dev/personas (test-trust-only; lists admin emails +
    roles). Returns None if no org_admin is reachable → the caller
    skips with a clear reason rather than 403-failing."""
    # (1) explicit override wins — trust the operator.
    explicit = os.environ.get("E2E_ADMIN_EMAIL")
    if explicit:
        return explicit
    # (2) the default might already be org_admin on this seed.
    if _is_org_admin(default_email):
        return default_email
    # (3) discover from the dev-personas endpoint (test-trust-gated).
    try:
        status, body = _http(
            "GET", "/api/dev/personas",
            headers=_bypass_headers(default_email))
        if status == 200:
            import json
            for p in json.loads(body).get("personas", []):
                if p.get("role") == "org_admin" and p.get("email"):
                    return p["email"]
    except Exception:
        pass
    return None


@pytest.fixture(scope="session")
def login_strategy(live_base):
    """Resolve the login strategy for this stack.

    Returns a dict {mode, headers, email}. `headers` is what an
    authenticated request must carry:
      - test-trust: the bypass Authorization + X-Tg-Test-Email.
      - creds:      a Cognito session cookie obtained unattended.
      - saml-human: a session cookie captured after a human pause.

    E2E_LOGIN_MODE forces a mode; `auto` (default) probes the stack.
    Only `saml-human` is allowed to pause — `creds` runs unattended.

    For the test-trust path the persona is resolved to a REAL org_admin
    on the target (the workflow tests need admin scope), so a default
    that isn't org_admin on the seed doesn't 403 the whole suite.
    """
    base = live_base
    default_admin = os.environ.get(
        "E2E_ADMIN_EMAIL", "tg-org-admin@example.com")
    mode = LOGIN_MODE

    if mode == "auto":
        # Cheapest, most reliable signal first: does the stack honor
        # test-trust? If so, use it (no creds, deterministic). Else
        # inspect which IdP is wired and pick creds vs saml-human.
        if _test_trust_honored(base):
            mode = "test-trust"
        else:
            prov = _providers(base)
            if prov.get("cognito"):
                mode = "creds"
            elif prov.get("okta"):
                mode = "saml-human"
            else:
                mode = "creds"

    if mode in ("test-trust", "auto-test-trust"):
        # Resolve a real org_admin so the workflow tests (which hit
        # org_admin-scoped endpoints) don't 403 on a default that is a
        # plain member on this seed.
        admin_email = _discover_org_admin(default_admin)
        if not admin_email:
            pytest.skip(
                "test-trust target has no reachable org_admin persona: "
                f"{default_admin!r} is not org_admin here and none was "
                "found via /api/dev/personas. Set E2E_ADMIN_EMAIL to an "
                "org admin on the target to run the workflow suite.")
        return {
            "mode": "test-trust",
            "email": admin_email,
            "headers": _bypass_headers(admin_email),
        }

    if mode == "creds":
        # Fully unattended: seeded Cognito login. We don't hardcode the
        # Cognito flow here — a stack reachable for `creds` mode is
        # expected to also honor test-trust (the seeded-admin path), so
        # fall back to it. If neither works the dependent tests skip.
        if _test_trust_honored(base):
            admin_email = _discover_org_admin(default_admin)
            if not admin_email:
                pytest.skip(
                    "creds fallback to test-trust found no org_admin "
                    "persona on the target; set E2E_ADMIN_EMAIL.")
            return {
                "mode": "test-trust",
                "email": admin_email,
                "headers": _bypass_headers(admin_email),
            }
        pytest.skip(
            "E2E_LOGIN_MODE=creds needs an unattended admin session; "
            "the stack does not honor the seeded test-trust path. Point "
            "at a test-trust stack or use saml-human.")

    if mode == "saml-human":
        # The ONLY interactive mode. Without an operator present (no
        # E2E_SESSION_COOKIE handed in), skip rather than block CI.
        cookie = os.environ.get("E2E_SESSION_COOKIE")
        if not cookie:
            pytest.skip(
                "E2E_LOGIN_MODE=saml-human needs a human-driven session: "
                "log in via the browser and pass the session cookie as "
                "E2E_SESSION_COOKIE, then re-run.")
        return {
            "mode": "saml-human",
            "email": default_admin,
            "headers": {"Cookie": cookie},
        }

    pytest.skip(f"unknown E2E_LOGIN_MODE={mode!r}")


# ── live HTTP client (authenticated per the resolved strategy) ──────

class LiveClient:
    """Authenticated HTTP client over the REAL API — the live-tier
    analog of the in-process PersonaClient. Carries whatever the
    resolved login strategy dictates (test-trust headers / session
    cookie). Returns a small Response with .status / .json()."""

    def __init__(self, base: str, headers: dict):
        # base is unused directly: _http() targets the module API_BASE
        # (validated by live_base). Kept for a clear call signature.
        self._base = base
        self._h = dict(headers)

    def _do(self, method: str, path: str, json_body=None):
        import json as _json
        headers = dict(self._h)
        data = None
        if json_body is not None:
            data = _json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        status, body = _http(method, path, headers=headers, data=data)

        class _Resp:
            def __init__(self, status, text):
                self.status = status
                self._text = text

            def json(self):
                return _json.loads(self._text) if self._text else {}

            @property
            def text(self):
                return self._text

            def raise_for_status(self):
                if self.status >= 400:
                    raise AssertionError(
                        f"{method} {path} -> {self.status}: "
                        f"{self._text[:400]}")
                return self

        # _http prepends base already via API_BASE; call it with a
        # base-relative path.
        return _Resp(status, body)

    def get(self, path):
        return self._do("GET", path)

    def post(self, path, json_body=None):
        return self._do("POST", path, json_body)

    def put(self, path, json_body=None):
        return self._do("PUT", path, json_body)

    def delete(self, path, json_body=None):
        return self._do("DELETE", path, json_body)


@pytest.fixture
def live_client(login_strategy):
    """An authenticated LiveClient bound to the resolved login
    strategy (default persona = the org admin)."""
    return LiveClient(API_BASE, login_strategy["headers"])
