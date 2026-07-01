"""
cur_spend_sync — populate cur_user_spend from AWS-billed CUR 2.0.
#724 (#720 slice 2).

The new spend source. Hourly (CUR delivers <=3x/day) it queries
Athena over the current + previous billing month, grouped by
(principal, usage_hour, region, model_id), classifies each
principal through the shared classify_principal()/classify_role_type()
(#723), JIT-upserts the User row + DiscoveredModel + PrincipalModel,
and REPLACES the current-month rows in cur_user_spend (CUR overwrites
the month partition — never additive; a month can revise down).

Reads the populated `line_item_iam_principal` column (#714). Region
is recorded for display/aggregation only — NEVER a deny key.

Fail-soft: if CUR/Athena is absent or unhealthy, log + return a
status dict rather than crash (feeds the 720d cur_health warning).

Option C (#720b): this ADDS the CUR path; metrics_aggregator +
quota_metrics stay in place until #725 (720c) removes them.
"""
from __future__ import annotations
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from db.session import get_db
from db.models import (
    CurUserSpend, DiscoveredModel, PrincipalModel, User,
)
from worker.principal_classify import (
    classify_principal, classify_role_type,
)

log = logging.getLogger("worker.cur_spend_sync")

REGION = os.environ.get("AWS_REGION", "us-east-1")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "tg-cur-analytics")
ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "tg_cur")
ATHENA_RESULTS_BUCKET = os.environ.get("ATHENA_RESULTS_BUCKET", "")
CUR_TABLE_NAME = os.environ.get("CUR_TABLE_NAME", "data")


def _billing_periods() -> list[str]:
    """Current + previous billing month as YYYY-MM (CUR overwrites
    only these partitions; older months are closed)."""
    now = datetime.now(timezone.utc)
    cur = now.strftime("%Y-%m")
    py, pm = (now.year, now.month - 1) if now.month > 1 else (
        now.year - 1, 12)
    prev = f"{py:04d}-{pm:02d}"
    return [prev, cur]


def _query_sql(periods: list[str]) -> str:
    """Per-(principal, hour, region, model) spend + usage over the
    given billing periods. model_id is parsed from the
    inference-profile/foundation-model resource ARN, falling back
    to usage_type when the resource id is null."""
    # #785: filter on the `billing_period` PARTITION key, not the
    # `bill_billing_period_start_date` data column. periods are
    # 'YYYY-MM' strings (from _billing_periods), which is exactly the
    # partition's projection format (cfn/tg-cur-athena.yaml: yyyy-MM).
    # The timestamp column is timestamp(3), so `timestamp IN
    # ('2026-06')` raised TYPE_MISMATCH (varchar vs timestamp) and the
    # whole query FAILED every cycle → fail-soft skip → cur_user_spend
    # never populated. The partition key also prunes partitions
    # (cheaper/faster scan) instead of reading the timestamp column.
    plist = ", ".join(f"'{p}'" for p in periods)
    tbl = f'"{ATHENA_DATABASE}"."{CUR_TABLE_NAME}"'
    # #806: also group by line_item_usage_type so each row carries the
    # token DIMENSION (input / output / cache-read / cache-write), with
    # usage_amount = that dimension's token count. The write loop
    # classifies usage_type → the right token column (cache BEFORE
    # input — `cache-read-input-token-count` contains `input-token`).
    # spend_usd still sums correctly: the multiple dimension rows per
    # (principal,hour,region,model) accumulate onto the one
    # cur_user_spend row via the existing spend_rows[key] += logic.
    # model_id is derived BEFORE usage_type splits it, so all four
    # dimensions map to the same model row.
    return f"""
      SELECT
        line_item_iam_principal                       AS principal,
        date_trunc('hour', line_item_usage_start_date) AS usage_hour,
        product_region_code                           AS region,
        COALESCE(
          REGEXP_EXTRACT(line_item_resource_id,
                         '(?:inference-profile|foundation-model)/(.+)$', 1),
          line_item_usage_type)                       AS model_id,
        line_item_usage_type                          AS usage_type,
        SUM(line_item_unblended_cost)                 AS spend_usd,
        SUM(line_item_usage_amount)                   AS usage_amount
      FROM {tbl}
      WHERE line_item_product_code = 'AmazonBedrockService'
        AND line_item_iam_principal IS NOT NULL
        AND billing_period IN ({plist})
      GROUP BY 1, 2, 3, 4, 5
    """


