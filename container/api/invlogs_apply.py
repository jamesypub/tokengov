"""invlogs_apply — apply the Bedrock invocation-logging config for the
analytics capture stream via the Bedrock API (NOT CloudFormation).

Owner decision (slice 2): a Settings region enable/disable toggles the
per-region logging SINGLETON by calling
Put/DeleteModelInvocationLoggingConfiguration against the
INSTALL-provisioned S3 bucket (the slice-1 stack created it). No
CFN-from-app; a small scoped task-role grant
(*ModelInvocationLoggingConfiguration + Get) is all it needs.

Two load-bearing safety rules:
  1. ENABLE only when invocation logging is NOT already enabled — tg
     never clobbers a config the customer already runs (respects the
     account/Region singleton). If a config is present we leave it and
     report already_enabled.
  2. DISABLE only when the live sink is OUR bucket — never turn off a
     config pointing somewhere else (fail closed).

Bedrock invocation logging is a per-region same-region singleton, so
each call targets ONE region (a bedrock client bound to that region).
The bedrock client is injected so tests mock it — no live AWS.

Text modality mirrors the slice-1 template: S3 destination only, no
largeDataDeliveryS3Config (invalid for an S3-only sink — large bodies
overflow to the bucket data prefix automatically).
"""
from __future__ import annotations

import logging

log = logging.getLogger("api.invlogs_apply")

# Apply outcomes (returned per region so Settings can report honestly).
APPLIED_ENABLED = "enabled"            # tg turned it on
ALREADY_ENABLED = "already_enabled"    # a config already present — left as-is
APPLIED_DISABLED = "disabled"          # tg turned off OUR config
NOT_OURS = "not_ours"                  # live config points elsewhere — left as-is
NOOP = "noop"                          # already in the desired state
FAILED = "failed"                      # the API call errored


def _live_config(bedrock) -> dict | None:
    """The current loggingConfig for this region, or None if logging is
    not enabled / unreadable. Bedrock returns {} (or omits
    loggingConfig) when disabled."""
    try:
        resp = bedrock.get_model_invocation_logging_configuration()
    except Exception as e:  # noqa: BLE001 — treat unreadable as unknown
        log.info("invlogs: get config failed: %s", e)
        return None
    return (resp or {}).get("loggingConfig") or None


def _live_s3_bucket(cfg: dict | None) -> str | None:
    if not cfg:
        return None
    return ((cfg.get("s3Config") or {}).get("bucketName")) or None


def enable_region(bedrock, bucket: str, *, text_on: bool = True) -> str:
    """Enable invocation logging in this region → OUR bucket, but ONLY
    if no config is already present (never clobber the singleton).
    Returns APPLIED_ENABLED / ALREADY_ENABLED / NOOP / FAILED."""
    live = _live_config(bedrock)
    if live is not None:
        # A config already exists. If it's already ours + matching,
        # that's a no-op; otherwise leave the customer's config intact.
        if _live_s3_bucket(live) == bucket:
            return NOOP
        log.info(
            "invlogs: logging already enabled to a different sink — "
            "leaving it (not clobbering the singleton)")
        return ALREADY_ENABLED
    try:
        bedrock.put_model_invocation_logging_configuration(
            loggingConfig={
                "textDataDeliveryEnabled": bool(text_on),
                "imageDataDeliveryEnabled": False,
                "embeddingDataDeliveryEnabled": False,
                "s3Config": {"bucketName": bucket, "keyPrefix": ""},
            })
        return APPLIED_ENABLED
    except Exception as e:  # noqa: BLE001 — report, never crash the toggle
        log.warning("invlogs: enable failed for %s: %s", bucket, e)
        return FAILED


def disable_region(bedrock, bucket: str) -> str:
    """Disable invocation logging in this region — but ONLY when the
    live sink is OUR bucket (fail closed; never turn off a config that
    points elsewhere). Returns APPLIED_DISABLED / NOT_OURS / NOOP /
    FAILED."""
    live = _live_config(bedrock)
    if live is None:
        return NOOP
    if _live_s3_bucket(live) != bucket:
        return NOT_OURS
    try:
        bedrock.delete_model_invocation_logging_configuration()
        return APPLIED_DISABLED
    except Exception as e:  # noqa: BLE001
        log.warning("invlogs: disable failed for %s: %s", bucket, e)
        return FAILED


def plan(desired: list[dict], live_regions: set[str]) -> dict:
    """Pure diff: given the DESIRED catalog (list of
    {region, bucket, enabled, text_on}) and the set of regions that
    currently have OUR logging live, compute which regions to enable
    vs disable. A desired entry with enabled=False is a disable.
    Unit-tested without any AWS."""
    to_enable, to_disable = [], []
    desired_on = set()
    seen_disable: set[str] = set()
    for e in desired:
        region = e.get("region")
        if not region:
            continue
        if e.get("enabled", True):
            desired_on.add(region)
            if region not in live_regions:
                to_enable.append(e)
        else:
            # Explicitly disabled → disable iff currently live.
            if region in live_regions:
                to_disable.append(e)
            seen_disable.add(region)
    # A region live but ENTIRELY absent from the desired list (not just
    # disabled above) → orphan, disable it. Skip regions already handled
    # in the disable branch so it isn't listed twice.
    for region in live_regions - desired_on - seen_disable:
        to_disable.append({"region": region})
    return {"enable": to_enable, "disable": to_disable}
