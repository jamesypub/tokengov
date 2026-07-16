"""API-tier e2e: onboarding a user is CONSISTENT across the members
list and the team count — the workflow gap that let the team
count-vs-list divergence ship. Onboard → the user appears in the
team's members LIST and the team's member_count == len(list).
"""
from __future__ import annotations

import pytest

from tests.e2e.api import workflows as wf

pytestmark = pytest.mark.e2e


def test_onboard_user_appears_in_list_and_count_matches(api):
    c = api()  # org admin
    team = "team-1.1"
    before = wf.members(c, team)

    r = wf.onboard_user(
        c, "onboardee@example.com", role="member", team=team)
    assert r.status_code == 200, r.text
    # onboard_user created the user + its team membership (the source of
    # truth the list reads) in one workflow — no separate add needed.

    after = wf.members(c, team)
    assert "onboardee@example.com" in after
    assert after == before | {"onboardee@example.com"}
    # The count the teams list reports must equal the members list — the
    # consistency the divergence bug broke.
    assert wf.team_count(c, team) == len(after)
