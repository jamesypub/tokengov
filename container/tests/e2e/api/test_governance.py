"""API-tier e2e: the govern → ungovern cycle is CONSISTENT — the
user's governed state flips as observed through the read API. This
guards the attachment-vs-enforcement class of workflow bug (a user
reported governed while enforcing nothing, or vice-versa) at the
app-contract level; the reconciler+mocked-IAM depth stays in the
existing unit tests.
"""
from __future__ import annotations

import pytest

from tests.e2e.api import workflows as wf

pytestmark = pytest.mark.e2e


def test_govern_then_ungovern_round_trips(api, no_aws):
    # no_aws stubs boto3 so Manage/Unmanage's IAM attach is a no-op —
    # the case asserts the app-contract flag round-trip, not the real
    # attach (deep IAM path is unit-covered in test_deny_reconciler).
    c = api()  # org admin
    email = "member-1@example.com"  # seeded, has a governable role ARN

    assert wf.user(c, email)["governed"] is False

    r = wf.govern(c, email)
    assert r.status_code == 200, r.text
    assert wf.user(c, email)["governed"] is True

    r = wf.ungovern(c, email)
    assert r.status_code == 200, r.text
    assert wf.user(c, email)["governed"] is False
