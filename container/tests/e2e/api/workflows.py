"""API-tier e2e ACTIONS — reusable wrappers over the real endpoints.

Fowler page-object spirit at the API layer: each function performs a
workflow step against a live endpoint and returns the raw result; it
holds NO assertions (the cases assert end state via the read actions).
Composed by the thin workflow cases (test_onboard.py, test_governance.py).
"""
from __future__ import annotations


# ── onboarding / teams ──────────────────────────────────────

def onboard_user(client, email, role="member", team=None):
    """Onboard a NEW user end to end: create the user row (+ team
    membership via the source of truth) with the create-user endpoint,
    then grant the role. Returns the grant response. This is the real
    'add a person and put them on a team' workflow — a users row must
    exist before a team membership can reference it."""
    create_body = {"email": email}
    if team is not None:
        create_body["team_id"] = team
    r = client.post("/api/users/preregister", json=create_body)
    r.raise_for_status()
    body = {"email": email, "role": role}
    if team is not None:
        body["team_id"] = team
    return client.post("/api/admin-roles", json=body)


def add_to_team(client, team, email):
    return client.post(f"/api/teams/{team}/members", json={"email": email})


def members(client, team):
    """The team's member emails (the LIST — source of truth)."""
    r = client.get(f"/api/teams/{team}/members")
    r.raise_for_status()
    return {m["email"] for m in r.json().get("members", [])}


def team_count(client, team):
    """The team's member_count as reported by the teams list."""
    r = client.get("/api/teams")
    r.raise_for_status()
    for t in r.json().get("teams", []):
        if t["team_id"] == team:
            return t["member_count"]
    return None


# ── governance ──────────────────────────────────────────────

def govern(client, email):
    # The "Manage" action sets governed=true (deny-only governance).
    return client.post(f"/api/users/{email}/manage")


def ungovern(client, email):
    # The "Unmanage" action sets governed=false.
    return client.post(f"/api/users/{email}/unmanage")


def user(client, email):
    """The user's detail row (to read governed state)."""
    r = client.get(f"/api/users/{email}")
    r.raise_for_status()
    return r.json()
