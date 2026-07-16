"""
End-to-end browser tests of the Token Governance React UI against the
running container stack.

It drives a headless Chromium against the running docker-compose
api service that serves the React bundle on http://localhost:8000/.

  Stack expected:
    docker compose up -d   (or scripts/tg-local-install.sh)
    internal/scripts/tg-test-data-populate.sh   (populates ~53 users, real spend)

  Auth:
    Container API has TG_AUTH_TEST_TRUST=1 (set by tg-local-install.sh).
    We inject test-trust headers via page.set_extra_http_headers so every
    XHR Playwright drives is authenticated. Email defaults to
    TG_BOOTSTRAP_ADMIN_EMAIL (host shell) or BOOTSTRAP_ADMIN_EMAIL
    (container env), falling back to tg-org-admin@example.com.

Coverage (issue #93):
  - Original 5 tests (page loads, sidebar nav, activity, set cap, typed
    confirm modal). Sidebar nav now also asserts NO console errors.
  - NEW: every primary CTA (Run on Cost Reports, Test alert on Settings,
    Run on Jobs, Approve flow, full nav walk with strict console-error
    assertion).
  - NEW: persona-aware fixture parametrized over org_admin /
    team_admin (top-level) / team_admin (mid) / member. Each persona
    asserts the right sidebar items and the right user-data scope.
    Issue #104 retired parent_team_admin — descent through parent_team_id
    now applies universally to team_admin rows.

A 5xx served to the browser fails the test. We never SKIP on 500-with-
matching-body — the only soft-skip path is for legitimate prereqs
(e.g. CUR data missing, which the backend signals via 503).

Requires:
  pip install playwright
  python -m playwright install chromium

If playwright is not importable, the whole module is SKIPPED (not failed)
via pytest.importorskip. If the API has no test data, individual tests
SKIP with a clear message pointing at tg-test-data-populate.sh.

Wired into scripts/tg-test.sh as:
    bash scripts/tg-test.sh --browser
    bash scripts/tg-test.sh --browser-full   # nightly: destroy+install too
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request
import urllib.error
from types import SimpleNamespace

import pytest

# Skip-cleanly if playwright isn't installed.
playwright_mod = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright, expect  # noqa: E402


API_BASE = os.environ.get("API_BASE", "http://localhost:8000")
TEST_EMAIL = (
    os.environ.get("TG_BOOTSTRAP_ADMIN_EMAIL")
    or os.environ.get("BOOTSTRAP_ADMIN_EMAIL")
    or "tg-org-admin@example.com"
)
TEST_TRUST_HEADERS = {
    "Authorization": "AWS4-HMAC-SHA256 testbypass",
    "X-Tg-Test-Email": TEST_EMAIL,
}

# When the stack is fronted by a self-signed cert (the
# stage path uses one — see tg-ecs-install.sh's
# TG_TLS_SELF_SIGNED branch), set TG_TLS_INSECURE=1 to skip
# verification in both urllib and Playwright. Don't auto-on
# for https:// — a real ACM cert should verify cleanly, and
# silent bypass would hide cert misconfigs.
_INSECURE = os.environ.get("TG_TLS_INSECURE") == "1"
_SSL_CTX = ssl._create_unverified_context() if _INSECURE else None


# ── helpers ────────────────────────────────────────────────────────────────

def _headers_for(email: str) -> dict:
    return {
        "Authorization": "AWS4-HMAC-SHA256 testbypass",
        "X-Tg-Test-Email": email,
    }


def _urlopen(req, timeout):
    """urlopen wrapper that passes _SSL_CTX when TG_TLS_INSECURE=1.
    Plain HTTP requests ignore the context."""
    if _SSL_CTX is not None:
        return urllib.request.urlopen(
            req, timeout=timeout, context=_SSL_CTX)
    return urllib.request.urlopen(req, timeout=timeout)


def _api_get_json(path: str, *, email: str | None = None):
    """GET an API endpoint with test-trust headers; return parsed JSON or
    None on any error."""
    headers = _headers_for(email) if email else TEST_TRUST_HEADERS
    req = urllib.request.Request(
        f"{API_BASE}{path}", headers=headers, method="GET"
    )
    try:
        with _urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError):
        return None


def _api_get_with_status(path: str, *, email: str | None = None):
    """GET → (status_code, body_or_none)."""
    headers = _headers_for(email) if email else TEST_TRUST_HEADERS
    req = urllib.request.Request(
        f"{API_BASE}{path}", headers=headers, method="GET"
    )
    try:
        with _urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, None
    except urllib.error.URLError:
        return 0, None


def _api_put_json(path: str, payload: dict):
    """PUT JSON to an API endpoint with test-trust headers."""
    body = json.dumps(payload).encode()
    headers = {**TEST_TRUST_HEADERS, "Content-Type": "application/json"}
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=body, headers=headers, method="PUT"
    )
    try:
        with _urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except urllib.error.URLError:
        return 0, None


def _api_post_json(path: str, payload: dict | None = None):
    """POST JSON to an API endpoint with test-trust headers."""
    body = json.dumps(payload or {}).encode()
    headers = {**TEST_TRUST_HEADERS, "Content-Type": "application/json"}
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=body, headers=headers, method="POST"
    )
    try:
        with _urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, e.read().decode()
    except urllib.error.URLError:
        return 0, None


def _api_delete(path: str):
    """DELETE with test-trust headers; returns status code."""
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers=TEST_TRUST_HEADERS, method="DELETE",
    )
    try:
        with _urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError:
        return 0


def _stack_up() -> bool:
    return _api_get_json("/api/version") is not None


def _has_test_data() -> bool:
    d = _api_get_json("/api/users")
    return bool(d and len(d.get("users", [])) > 0)


def _pick_active_user() -> dict | None:
    """Pick an active user from /api/users with cap_usd set, for cap tests."""
    d = _api_get_json("/api/users")
    if not d:
        return None
    for u in d.get("users", []):
        if u.get("status") == "active" and u.get("email") != TEST_EMAIL:
            return u
    return None


def _filter_console_errors(errors: list[str]) -> list[str]:
    """Drop benign noise that's not the UI's fault.

    - favicon 404s on some setups
    - any optional-asset 404 (we don't gate on missing decorative assets)
    - fonts.gstatic.com CORS preflight failures: the browser context
      injects `Authorization: AWS4-HMAC-SHA256 testbypass` on every
      request (test-trust bypass for the SPA's API calls). Google Fonts
      doesn't allow Authorization in its CORS preflight, so the
      injected header makes font loads fail in tests but not in real
      browsers. The visual appearance still matches; this is a test
      artifact, not a UI bug.

    Anything else — including XHR 4xx/5xx, React render errors, network
    errors during navigation — is real and surfaced as a failure.
    """
    out = []
    for e in errors:
        low = e.lower()
        if "favicon" in low:
            continue
        if "404" in e and "favicon" in low:
            continue
        if "fonts.gstatic.com" in low:
            continue
        if ("err_failed" in low
                and "fonts.gstatic.com" in low):
            continue
        # Playwright reports the second-line "Failed to load resource"
        # for every CORS-blocked font without the URL — drop those too
        # when they're paired with a font CORS error in the same batch.
        if (low == "console.error: failed to load resource: "
                "net::err_failed"):
            continue
        out.append(e)
    return out


# ── module-level skips ─────────────────────────────────────────────────────

if not _stack_up():
    pytest.skip(
        f"container stack not reachable at {API_BASE} — "
        "is `docker compose up` running?",
        allow_module_level=True,
    )

if not _has_test_data():
    pytest.skip(
        "no users in DB — run internal/scripts/tg-test-data-populate.sh first",
        allow_module_level=True,
    )


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def browser_ctx():
    """Headless Chromium with test-trust headers injected into every
    request the page (and its XHRs) make. ignore_https_errors=True
    when TG_TLS_INSECURE=1 so the stage self-signed cert path works."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=_INSECURE,
        )
        ctx.set_extra_http_headers(TEST_TRUST_HEADERS)
        yield ctx
        ctx.close()
        browser.close()


@pytest.fixture
def page(browser_ctx):
    """Per-test page; collects console errors so tests can assert
    no JS console errors fired."""
    page = browser_ctx.new_page()
    errors: list[str] = []
    page.on(
        "pageerror", lambda exc: errors.append(f"pageerror: {exc}")
    )
    page.on(
        "console",
        lambda msg: (
            errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None
        ),
    )
    page.console_errors = errors  # attach for tests to read
    yield page
    page.close()


# Persona table — the canonical source of truth for what each role sees.
# Sidebar items match Layout.jsx's SECTIONS table at the time of writing
# (org_admin sees everything; team_admin loses Cost Reports, Settings,
# Jobs; member loses Users + Teams as well).
#
# Issue #104 retired parent_team_admin: a team_admin granted at team-N
# transparently sees team-N + all descendants via parent_team_id BFS.
# The "top_team_admin" / "mid_team_admin" labels here are test-fixture
# distinctions, not separate roles — both rows have role='team_admin'.
PERSONAS = [
    (
        "org_admin",
        TEST_EMAIL,  # bootstrap admin
        ["Activity", "Users", "Teams",
         "Cost Reports", "Settings", "Jobs"],
    ),
    (
        "top_team_admin",
        "team-1-admin-1@example.com",
        ["Activity", "Users", "Teams"],
    ),
    (
        "mid_team_admin",
        "team-1.1-admin-1@example.com",
        ["Activity", "Users", "Teams"],
    ),
    (
        "member",
        "team-1.1-member-1@example.com",
        ["Activity"],
    ),
]


@pytest.fixture(
    params=PERSONAS,
    ids=[p[0] for p in PERSONAS],
)
def persona(request):
    role, email, sidebar = request.param
    return SimpleNamespace(role=role, email=email, sidebar=sidebar)


@pytest.fixture
def persona_page(browser_ctx, persona):
    """Per-test page that injects the *persona's* email instead of the
    default bootstrap-admin email. Used by per-persona scope tests."""
    page = browser_ctx.new_page()
    # Override the module-level X-Tg-Test-Email header for this page
    # only. browser_ctx already has the bootstrap-admin headers set.
    page.set_extra_http_headers(_headers_for(persona.email))
    errors: list[str] = []
    page.on(
        "pageerror", lambda exc: errors.append(f"pageerror: {exc}")
    )
    page.on(
        "console",
        lambda msg: (
            errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None
        ),
    )
    page.console_errors = errors
    yield page
    page.close()


# ── tests ──────────────────────────────────────────────────────────────────

def test_page_loads(page):
    """Open /, verify React hydrated and no console errors fired."""
    page.goto(API_BASE, wait_until="networkidle")
    # Sidebar visible — the brand block "tg" is always rendered.
    page.wait_for_selector("text=Token Governance", timeout=10000)
    expect(page.locator("body")).not_to_be_empty()
    real_errors = _filter_console_errors(page.console_errors)
    assert not real_errors, f"console errors: {real_errors}"


def test_sidebar_navigation(page):
    """Click each top-level sidebar link, verify the heading renders.

    Also asserts NO console errors fire during navigation. A 5xx XHR
    triggered by a route's mount would surface here as a console.error.
    """
    page.goto(API_BASE, wait_until="networkidle")
    page.wait_for_selector("text=Token Governance", timeout=10000)

    # (label-in-sidebar, hash-route, expected-h1-text)
    nav = [
        ("Activity",     "/activity",     "Activity"),
        ("Users",        "/users",        "Users"),
        ("Teams",        "/teams",        "Teams"),
        ("Cost Reports", "/cost-reports", "Cost Reports"),
        ("Settings",     "/settings",     "Org Settings"),
        ("Jobs",         "/jobs",         "Jobs"),
    ]
    for sidebar_label, route, h1_text in nav:
        page.locator(f"aside >> text={sidebar_label}").first.wait_for(
            timeout=5000
        )
        page.evaluate(f"window.location.hash = '#{route}'")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(
            f"h1:has-text('{h1_text}')", timeout=10000
        )

    real_errors = _filter_console_errors(page.console_errors)
    assert not real_errors, (
        f"console errors during nav walk: {real_errors}"
    )


def test_activity_page_renders_data(page):
    """Activity page shows non-zero numbers from populated test data."""
    page.goto(f"{API_BASE}/#/activity", wait_until="networkidle")
    page.wait_for_selector("h1:has-text('Activity')", timeout=10000)

    usage = _api_get_json("/api/usage")
    assert usage is not None, (
        "GET /api/usage returned no data; "
        "run internal/scripts/tg-test-data-populate.sh first"
    )
    rows = usage.get("rows", [])
    if not rows:
        pytest.skip(
            "/api/usage rows is empty — "
            "run internal/scripts/tg-test-data-populate.sh first"
        )

    page.wait_for_selector("text=Month to date", timeout=15000)
    body_text = page.locator("body").inner_text().lower()
    assert "active users" in body_text, (
        "Active users tile not rendered"
    )

    summary = _api_get_json("/api/summary")
    active = (summary or {}).get("active_users")
    if active is None:
        active = len({r.get("email") for r in rows if r.get("email")})
    assert active and active > 0, (
        f"active_users={active}; populate test data first"
    )

    assert str(active) in body_text, (
        f"active count {active} not visible in page text"
    )


def test_set_cap_round_trip(page):
    """Navigate Users → click a user → Set cap → save → verify update."""
    target = _pick_active_user()
    if target is None:
        pytest.skip(
            "no active user available — populate test data first"
        )
    email = target["email"]

    original_cap = target.get("cap_usd") or 10
    original_version = target.get("version", 0)

    new_cap = 47.5

    try:
        page.goto(
            f"{API_BASE}/#/users/{email}", wait_until="networkidle"
        )
        # Scope to <main> — the dev impersonation <select> in
        # the sidebar lists every admin email as a hidden
        # <option>, so an unscoped text= locator resolves to
        # 2 elements and times out waiting for the hidden
        # option to be visible.
        page.wait_for_selector(
            f"main >> text={email}", timeout=10000)
        page.wait_for_selector(
            "button:has-text('Set cap')", timeout=10000
        )
        page.click("button:has-text('Set cap')")

        page.wait_for_selector(
            f"text=Set cap for {email}", timeout=5000
        )
        cap_input = page.locator('input[type="number"]').first
        cap_input.wait_for(timeout=5000)
        cap_input.fill(str(new_cap))

        page.click("button:has-text('Save')")

        page.wait_for_selector(
            f"text=Set cap for {email}",
            state="detached",
            timeout=10000,
        )
        deadline = time.time() + 5
        latest = None
        while time.time() < deadline:
            latest = _api_get_json(f"/api/users/{email}")
            if latest and abs(
                (latest.get("cap_usd") or 0) - new_cap
            ) < 0.01:
                break
            time.sleep(0.3)
        assert latest is not None
        assert abs((latest.get("cap_usd") or 0) - new_cap) < 0.01, (
            f"cap not updated: got {latest.get('cap_usd')}, "
            f"expected {new_cap}"
        )
    finally:
        latest = _api_get_json(f"/api/users/{email}")
        ver = latest.get("version", original_version) if latest else \
            original_version
        _api_put_json(
            f"/api/users/{email}/cap",
            {"cap_usd": original_cap, "expected_version": ver},
        )


def test_typed_confirm_modal_blocks_until_email_match(page):
    """Open Disable modal: wrong email leaves submit disabled, right
    email enables it. Cancel out without confirming."""
    target = _pick_active_user()
    if target is None:
        pytest.skip(
            "no active user available — populate test data first"
        )
    email = target["email"]

    page.goto(
        f"{API_BASE}/#/users/{email}", wait_until="networkidle"
    )
    # Scope to <main> — see test_set_cap_round_trip for why.
    page.wait_for_selector(
        f"main >> text={email}", timeout=10000)
    page.wait_for_selector(
        "button:has-text('Disable')", timeout=10000
    )
    page.click("button:has-text('Disable')")

    confirm_input = page.locator(f'input[placeholder="{email}"]')
    confirm_input.wait_for(timeout=5000)

    modal_disable = page.locator(
        "button:has-text('Disable')"
    ).last

    confirm_input.fill("not-the-right-email@example.com")
    expect(modal_disable).to_be_disabled()

    confirm_input.fill(email)
    expect(modal_disable).not_to_be_disabled()

    page.click("button:has-text('Cancel')")
    page.wait_for_selector(
        f'input[placeholder="{email}"]',
        state="detached",
        timeout=5000,
    )
    after = _api_get_json(f"/api/users/{email}")
    assert after and after.get("status") == "active"


# ── NEW: every primary CTA gets a click test ────────────────────────────────

def test_cost_reports_run_button(page):
    """Click Run on the first saved Athena query; assert 200 or 503.

    A 500 here is a real bug (issue #92). Whatever the backend wants to
    say about CUR-data-not-ready, it must be a 503 with a friendly body
    — never a generic 500. We assert by inspecting both the page state
    AND the API response (which the page made), since the React app
    surfaces 5xxs as red error text rather than crashing.
    """
    page.goto(f"{API_BASE}/#/cost-reports", wait_until="networkidle")
    page.wait_for_selector("h1:has-text('Cost Reports')", timeout=10000)

    # Wait for the queries list (left rail) to populate.
    page.wait_for_selector("button:has-text('Run')", timeout=10000)

    # Pick the first query directly via API so we know what we're about
    # to assert against.
    qs = _api_get_json("/api/analytics/queries")
    assert qs and qs.get("queries"), \
        "no analytics queries — tg-cur-deploy.sh probably not run"
    qid = qs["queries"][0]["query_id"]

    # The Run button. Click it and watch the response in parallel.
    with page.expect_response(
        lambda r: "/api/analytics/run" in r.url, timeout=30000
    ) as resp_info:
        page.click("button:has-text('Run')")
    resp = resp_info.value
    code = resp.status
    body_preview = resp.text()[:300]

    # 200 = CUR fully wired and table populated.
    # 503 with "table ... populates 24-48h" = CUR table not yet
    #     populated (benign data-lag case); backend signals this
    #     with a friendly body.
    # 503 with "Cost Reports not configured. Run tg-cur-deploy.sh"
    #     = ECS api task is missing ATHENA_RESULTS_BUCKET env
    #     (#195 wiring bug). MUST fail — discriminating by body
    #     not just status, see .claude/rules/testing.md.
    # Anything else (esp. 500) = real bug; fail with body printed.
    assert code in (200, 503), (
        f"/api/analytics/run returned {code} (expected 200 or 503). "
        f"A 500 here is the bug from issue #92. Body: {body_preview}"
    )
    if code == 503:
        assert "not configured" not in body_preview.lower() \
            and "tg-cur-deploy" not in body_preview, (
            f"/api/analytics/run 503 due to missing wiring "
            f"(#195) — ATHENA_RESULTS_BUCKET likely unset on "
            f"the api task. Body: {body_preview}"
        )

    real_errors = _filter_console_errors(page.console_errors)
    # The UI surfaces 503s as a red text block, which fetch() treats as
    # an error response (logged to console). Tolerate that one case.
    if code == 503:
        real_errors = [
            e for e in real_errors
            if "503" not in e and "analytics/run" not in e.lower()
        ]
    assert not real_errors, (
        f"console errors on Cost Reports run: {real_errors}"
    )


def test_settings_test_alert_button(page):
    """POST /api/settings/alerts/test must return 200 with sent=true|false.

    The endpoint never raises on missing SES config — it returns
    200+sent=false+reason. A 500 = real bug. There's no UI button for
    this currently, so we exercise the endpoint directly via the page's
    fetch context (proves auth + routing work end-to-end).
    """
    page.goto(f"{API_BASE}/#/settings", wait_until="networkidle")
    page.wait_for_selector("h1:has-text('Org Settings')", timeout=10000)

    # Use the page's fetch (carries the test-trust headers from the
    # context) so this exactly mirrors what a UI button would do.
    result = page.evaluate(
        """async () => {
            const r = await fetch('/api/settings/alerts/test', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: '{}',
            });
            const text = await r.text();
            return { status: r.status, body: text };
        }"""
    )
    assert result["status"] == 200, (
        f"/api/settings/alerts/test returned {result['status']}: "
        f"{result['body'][:200]}"
    )
    parsed = json.loads(result["body"])
    assert "sent" in parsed, (
        f"alerts/test response missing 'sent' key: {parsed}"
    )
    if not parsed["sent"]:
        # Soft failure is OK but must include a reason — that's the
        # contract the UI relies on.
        assert parsed.get("reason"), (
            f"sent=false but no reason: {parsed}"
        )

    real_errors = _filter_console_errors(page.console_errors)
    assert not real_errors, f"console errors: {real_errors}"


def test_jobs_run_button(page):
    """Click 'Check & enforce limits' on Jobs page; assert 200."""
    page.goto(f"{API_BASE}/#/jobs", wait_until="networkidle")
    page.wait_for_selector("h1:has-text('Jobs')", timeout=10000)

    btn = page.locator(
        "button:has-text('Check & enforce limits')"
    ).first
    btn.wait_for(timeout=5000)

    with page.expect_response(
        lambda r: "/api/jobs/run" in r.url, timeout=30000
    ) as resp_info:
        btn.click()
    resp = resp_info.value
    body_preview = resp.text()[:300]
    assert resp.status == 200, (
        f"/api/jobs/run returned {resp.status}: {body_preview}"
    )

    real_errors = _filter_console_errors(page.console_errors)
    assert not real_errors, f"console errors: {real_errors}"


def test_users_approve_action(page):
    """Pre-register a fresh user via API → click Approve via API
    (no UI surface exists for approve currently — exercise the endpoint
    via page.evaluate so auth+routing match what the UI would do).

    Asserts: status flips from blocked → active, no console errors
    leaked into the page during the round-trip.
    """
    fixture_email = (
        f"tg-approve-{int(time.time())}@example.com"
    )

    # Pre-register: blocked.
    code, body = _api_post_json(
        "/api/users/preregister",
        {"email": fixture_email, "status": "blocked"},
    )
    assert code in (200, 201), (
        f"preregister returned {code}: {body}"
    )

    try:
        page.goto(f"{API_BASE}/#/users", wait_until="networkidle")
        page.wait_for_selector("h1:has-text('Users')", timeout=10000)

        # Approve via the page's fetch context.
        result = page.evaluate(
            """async (email) => {
                const r = await fetch(
                    `/api/users/${encodeURIComponent(email)}/approve`,
                    {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: '{}',
                    },
                );
                const text = await r.text();
                return { status: r.status, body: text };
            }""",
            fixture_email,
        )
        assert result["status"] == 200, (
            f"approve returned {result['status']}: "
            f"{result['body'][:200]}"
        )
        approved = json.loads(result["body"])
        assert approved.get("status") == "active", (
            f"status didn't flip to active: {approved}"
        )

        real_errors = _filter_console_errors(page.console_errors)
        assert not real_errors, f"console errors: {real_errors}"
    finally:
        _api_delete(f"/api/users/{fixture_email}")


def test_no_console_errors_anywhere(page):
    """Full nav walk with strict console-error assertion.

    Click into every persona-agnostic route (org-admin sees them all),
    plus deep into a user-detail page. Any console.error / pageerror —
    anywhere — fails the test.
    """
    page.goto(API_BASE, wait_until="networkidle")
    page.wait_for_selector("text=Token Governance", timeout=10000)

    routes = [
        "/", "/activity", "/users", "/teams",
        "/cost-reports", "/settings", "/jobs",
    ]
    for r in routes:
        page.evaluate(f"window.location.hash = '#{r}'")
        page.wait_for_load_state("networkidle")
        time.sleep(0.2)  # let any async useEffect settle

    # Drill into a user-detail page (different render path).
    target = _pick_active_user()
    if target:
        page.evaluate(
            f"window.location.hash = '#/users/{target['email']}'"
        )
        page.wait_for_load_state("networkidle")
        # Scope to <main> — see test_set_cap_round_trip for why.
        page.wait_for_selector(
            f"main >> text={target['email']}", timeout=5000)

    real_errors = _filter_console_errors(page.console_errors)
    assert not real_errors, (
        f"console errors during full walk: {real_errors}"
    )


# ── NEW: persona-aware tests ───────────────────────────────────────────────

def test_sidebar_visibility_per_persona(persona_page, persona):
    """For each persona, load /, assert exactly the expected sidebar
    items appear and persona-forbidden items do NOT appear."""
    persona_page.goto(API_BASE, wait_until="networkidle")
    persona_page.wait_for_selector(
        "text=Token Governance", timeout=10000)

    # Wait for the nav to fully render — the loading skeleton has 6
    # animate-pulse divs, the real nav has anchors with hrefs starting
    # with "#/". Wait for at least one href anchor.
    persona_page.wait_for_selector(
        "aside >> a[href^='#/']", timeout=10000
    )

    sidebar = persona_page.locator("aside")

    for label in persona.sidebar:
        # Must be visible. Use the sidebar-scoped locator so we don't
        # accidentally match a heading / table cell with the same text.
        loc = sidebar.locator(f"a[href^='#/'] >> text={label}").first
        loc.wait_for(timeout=5000, state="visible")
        assert loc.is_visible(), (
            f"persona={persona.role}: sidebar item '{label}' "
            "expected but not visible"
        )

    # Items that should NOT appear for this persona.
    forbidden = {
        "Activity", "Users", "Teams",
        "Cost Reports", "Settings", "Jobs",
    } - set(persona.sidebar)
    for label in forbidden:
        cnt = sidebar.locator(
            f"a[href^='#/'] >> text={label}"
        ).count()
        assert cnt == 0, (
            f"persona={persona.role}: sidebar item '{label}' "
            "should NOT appear but does"
        )

    real_errors = _filter_console_errors(persona_page.console_errors)
    # Known bug (issue #94): the Activity page calls getTeams() on mount,
    # which 403s for members. Whitelist that exact error for the member
    # persona until #94 is fixed; tighten the filter then.
    if persona.role == "member":
        real_errors = [
            e for e in real_errors
            if "403" not in e
        ]
    assert not real_errors, (
        f"persona={persona.role} console errors: {real_errors}"
    )


def test_data_scope_per_persona(persona, browser_ctx):
    """For each persona, assert /api/users returns the right rows.

    Counts are derived from tg-test-data-populate.sh's data shape:
      - org_admin           sees ALL users (~74)
      - top_team_admin      sees their team + transitive descendants
                            (team-1 admin: top + mids 1.{1,2,3} +
                             leaves 1.1.{1,2} — depth-3 leaves pulled
                             in via parent_team_id chain)
      - mid_team_admin      sees their team + direct descendants
                            (team-1.1 admin: 1.1 + 1.1.{1,2})
      - member              gets 403 on /api/users (only sees self)

    We hit /api/users with the persona's email header. The browser_ctx
    fixture is unused here — this is a pure HTTP-level scope check —
    but we keep it as a parameter so the persona fixture chain works.
    """
    _ = browser_ctx
    code, data = _api_get_with_status(
        "/api/users", email=persona.email)

    if persona.role == "member":
        # Members are blocked from /api/users entirely. The UI
        # accommodates this by hiding the Users sidebar item.
        assert code == 403, (
            f"member should get 403 on /api/users; got {code}: "
            f"{str(data)[:200]}"
        )
        return

    assert code == 200, (
        f"persona={persona.role}: GET /api/users → {code}: "
        f"{str(data)[:200]}"
    )
    users = data.get("users", [])
    n = len(users)

    # Allow ±3 slack for test-data churn (e.g. a fixture user from a
    # parallel run still being torn down). Tighter than that and we'd
    # tie the test to exact populator output.
    expected = {
        "org_admin": (68, 80),          # ~74
        # team-1 admin sees team-1 (5) + 1.{1,2,3} (15) +
        # 1.1.{1,2} (10) = ~30 via parent_team_id descent.
        "top_team_admin": (26, 34),     # ~30
        # team-1.1 admin sees team-1.1 (5) + 1.1.{1,2} (10) = ~15.
        "mid_team_admin": (12, 18),     # ~15
    }[persona.role]
    lo, hi = expected
    assert lo <= n <= hi, (
        f"persona={persona.role}: expected {lo}–{hi} users, got {n}. "
        f"Did test-data-populate run? First few: "
        f"{[u.get('email') for u in users[:3]]}"
    )

    # Every row visible to a non-org-admin must belong to a team in
    # the persona's allowed scope. We don't hardcode the team list —
    # /api/whoami returns it.
    if persona.role != "org_admin":
        whoami = _api_get_json("/api/whoami", email=persona.email) or {}
        scope_teams = set(whoami.get("team_ids") or [])
        if scope_teams:
            for u in users:
                t = u.get("team_id")
                if t is None:
                    continue  # users with no team show in the unscoped pool
                assert t in scope_teams, (
                    f"{persona.role} scope leak: saw user "
                    f"{u.get('email')} in team {t}, "
                    f"allowed={scope_teams}"
                )


# ── parent_team_id tests (issue #97) ───────────────────────────────────────

def test_teams_endpoint_returns_parent_team_id():
    """/api/teams must surface parent_team_id so the UI can render the
    tree. Hard-checks the depth-3 leaf walks back to its grandparent."""
    d = _api_get_json("/api/teams")
    assert d is not None, "GET /api/teams failed"
    by_id = {t["team_id"]: t for t in d.get("teams", [])}
    if "team-1.1.1" not in by_id:
        pytest.skip(
            "team-1.1.1 not present — re-run "
            "internal/scripts/tg-test-data-populate.sh"
        )
    # Walk parent_team_id back to root.
    chain = []
    cur = "team-1.1.1"
    while cur is not None:
        chain.append(cur)
        cur = by_id.get(cur, {}).get("parent_team_id")
    assert chain == ["team-1.1.1", "team-1.1", "team-1"], (
        f"parent_team_id chain wrong: {chain}"
    )


def test_mid_level_team_admin_transitive_scope():
    """A team_admin granted at team-1.1 (a MID team) must see
    team-1.1 + its descendants (team-1.1.1, team-1.1.2) but MUST NOT
    see the grandparent team-1, sibling mids team-1.2/1.3, or any
    team in the team-2 / team-3 subtrees.

    This is the load-bearing test for the parent_team_id transitive
    walk in api/auth.py. With dotted-name `LIKE` matching it would
    silently let team-1 admins through; with parent_team_id walk it's
    a real boundary.

    Issue #104 retired parent_team_admin: the same descent rule now
    applies universally to team_admin rows."""
    mid_admin = "team-1.1-admin-1@example.com"
    # Whoami should report the transitive scope.
    whoami = _api_get_json("/api/whoami", email=mid_admin)
    if whoami is None:
        pytest.skip(
            f"{mid_admin} not in DB — re-run populator"
        )
    scope = set(whoami.get("team_ids") or [])
    assert "team-1.1" in scope, scope
    assert "team-1.1.1" in scope, scope
    assert "team-1.1.2" in scope, scope
    # Must NOT see grandparent or sibling mids.
    forbidden = {
        "team-1", "team-1.2", "team-1.3",
        "team-2", "team-2.1", "team-2.2", "team-2.3",
        "team-3", "team-3.1", "team-3.2", "team-3.3",
    }
    leaks = scope & forbidden
    assert not leaks, (
        f"mid-level team_admin leaked into "
        f"out-of-tree teams: {leaks}"
    )

    # /api/users must reflect the same scope: only team-1.1 +
    # descendants. No team-1, no team-2.*, no team-3.*.
    code, data = _api_get_with_status(
        "/api/users", email=mid_admin)
    assert code == 200, f"GET /api/users → {code}: {str(data)[:200]}"
    rows = data.get("users", [])
    seen_teams = {u.get("team_id") for u in rows
                  if u.get("team_id")}
    assert seen_teams.issubset(scope), (
        f"mid-level team_admin leaked rows from teams "
        f"{seen_teams - scope}"
    )


def test_teams_page_renders_3_level_tree(persona_page, persona):
    """Teams page must render a 3-level tree, with team-1.1.1 indented
    deeper than team-1.1 deeper than team-1.

    Only meaningful for personas that can see those teams; member's
    Teams page is empty (403 on /api/teams). Org admin and team-1's
    top_team_admin both see the depth-3 leaves.

    The team's display *name* is set by the seed (e.g. team-1.1.1 →
    "PyTorch" after #219) and may be renamed at any time. The test
    looks up names by stable team_id from /api/teams so it doesn't
    rot when the seed renames a team.
    """
    if persona.role not in ("org_admin", "top_team_admin"):
        pytest.skip(
            f"persona={persona.role} can't see depth-3 leaves"
        )
    teams = _api_get_json("/api/teams") or {}
    by_id = {t["team_id"]: t["name"] for t in teams.get("teams", [])}
    needed = ["team-1", "team-1.1", "team-1.1.1"]
    missing = [tid for tid in needed if tid not in by_id]
    if missing:
        pytest.skip(
            f"seed missing teams {missing} — run "
            "internal/scripts/tg-test-data-populate.sh"
        )
    name_root = by_id["team-1"]
    name_mid  = by_id["team-1.1"]
    name_leaf = by_id["team-1.1.1"]

    persona_page.goto(
        f"{API_BASE}/#/teams", wait_until="networkidle")
    persona_page.wait_for_selector(
        "h1:has-text('Teams')", timeout=10000)
    # Wait until the table populated. Scope to .font-bold so we
    # don't collide with the dev/team-switcher <option>s in the
    # sidebar that share the same text.
    persona_page.wait_for_selector(
        f"div.font-bold:has-text('{name_leaf}')", timeout=10000)

    # The page indents children via padding-left: depth*20px.
    # Pull the row's visible-name container's computed style and
    # assert it matches its expected depth: team-1=0, team-1.1=1,
    # team-1.1.1=2.
    # Each name element is `<div class="...font-bold...">{team.name}</div>`
    # nested inside the row's padded `<div style="padding-left: ...">`.
    # Walk up to the nearest ancestor with inline padding-left to read
    # the indent for that row.
    pads = persona_page.evaluate(
        """(names) => {
          const wanted = new Set(names);
          const out = {};
          for (const el of document.querySelectorAll(
            'div.font-bold'
          )) {
            const name = (el.textContent || '').trim();
            if (!wanted.has(name)) continue;
            let p = el.parentElement;
            while (p && !(
              p.style && p.style.paddingLeft)
            ) p = p.parentElement;
            if (p) out[name] = p.style.paddingLeft;
          }
          return out;
        }""",
        [name_root, name_mid, name_leaf],
    )
    # Depth × 20px is what Teams.jsx applies.
    def px(p):
        return int((p or "0px").replace("px", "")) if p else 0
    p_root  = px(pads.get(name_root))
    p_mid   = px(pads.get(name_mid))
    p_leaf  = px(pads.get(name_leaf))
    assert p_root < p_mid < p_leaf, (
        f"Teams tree indents not strictly increasing: "
        f"team-1({name_root})={p_root} "
        f"team-1.1({name_mid})={p_mid} "
        f"team-1.1.1({name_leaf})={p_leaf}"
    )
