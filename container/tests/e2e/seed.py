"""Load the public e2e seed (fixtures/e2e_seed.json) into a clean DB.

Replaces the scrubbed private populator so the e2e suite runs on a
fresh stack with NO dependency on the private populator. Synthetic @example.com users
+ scrub-safe ids only. Team assignment goes through the membership
source of truth (assign_user_team) so seeded users appear in the
members list + count consistently (the one-user-one-team contract).
"""
from __future__ import annotations

import json
import os

from db.session import get_db
from db.models import (
    Team, User, AdminRole, ModelPricing, CurUserSpend,
)
from db.teams_membership import assign_user_team
from db.usage_windows import month_start_utc

_SEED_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),  # container/tests
    "fixtures", "e2e_seed.json",
)


def load_seed(path: str = _SEED_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


def seed_db(data: dict | None = None) -> None:
    """Insert the seed into the current DB (idempotent-ish: assumes a
    clean_db). Order matters for FKs: teams → users → memberships →
    roles → pricing → spend."""
    data = data or load_seed()
    with get_db() as db:
        # e2e models an external-IdP deployment: onboarding authorizes
        # only (no Cognito AdminCreateUser), so the workflow cases stay
        # AWS-free and deterministic. (The Cognito-provisioning path has
        # its own unit coverage.)
        from db.org_config import set_tg_owns_directory
        set_tg_owns_directory(db, False)
        for t in data.get("teams", []):
            db.add(Team(
                team_id=t["team_id"], name=t["name"],
                parent_team_id=t.get("parent_team_id")))
        db.flush()
        for u in data.get("users", []):
            db.add(User(
                email=u["email"], status=u.get("status", "active"),
                cap_usd=u.get("cap_usd"),
                principal_arn=u.get("principal_arn"),
                principal_type=u.get("principal_type"),
                identity_key=u["email"]))
        db.flush()
        # Team assignment via the source of truth (creates the
        # membership + shadows User.team_id) so count == list holds.
        for u in data.get("users", []):
            if u.get("team_id"):
                assign_user_team(
                    db, u["email"], u["team_id"], added_by="e2e-seed")
        for r in data.get("admin_roles", []):
            db.add(AdminRole(
                email=r["email"], role=r["role"],
                team_id=r.get("team_id"), granted_by="e2e-seed"))
        for p in data.get("model_pricing", []):
            db.add(ModelPricing(
                model_id=p["model_id"],
                input_per_1m=p["input_per_1m"],
                output_per_1m=p["output_per_1m"],
                cache_write_per_1m=p.get("cache_write_per_1m", 0.0),
                cache_read_per_1m=p.get("cache_read_per_1m", 0.0),
                updated_by="e2e-seed"))
        for s in data.get("cur_user_spend", []):
            db.add(CurUserSpend(
                email=s["email"], model_id=s["model_id"],
                usage_hour=month_start_utc(),
                region="us-east-1",
                input_tokens=s.get("input_tokens", 0),
                output_tokens=s.get("output_tokens", 0),
                total_tokens=s.get("input_tokens", 0)
                + s.get("output_tokens", 0),
                spend_usd=s.get("spend_usd", 0.0)))
        db.flush()
