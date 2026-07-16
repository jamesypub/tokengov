"""
seed_quota_metrics — dev-only synthetic spend seeder (#426).

Writes one cur_user_spend row per user for the current hour
(model_id="synthetic", spend_usd=spend_per_user) so the dev UI
shows meaningful $ without real Bedrock invokes.

#724 (#720 slice 2): repointed from the retired quota_metrics
table to cur_user_spend — that's now the spend source every
reader queries, so dev seeding must write there or the seeded $
is invisible. Idempotent on the (email, usage_hour, region,
model_id) unique key — re-running overwrites spend_usd rather
than stacking it. (Job/endpoint name kept stable; it's wired
into the run-now map + the test-data populate path.)

Triggered via POST /internal/run-job/seed_quota_metrics
(org_admin only). Not on the worker scheduler — a one-shot
seeding helper.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from db.session import get_db
from db.models import User, CurUserSpend

log = logging.getLogger("worker.seed_quota_metrics")

# Synthetic seed region — display-only, never a deny key (#724).
_SEED_REGION = "us-east-1"


def run(spend_per_user: float = 0.50) -> dict:
    spend = float(spend_per_user)
    now = datetime.now(timezone.utc)
    usage_hour = now.replace(minute=0, second=0, microsecond=0)
    billing_period = now.strftime("%Y-%m")
    seeded = 0
    with get_db() as db:
        for u in db.query(User).all():
            existing = (
                db.query(CurUserSpend)
                .filter(
                    CurUserSpend.email == u.email,
                    CurUserSpend.usage_hour == usage_hour,
                    CurUserSpend.region == _SEED_REGION,
                    CurUserSpend.model_id == "synthetic",
                )
                .first()
            )
            if existing:
                existing.spend_usd = spend
            else:
                db.add(CurUserSpend(
                    email=u.email,
                    identity_arn=u.principal_arn,
                    usage_hour=usage_hour,
                    region=_SEED_REGION,
                    model_id="synthetic",
                    input_tokens=0,
                    output_tokens=0,
                    cache_write_tokens=0,
                    cache_read_tokens=0,
                    total_tokens=0,
                    spend_usd=spend,
                    billing_period=billing_period,
                    data_source="seed",
                ))
            seeded += 1
        db.flush()
    log.info(
        "seed_quota_metrics: %d users seeded at $%.2f for %s",
        seeded, spend, usage_hour.isoformat(),
    )
    return {"seeded": seeded, "usage_hour": usage_hour.isoformat(),
            "spend_per_user": spend}
