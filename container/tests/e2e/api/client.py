"""API-tier e2e engine: an authenticated client per persona.

Wraps a FastAPI TestClient and injects the test-trust headers
(Authorization bypass + X-Tg-Test-Email) so requests resolve as the
given persona (org admin / team admin / member). This is the reusable
transport; workflows.py layers reusable ACTIONS on top, and the cases
assert end state via read endpoints. No assertions live here.
"""
from __future__ import annotations


def _headers_for(email: str) -> dict:
    # Mirrors the test-trust headers the browser harness uses: the
    # bypass Authorization + the impersonated email. Valid only when
    # the app runs with TG_AUTH_TEST_TRUST=1 (set by the e2e fixture).
    return {
        "Authorization": "AWS4-HMAC-SHA256 testbypass",
        "X-Tg-Test-Email": email,
    }


class PersonaClient:
    """Thin persona-scoped wrapper over a FastAPI TestClient."""

    def __init__(self, client, email: str):
        self._c = client
        self.email = email
        self._h = _headers_for(email)

    def get(self, path: str):
        return self._c.get(path, headers=self._h)

    def post(self, path: str, json=None):
        return self._c.post(path, headers=self._h, json=json or {})

    def put(self, path: str, json=None):
        return self._c.put(path, headers=self._h, json=json or {})

    def delete(self, path: str, json=None):
        return self._c.request(
            "DELETE", path, headers=self._h, json=json or {})
