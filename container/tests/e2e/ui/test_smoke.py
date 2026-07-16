"""UI-tier e2e smoke: the onboard→listed workflow through the browser,
mirroring the cheaper API-tier onboard case. This is the heavier tier
— it needs a LIVE stack (a running API + built SPA) and Playwright, so
it SKIPS cleanly when either is absent (the deterministic consistency
coverage is the API tier). Start small: one smoke; grow later.

Run against a live test-trust stack:
    API_BASE=http://localhost:8000 pytest container/tests/e2e/ui -m e2e
"""
from __future__ import annotations

import os
import urllib.request

import pytest

pytestmark = pytest.mark.e2e

# Browser tier is optional infra — skip the whole module if Playwright
# isn't installed (mirrors test_browser.py's importorskip).
pytest.importorskip("playwright.sync_api")

API_BASE = os.environ.get("API_BASE", "http://localhost:8000")


def _stack_up() -> bool:
    try:
        with urllib.request.urlopen(
                f"{API_BASE}/api/version", timeout=3):
            return True
    except Exception:
        return False


@pytest.fixture
def live_stack():
    # The UI tier needs a KNOWN-GOOD seeded test-trust stack (running
    # API + built SPA + Chromium). It is opt-in via TG_E2E_UI=1 so it
    # skips by default — a bare API answering on API_BASE is not proof
    # it's the seeded test-trust target, and we won't run a browser
    # against an arbitrary stack. The deterministic consistency coverage
    # lives in the API tier; this is the heavier, operator-run mirror.
    if os.environ.get("TG_E2E_UI") != "1":
        pytest.skip(
            "UI e2e is opt-in: set TG_E2E_UI=1 with API_BASE pointing "
            "at a seeded test-trust stack (API + built SPA). The API "
            "tier covers the same workflow deterministically.")
    if not _stack_up():
        pytest.skip(f"no live stack at {API_BASE}")
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Chromium not launchable ({e}); UI e2e skipped")


def test_add_user_appears_on_users_page(live_stack):
    from playwright.sync_api import sync_playwright
    from tests.e2e.ui.pages import UsersPage

    email = "ui-e2e@example.com"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context()
        ctx.set_extra_http_headers({
            "Authorization": "AWS4-HMAC-SHA256 testbypass",
            "X-Tg-Test-Email": os.environ.get(
                "TG_BOOTSTRAP_ADMIN_EMAIL",
                "tg-org-admin@example.com"),
        })
        page = ctx.new_page()
        users = UsersPage(page, API_BASE).open()
        users.open_add_user().add_user(email)
        users.open()  # reload the list
        assert any(email in e for e in users.row_emails())
        browser.close()
