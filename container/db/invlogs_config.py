"""invlogs_config — the persisted region→bucket CATALOG for Bedrock
invocation logging (the analytics capture stream, separate from CUR
spend).

This is the load-bearing "where the invocation data lives" map: the
set of regions logging is enabled in, each region's S3 bucket name, and
whether Text capture is on. It is stored in `admin_config` (one JSON
row) and read by BOTH the admin Settings surface AND a future analysis
layer — so the recommender knows exactly which regions/buckets hold the
data without scanning every region.

Each catalog entry:
  {"region": "us-east-1",
   "bucket": "tg-bedrock-invlogs-us-east-1-<account>",
   "enabled": true,
   "text_on": true}

Bedrock invocation logging is a per-region same-region singleton, so
one entry == one region == one bucket == one deployed
`cfn/tg-bedrock-invocation-logs.yaml` stack. Mirrors org_config's
JSON-list accessor shape (get returns [] on unset/malformed so nothing
downstream crashes; set validates + stores a normalized JSON array).
"""
from __future__ import annotations
import json
import re

from sqlalchemy.orm import Session

from db.models import AdminConfig

INVLOGS_REGIONS_KEY = "invlogs_regions"

# AWS region tokens: <area>-<direction>-<number> (e.g. us-east-1,
# eu-west-2, ap-southeast-1). Validate so a typo can't provision a
# nonsense stack / bucket name.
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d+$")


def _bucket_for(region: str, account_id: str) -> str:
    """The deterministic per-region bucket name the slice-1 CFN
    auto-names (tg-bedrock-invlogs-<region>-<account>). One definition
    so the catalog, the stack, and the future reader agree."""
    return f"tg-bedrock-invlogs-{region}-{account_id}"


def s3_uri_for(bucket: str, key_prefix: str = "") -> str:
    """The full S3 location the logs land in, as a single authoritative
    server-built string — so the UI shows one path and never assembles
    `s3://…` itself. The sink writes with an empty key-prefix today
    (invlogs_apply enable_region), so this is normally `s3://<bucket>`;
    a non-empty prefix (should one ever be configured) is appended so
    the displayed path stays truthful. Blank bucket → "" (nothing to
    show; the UI hides the path for off/unresolved regions)."""
    if not bucket:
        return ""
    prefix = (key_prefix or "").strip("/")
    return f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"


def _normalize(entry: dict, account_id: str) -> dict | None:
    """Coerce one raw entry to the canonical shape, or None if the
    region is invalid. bucket is always DERIVED (never trusted from the
    client) so the catalog can't point the reader at an arbitrary
    bucket. s3_uri is the full location the UI displays, built here so
    the path lives in exactly one place."""
    if not isinstance(entry, dict):
        return None
    region = str(entry.get("region", "")).strip().lower()
    if not _REGION_RE.match(region):
        return None
    bucket = _bucket_for(region, account_id)
    return {
        "region": region,
        "bucket": bucket,
        "s3_uri": s3_uri_for(bucket),
        "enabled": bool(entry.get("enabled", True)),
        "text_on": bool(entry.get("text_on", True)),
    }


def get_invlogs_regions(db: Session, account_id: str = "") -> list[dict]:
    """The invocation-logging region catalog — a list of
    {region, bucket, enabled, text_on}. Returns [] when unset or
    malformed (never crashes a caller). bucket is recomputed from
    account_id so it stays correct even if the stored value predates a
    naming change."""
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == INVLOGS_REGIONS_KEY)
        .first()
    )
    if not row or row.value is None:
        return []
    try:
        val = json.loads(row.value)
    except (TypeError, ValueError):
        return []
    if not isinstance(val, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in val:
        norm = _normalize(item, account_id)
        if norm and norm["region"] not in seen:
            seen.add(norm["region"])
            out.append(norm)
    return out


def set_invlogs_regions(
    db: Session, entries: list[dict], account_id: str = "",
) -> list[dict]:
    """Replace the region catalog. Validates every entry has a real
    AWS region; stores a deduped, region-sorted JSON array with
    DERIVED bucket names. Raises ValueError on a non-list or an entry
    with an invalid region."""
    if not isinstance(entries, list):
        raise ValueError("invlogs_regions must be a list")
    cleaned: list[dict] = []
    seen: set[str] = set()
    for item in entries:
        norm = _normalize(item, account_id)
        if norm is None:
            raise ValueError(
                "each entry needs a valid AWS region "
                "(e.g. us-east-1)")
        if norm["region"] in seen:
            continue
        seen.add(norm["region"])
        cleaned.append(norm)
    cleaned.sort(key=lambda e: e["region"])
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == INVLOGS_REGIONS_KEY)
        .first()
    )
    payload = json.dumps(cleaned, separators=(",", ":"))
    if row:
        row.value = payload
    else:
        db.add(AdminConfig(key=INVLOGS_REGIONS_KEY, value=payload))
    db.flush()
    return cleaned


def get_invlogs_regions_updated_at(db: Session):
    """When the catalog was last saved (admin_config.updated_at), or
    None. The apply-status UI compares this against the reconciler's
    last run the same way the blocked-models surface does."""
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == INVLOGS_REGIONS_KEY)
        .first()
    )
    return row.updated_at if row else None