def _run_athena(sql: str) -> tuple[list[str], list[list[str]]]:
    """Start → poll → fetch. Returns (columns, rows). Raises on a
    genuine Athena failure so the caller's fail-soft wrapper can
    classify it."""
    ath = boto3.client("athena", region_name=REGION)
    exec_resp = ath.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": ATHENA_DATABASE},
        WorkGroup=ATHENA_WORKGROUP,
        ResultConfiguration={"OutputLocation": ATHENA_RESULTS_BUCKET},
    )
    execution_id = exec_resp["QueryExecutionId"]
    deadline = time.time() + 55
    state = "QUEUED"
    while time.time() < deadline:
        status = ath.get_query_execution(
            QueryExecutionId=execution_id)
        state = status["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status["QueryExecution"]["Status"].get(
                "StateChangeReason", state)
            raise RuntimeError(f"Athena {state}: {reason}")
        time.sleep(1)
    if state != "SUCCEEDED":
        raise RuntimeError("Athena query timed out")

    results = ath.get_query_results(
        QueryExecutionId=execution_id, MaxResults=1000)
    rs = results.get("ResultSet", {}).get("Rows", [])
    columns = (
        [c.get("VarCharValue", "") for c in rs[0].get("Data", [])]
        if rs else [])
    rows = [
        [c.get("VarCharValue", "") for c in r.get("Data", [])]
        for r in rs[1:]
    ]
    return columns, rows


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _classify_token_dimension(usage_type: str) -> str | None:
    """#806: map a CUR line_item_usage_type to its token dimension —
    one of 'cache_read' / 'cache_write' / 'input' / 'output', or None
    when the usage_type isn't a token dimension (so its usage_amount is
    not counted as tokens). Real usage_type values are NOT uniform and
    carry varying suffixes (-cross-region-global, -standard,
    -global-standard); match on the dimension SUBSTRING, not the whole
    string.

    ORDER IS LOAD-BEARING: cache types contain the substring
    `input-token` (e.g. `cache-read-input-token-count`), so cache MUST
    be checked BEFORE input/output or cache rows get miscounted as
    input (the #806 gotcha). Observed shapes:
        USE1-Claude4.5Haiku-input-tokens
        USW2-...-output-tokens-cross-region-global
        USW2-...-cache-read-input-token-count-cross-region-global
        USW2-...-cache-write-input-token-count-cross-region-global
    """
    u = (usage_type or "").lower()
    if "cache-read" in u:
        return "cache_read"
    if "cache-write" in u:
        return "cache_write"
    if "input-token" in u:
        return "input"
    if "output-token" in u:
        return "output"
    return None


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_hour(v: str):
    """Athena date_trunc('hour', ts) → 'YYYY-MM-DD HH:00:00[.000]'.
    Parse to an aware UTC datetime; None on unparseable."""
    if not v:
        return None
    head = v.strip().replace("T", " ").split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(head, fmt).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def run() -> dict:
    """Sync CUR → cur_user_spend. Fail-soft: returns a status dict;
    never raises out (the worker scheduler must not crash-loop on a
    missing/unhealthy CUR)."""
    periods = _billing_periods()
    try:
        columns, rows = _run_athena(_query_sql(periods))
    except (ClientError, RuntimeError) as e:
        log.warning(
            "cur_spend_sync: CUR/Athena unavailable (%s) — "
            "skipping this cycle (feeds cur_health, 720d)", e)
        return {"status": "skipped", "reason": str(e), "rows": 0}

    idx = {name: i for i, name in enumerate(columns)}

    def col(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    written = 0
    principals_seen: set[str] = set()
    # #789: the same model_id (and identity_key+model_id pair) recurs
    # across many CUR rows in one batch. The check-then-add below uses
    # db.query(...).first(), which does NOT see this session's own
    # un-flushed pending adds (flush is once, after the loop) — so a
    # repeated id was added twice and the post-loop flush collided on
    # discovered_models_pkey / principal_models_pkey, rolling back the
    # WHOLE spend ingest ($0 in the UI). Track what we've already
    # added THIS batch so each distinct id is added exactly once.
    models_added: set[str] = set()
    principal_models_added: set[tuple[str, str]] = set()
    # identity_key → the User created/looked up this batch (the same
    # principal recurs across rows; avoid re-adding → users_pkey).
    users_added: dict[str, User] = {}
    # #950: identity_key → {principal_arn → {spend, rows, raw_arn,
    # ptype}}. One identity (session-name) can be billed under MULTIPLE
    # roles in one CUR window (e.g. the same person via tg-consumer AND
    # tg-install-from-...). We accumulate each candidate role's spend +
    # row-count across the batch and, AFTER the loop, reconcile the
    # User's principal_arn to the DOMINANT (highest-spend) role — never
    # the last-seen one. CUR is ground truth, so this overwrites a
    # stale seeded value (e.g. a hardcoded tg-consumer).
    principal_candidates: dict[str, dict[str, dict]] = {}
    # #789: per-(email, usage_hour, region, model_id) spend row this
    # batch, so rows whose distinct ARNs collapse to one identity_key
    # accumulate onto a single row instead of colliding on the unique
    # constraint.
    spend_rows: dict[tuple, CurUserSpend] = {}
    now = datetime.now(timezone.utc)
    cur_period = periods[-1]

    with get_db() as db:
        # Replace the current billing month's rows (CUR overwrites
        # that partition; never additive). Prior month is closed —
        # re-upsert is idempotent but we clear the current month so
        # a downward revision lowers the stored total.
        db.query(CurUserSpend).filter(
            CurUserSpend.billing_period == cur_period).delete()

        for row in rows:
            arn = col(row, "principal")
            usage_hour = _parse_hour(col(row, "usage_hour"))
            region = col(row, "region")
            model_id = col(row, "model_id") or ""
            spend = _to_float(col(row, "spend_usd"))
            if not arn or usage_hour is None or not model_id:
                continue

            # #806: split this row's usage_amount into the right token
            # column by its usage_type dimension (cache before input).
            # A non-token dimension (or unrecognized usage_type) yields
            # no token counts — spend_usd still accumulates as before.
            dim = _classify_token_dimension(col(row, "usage_type") or "")
            amt = _to_int(col(row, "usage_amount"))
            tok = {
                "input_tokens": amt if dim == "input" else 0,
                "output_tokens": amt if dim == "output" else 0,
                "cache_read_tokens": amt if dim == "cache_read" else 0,
                "cache_write_tokens": amt if dim == "cache_write" else 0,
            }
            # total_tokens = input + output (matches the pre-#720
            # invocation-log convention so trends stay continuous;
            # cache read/write are tracked + displayed separately).
            tok["total_tokens"] = tok["input_tokens"] + tok["output_tokens"]

            identity_key, email, ptype, principal_arn = (
                classify_principal(arn))
            if not identity_key:
                continue
            principals_seen.add(identity_key)

            # billing_period of THIS row (prev rows keep their own).
            row_period = usage_hour.strftime("%Y-%m")

            # JIT user upsert (email PK = identity_key for machine
            # rows; mirrors metrics_aggregator's contract). #789: same
            # in-batch-duplicate hazard — many CUR rows share one
            # identity_key, and the .first() lookups don't see this
            # session's pending User add, so a repeat re-added the same
            # user → users_pkey collision. Reuse the User we already
            # created/looked up this batch.
            u = users_added.get(identity_key)
            if u is None:
                u = (
                    db.query(User)
                    .filter(User.identity_key == identity_key)
                    .first()
                )
                if u is None and email:
                    u = db.query(User).filter(
                        User.email == email).first()
                if u is None:
                    u = User(
                        email=email or identity_key, status="active",
                        cap_usd=None)
                    db.add(u)
                users_added[identity_key] = u
            u.identity_key = identity_key
            # #950: do NOT set principal_arn/type/role_type here — the
            # last CUR row would win arbitrarily. Accumulate this row's
            # spend against its candidate role and reconcile to the
            # dominant role after the loop (see the post-loop pass).
            cand = principal_candidates.setdefault(identity_key, {})
            slot = cand.setdefault(principal_arn, {
                "spend": 0.0, "rows": 0, "raw_arn": arn,
                "ptype": ptype, "role_type": classify_role_type(arn),
            })
            slot["spend"] += spend
            slot["rows"] += 1

            # DiscoveredModel + PrincipalModel from the CUR model id.
            # #789: guard the check-then-add with an in-batch seen-set
            # so a model_id repeated across rows is added only once
            # (the .first() query can't see this batch's pending adds).
            if model_id in models_added:
                pass  # already added this batch — flush will persist it
            else:
                dm = db.query(DiscoveredModel).filter(
                    DiscoveredModel.model_id == model_id).first()
                if dm is None:
                    db.add(DiscoveredModel(
                        model_id=model_id, invocations_count=1,
                        last_seen_at=now))
                else:
                    dm.last_seen_at = now
                models_added.add(model_id)
            pm_key = (identity_key, model_id)
            if pm_key in principal_models_added:
                pass  # already added this batch
            else:
                pm = (
                    db.query(PrincipalModel)
                    .filter(
                        PrincipalModel.identity_key == identity_key,
                        PrincipalModel.model_id == model_id)
                    .first()
                )
                if pm is None:
                    db.add(PrincipalModel(
                        identity_key=identity_key, model_id=model_id,
                        last_seen_at=now))
                else:
                    pm.last_seen_at = now
                principal_models_added.add(pm_key)

            # Upsert the spend row on the unique key
            # (email, usage_hour, region, model_id). #789: distinct
            # raw principal ARNs can classify to the SAME identity_key
            # (e.g. two machine-role instances → role:<R>), so this
            # key recurs in one batch and a check-then-add would
            # collide on the unique constraint like the model rows did.
            # Those are separate billed amounts for the same governed
            # principal, so ACCUMULATE them into one row. Track the
            # pending/looked-up object per key so repeats sum onto it
            # instead of re-adding (the .first() can't see this
            # session's un-flushed adds).
            spend_key = (identity_key, usage_hour, region, model_id)
            row_obj = spend_rows.get(spend_key)
            if row_obj is None:
                row_obj = (
                    db.query(CurUserSpend)
                    .filter(
                        CurUserSpend.email == identity_key,
                        CurUserSpend.usage_hour == usage_hour,
                        CurUserSpend.region == region,
                        CurUserSpend.model_id == model_id)
                    .first()
                )
                if row_obj is None:
                    row_obj = CurUserSpend(
                        email=identity_key,
                        identity_arn=arn,
                        usage_hour=usage_hour,
                        region=region,
                        model_id=model_id,
                        spend_usd=spend,
                        billing_period=row_period,
                        data_source="cur",
                        # #806: this row's token dimension (the other
                        # dimensions for the same key sum on below).
                        **tok,
                    )
                    db.add(row_obj)
                else:
                    # First time we touch a pre-existing (prev-month)
                    # row this batch: replace, don't add to stale value.
                    row_obj.spend_usd = spend
                    row_obj.identity_arn = arn
                    row_obj.billing_period = row_period
                    # #806: reset token columns to THIS dimension's
                    # counts (a stale prev-month value must not persist;
                    # other dimensions for the key accumulate below).
                    for k, v in tok.items():
                        setattr(row_obj, k, v)
                spend_rows[spend_key] = row_obj
            else:
                # Same key already seen this batch → sum the separate
                # billed amount onto the one row. #806: each token
                # dimension (input / output / cache-read / cache-write)
                # is its OWN CUR row for the same (principal,hour,region,
                # model) key, so summing here is exactly how the four
                # columns get populated onto the single spend row.
                row_obj.spend_usd += spend
                for k, v in tok.items():
                    setattr(row_obj, k, (getattr(row_obj, k) or 0) + v)
            written += 1

        # #950: reconcile each identity's principal_arn to the role CUR
        # actually billed the most — the role real traffic uses, which
        # is what governance must attach the deny to. The per-row set
        # above was dropped because the last CUR row would win at
        # random; the dominant candidate is chosen deterministically so
        # the same CUR window always converges to the same ARN (no
        # thrash between ticks). CUR is ground truth and always wins,
        # overwriting a stale seeded ARN (e.g. a hardcoded tg-consumer)
        # or a prior admin-set value (#946 doc: "CUR observation later
        # wins if it differs").
        reconciled = 0
        for identity_key, cands in principal_candidates.items():
            u = users_added.get(identity_key)
            if u is None or not cands:
                continue
            # Dominant = max spend, then row-count, then ARN string
            # (the final key makes the choice deterministic when spend
            # AND rows tie — no reliance on dict order).
            best_arn, best = max(
                cands.items(),
                key=lambda kv: (kv[1]["spend"], kv[1]["rows"], kv[0]),
            )
            prior = u.principal_arn
            u.principal_arn = best_arn
            u.principal_type = best["ptype"]
            u.role_type = best["role_type"]
            reconciled += 1
            if prior and prior != best_arn:
                log.info(
                    "cur_spend_sync: reconciled principal_arn for %s "
                    "%s -> %s (dominant by spend=%.4f rows=%d over %d "
                    "candidate role(s))",
                    identity_key, prior, best_arn, best["spend"],
                    best["rows"], len(cands))

        db.flush()

    log.info(
        "cur_spend_sync: wrote %d spend rows for %d principals "
        "(periods %s)", written, len(principals_seen), periods)
    return {
        "status": "ok",
        "rows": written,
        "principals": len(principals_seen),
        "reconciled": reconciled,
        "periods": periods,
    }
