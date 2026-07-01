"""
Catalog of Bedrock-supported Claude models for Claude Code.

The list here drives the Settings → Model pricing table. Anything
admins might want to set a price for must appear in CATALOG.

Source: AWS Bedrock model lifecycle + Anthropic models overview,
verified 2026-05-27. Drop a model when AWS lists it as Legacy/EOL;
add new ones when they ship.
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import DiscoveredModel
from api.auth import get_caller_email, Scope

router = APIRouter()


CATALOG: list[dict] = [
    {
        "model_id":           "us.anthropic.claude-opus-4-8",
        "display_name":       "Claude Opus 4.8",
        "input_per_1m":       5.00,
        "output_per_1m":      25.00,
        "cache_write_per_1m": 6.25,
        "cache_read_per_1m":  0.50,
    },
    {
        "model_id":           "us.anthropic.claude-opus-4-7",
        "display_name":       "Claude Opus 4.7",
        "input_per_1m":       5.00,
        "output_per_1m":      25.00,
        "cache_write_per_1m": 6.25,
        "cache_read_per_1m":  0.50,
    },
    {
        "model_id":           "us.anthropic.claude-opus-4-6-v1",
        "display_name":       "Claude Opus 4.6",
        "input_per_1m":       5.00,
        "output_per_1m":      25.00,
        "cache_write_per_1m": 6.25,
        "cache_read_per_1m":  0.50,
    },
    {
        "model_id":           "us.anthropic.claude-opus-4-5-20251101-v1:0",
        "display_name":       "Claude Opus 4.5",
        "input_per_1m":       5.00,
        "output_per_1m":      25.00,
        "cache_write_per_1m": 6.25,
        "cache_read_per_1m":  0.50,
    },
    {
        "model_id":           "us.anthropic.claude-opus-4-1-20250805-v1:0",
        "display_name":       "Claude Opus 4.1",
        "input_per_1m":       15.00,
        "output_per_1m":      75.00,
        "cache_write_per_1m": 18.75,
        "cache_read_per_1m":  1.50,
    },
    {
        "model_id":           "us.anthropic.claude-sonnet-4-6",
        "display_name":       "Claude Sonnet 4.6",
        "input_per_1m":       3.00,
        "output_per_1m":      15.00,
        "cache_write_per_1m": 3.75,
        "cache_read_per_1m":  0.30,
    },
    {
        "model_id":           "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "display_name":       "Claude Sonnet 4.5",
        "input_per_1m":       3.00,
        "output_per_1m":      15.00,
        "cache_write_per_1m": 3.75,
        "cache_read_per_1m":  0.30,
    },
    {
        "model_id":           "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "display_name":       "Claude Haiku 4.5",
        "input_per_1m":       1.00,
        "output_per_1m":      5.00,
        "cache_write_per_1m": 1.25,
        "cache_read_per_1m":  0.10,
    },
]


def _db():
    with get_db() as db:
        yield db


def _scope(
    request: Request,
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


def _discovered_display_name(model_id: str) -> str:
    """Best-effort human label for a CUR-discovered model_id that isn't
    in CATALOG. The raw id is unambiguous, so we keep it visible and only
    prettify lightly: drop the CRIS geo prefix (us./global./eu./apac.)
    and the trailing version suffix (-vN / -YYYYMMDD-vN:N), title-case
    the remaining anthropic.<name>. Falls back to the raw id."""
    mid = model_id
    for pre in ("us.", "global.", "eu.", "apac."):
        if mid.startswith(pre):
            mid = mid[len(pre):]
            break
    mid = mid.replace("anthropic.", "")
    # strip a trailing -<date>-v<x>:<y> or -v<x> version suffix
    import re
    mid = re.sub(r"-\d{8}-v\d+(?::\d+)?$", "", mid)
    mid = re.sub(r"-v\d+(?::\d+)?$", "", mid)
    pretty = mid.replace("-", " ").strip().title()
    return pretty or model_id


def build_catalog(db: Session) -> list[dict]:
    """Static CATALOG ∪ every model observed in CUR (discovered_models)
    that isn't already in CATALOG. Listed by DISTINCT model_id — us.* and
    global.* variants are SEPARATE entries, NOT collapsed (owner: no
    region/profile-agnostic de-dupe). So a model a principal starts using
    (or AWS ships) auto-appears as blockable after the next cur_spend_sync
    writes its discovered_models row — no code change. Discovered-only
    entries carry a derived display name, null pricing (still blockable;
    model_pricing seeding is separate), and discovered=True for a UI hint.
    Empty discovered_models → exactly the static CATALOG (no regression)."""
    catalog = [dict(m, discovered=False) for m in CATALOG]
    known = {m["model_id"] for m in CATALOG}
    rows = (
        db.query(DiscoveredModel.model_id)
        .order_by(DiscoveredModel.model_id)
        .all()
    )
    for (model_id,) in rows:
        if not model_id or model_id in known:
            continue
        known.add(model_id)
        catalog.append({
            "model_id":           model_id,
            "display_name":       _discovered_display_name(model_id),
            "input_per_1m":       None,
            "output_per_1m":      None,
            "cache_write_per_1m": None,
            "cache_read_per_1m":  None,
            "discovered":         True,
        })
    return catalog


@router.get("/models/catalog")
def get_catalog(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    return {"models": build_catalog(db)}
