"""
CUR health endpoint (#726, #720 slice 4).

A single org-admin probe that classifies the CUR/Athena spend
source into one of four states so the UI can show the right
banner — REUSING the analytics module's Athena session +
_principal_data_present so there's one definition of
"caller-identity attribution present".

CUR is now the SOLE spend source (#720), so its health IS the
spend pipeline's health. Read-only; no new IAM surface (same
Athena perms analytics.py already uses).
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from sqlalchemy import func

from db.session import get_db
from db.models import CurUserSpend
from api.auth import get_caller_email, Scope
from api.aws_session import get_aws_session
from api.aws_errors import EXPIRED_CRED_CODES as _EXPIRED_CRED_CODES
from api.routes import analytics as _an

log = logging.getLogger("api.cur")

router = APIRouter()

# Health states (forward-only note: the current billing month
# heals on the next overwrite; closed months don't — #714).
ABSENT = "absent"          # Glue table unresolved → no CUR
COLUMNS_MISSING = "columns_missing"   # table exists, key cols absent
PRINCIPAL_BLANK = "principal_blank"   # column delivered, 100% blank
# #749: the principal column is absent from the DELIVERED Parquet
# (the export's manifest lacks it) — distinct from present-but-blank.
# Athena projects NULL for a static-schema column the file omits, so
# the Glue query alone can't tell the two apart; the S3 manifest can.
# This state needs an export delete+recreate (infra/#742); a blank
# column just needs to wait for the next CUR delivery.
PRINCIPAL_ABSENT_FROM_EXPORT = "principal_absent_from_export"
HEALTHY = "healthy"

# The columns the spend pipeline requires: the IAM-principal
# attribution column (#714) and the unblended cost.
_REQUIRED = (_an._PRINCIPAL_COLUMN, "line_item_unblended_cost")


def _db():
    with get_db() as db:
        yield db


def _scope(
    request: Request,
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


def _probe() -> tuple[list[str], list[list[str]], str | None]:
    """Run a tiny probe over the CUR table. Returns
    (columns, rows, error_kind). error_kind is 'absent' when the
    Glue table doesn't resolve, else None (or re-raises on an
    expired-cred error so the central handler turns it into 503)."""
    from botocore.exceptions import ClientError
    import time
    ath = get_aws_session().client("athena")
    # #784: scope the probe to the CURRENT billing month and surface
    # any attributed row first. The old `LIMIT 50` with no date filter
    # / no ordering sampled arbitrarily across ALL history — and CUR
    # only heals the current month forward (closed months keep their
    # pre-attribution blanks, #714). On any account with pre-toggle
    # history the blank closed-month rows outnumber and outvote the
    # attributed current-month rows, so the sample came back all-blank
    # → cur_health=principal_blank → the whole spend pipeline behaved
    # as if attribution were OFF ($0 everywhere) despite a 100%-
    # attributed current month. Filtering to this month asks the right
    # question ("is the current month attributed?"); ORDER BY non-blank
    # first guarantees an attributed row lands in the sample if one
    # exists. The "" + IS NOT NULL pair covers both blank forms.
    sql = (
        f'SELECT {_an._PRINCIPAL_COLUMN}, line_item_unblended_cost '
        f'FROM "{_an.ATHENA_DATABASE}"."{_an.CUR_TABLE_NAME}" '
        f"WHERE line_item_product_code = 'AmazonBedrockService' "
        f"AND line_item_usage_start_date >= "
        f"date_trunc('month', current_date) "
        f"ORDER BY CASE WHEN {_an._PRINCIPAL_COLUMN} IS NULL "
        f"OR {_an._PRINCIPAL_COLUMN} = '' THEN 1 ELSE 0 END "
        f"LIMIT 50"
    )
    try:
        exec_resp = ath.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": _an.ATHENA_DATABASE},
            WorkGroup=_an.ATHENA_WORKGROUP,
            ResultConfiguration={
                "OutputLocation": _an.ATHENA_RESULTS_BUCKET},
        )
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        if code in _EXPIRED_CRED_CODES:
            raise
        return [], [], ABSENT

    execution_id = exec_resp["QueryExecutionId"]
    deadline = time.time() + 30
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
            low = reason.lower()
            if ("table_not_found" in low or "does not exist" in low
                    or "schema" in low):
                return [], [], ABSENT
            # A column-not-found failure means the table exists but
            # lacks a required column.
            if "column" in low and "cannot be resolved" in low:
                return [], [], COLUMNS_MISSING
            return [], [], ABSENT
        time.sleep(1)
    if state != "SUCCEEDED":
        return [], [], None

    results = ath.get_query_results(
        QueryExecutionId=execution_id, MaxResults=100)
    rs = results.get("ResultSet", {}).get("Rows", [])
    columns = (
        [c.get("VarCharValue", "") for c in rs[0].get("Data", [])]
        if rs else [])
    rows = [
        [c.get("VarCharValue", "") for c in r.get("Data", [])]
        for r in rs[1:]
    ]
    return columns, rows, None


# ── #749: S3 manifest column-set probe ──────────────────────────────
#
# BCM Data Exports (CUR 2.0) writes, per billing period, a manifest
# JSON alongside the data that lists the columns actually delivered in
# the Parquet:
#
#   s3://<bucket>/<export>/<export>/metadata/
#       BILLING_PERIOD=YYYY-MM/<export>-Manifest.json
#       (also a generic ...-Manifest.json — we match either)
#
# The data lands under  s3://<bucket>/<export>/<export>/data/  — which
# is exactly the Glue table's StorageDescriptor.Location. So we derive
# the bucket/prefix from Glue (glue:GetTable, already granted) rather
# than hardcoding a bucket or threading new env. The manifest read
# uses s3:GetObject on the same CUR bucket the app already reads
# (S3ReadForCurAndAthena Sid covers metadata/ — it's bucket-wide).


def _data_location() -> str | None:
    """The CUR table's S3 data location (…/<export>/data/) from the
    Glue catalog. None if Glue can't be reached / has no location."""
    try:
        glue = get_aws_session().client("glue")
        resp = glue.get_table(
            DatabaseName=_an.ATHENA_DATABASE, Name=_an.CUR_TABLE_NAME)
        return (resp.get("Table", {})
                .get("StorageDescriptor", {})
                .get("Location") or None)
    except Exception as e:  # noqa: BLE001 — best-effort probe
        log.info("cur manifest: glue get_table failed: %s", e)
        return None


def _metadata_prefix(data_location: str) -> tuple[str, str] | None:
    """(bucket, metadata_prefix) derived from the …/data/ location by
    swapping the trailing `data/` segment for `metadata/`. None if the
    location isn't the expected s3://…/data/ shape."""
    if not data_location.startswith("s3://"):
        return None
    rest = data_location[len("s3://"):]
    bucket, _, key = rest.partition("/")
    key = key.rstrip("/")
    if not bucket or not key.endswith("data"):
        return None
    meta_key = key[: -len("data")] + "metadata"
    return bucket, meta_key


def _latest_manifest_key(s3, bucket: str, meta_prefix: str) -> str | None:
    """The newest `*-Manifest.json` under the metadata/ prefix. Newest
    = largest BILLING_PERIOD= partition (lexical sort works on the
    YYYY-MM format)."""
    keys: list[str] = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": meta_prefix + "/"}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            k = o.get("Key", "")
            if k.endswith("-Manifest.json") or k.endswith("Manifest.json"):
                keys.append(k)
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
        if not token:
            break
    return sorted(keys)[-1] if keys else None


def _manifest_columns() -> list[str] | None:
    """The column names the delivered CUR Parquet actually carries,
    per the latest billing-period manifest. None when the manifest
    can't be located/read (so the caller falls back to the Glue-only
    classification — never a false 'absent'). Best-effort and fully
    fail-soft: any error → None."""
    loc = _data_location()
    if not loc:
        return None
    parsed = _metadata_prefix(loc)
    if not parsed:
        return None
    bucket, meta_prefix = parsed
    try:
        s3 = get_aws_session().client("s3")
        key = _latest_manifest_key(s3, bucket, meta_prefix)
        if not key:
            return None
        obj = s3.get_object(Bucket=bucket, Key=key)
        doc = json.loads(obj["Body"].read())
    except Exception as e:  # noqa: BLE001 — best-effort probe
        log.info("cur manifest: read failed (%s): %s", bucket, e)
        return None
    # CUR 2.0 manifest: {"columns": [{"name": "...", ...}, ...]}.
    # Athena lowercases/underscores names; the manifest stores them
    # in the delivered form, which for CUR 2.0 is already
    # snake_case (line_item_iam_principal). Compare case-insensitively
    # to be safe.
    cols = doc.get("columns") or doc.get("Columns") or []
    names = [
        str(c.get("name") or c.get("Name") or "").lower()
        for c in cols if isinstance(c, dict)
    ]
    return [n for n in names if n] or None


def cur_health_result() -> dict:
    """Plain-function core of the CUR health classification: run the
    probe + manifest disambiguation and return {status, detail} where
    status is one of absent / columns_missing / principal_blank /
    principal_absent_from_export / healthy. No FastAPI/DI — safe to
    call from the diagnostics check-engine as well as the route wrapper
    below, so the CLI/web/API can never disagree on CUR health. The
    last two states split the old single 'principal_blank' into
    present-but-empty (wait for the next delivery) vs absent-from-the-
    delivered-Parquet (re-create the export) using the S3 manifest
    column-set. Read-only (Athena/Glue/S3 Get/List only)."""
    columns, rows, err = _probe()

    if err == ABSENT:
        return {
            "status": ABSENT,
            "detail": (
                "Spend tracking unavailable — the CUR Glue table "
                "could not be queried. Deploy CUR (tg-cur-athena) "
                "to enable per-user spend."),
        }
    if err == COLUMNS_MISSING:
        return {
            "status": COLUMNS_MISSING,
            "detail": (
                "CUR exists but is missing required columns "
                f"({', '.join(_REQUIRED)}). Re-export with the "
                "IAM-principal allocation + Bedrock usage data."),
        }

    # Table resolved. Is the principal column populated? Reuse the
    # analytics definition so there's one source of truth.
    if not _an._principal_data_present(columns, rows):
        # #749: blank in Glue is ambiguous — the column may be absent
        # from the delivered Parquet (NULL-projected by the static
        # schema) or present-but-empty. The manifest disambiguates.
        # Manifest unreadable → fall back to the present-but-empty
        # copy (never a false 'absent_from_export').
        manifest_cols = _manifest_columns()
        if (manifest_cols is not None
                and _an._PRINCIPAL_COLUMN not in manifest_cols):
            return {
                "status": PRINCIPAL_ABSENT_FROM_EXPORT,
                "detail": (
                    "The delivered CUR file's schema lacks the "
                    f"{_an._PRINCIPAL_COLUMN} column — the export "
                    "predates the IAM-principal allocation setting "
                    "and must be deleted and re-created (an empty "
                    "value won't backfill into a column the Parquet "
                    "doesn't carry)."),
            }
        return {
            "status": PRINCIPAL_BLANK,
            "detail": (
                "Caller-identity attribution is OFF — the CUR's "
                f"{_an._PRINCIPAL_COLUMN} column is delivered but "
                "empty. If you just enabled 'Include caller identity "
                "(IAM principal) allocation data', the current "
                "billing month heals on the next CUR delivery "
                "(≤24h); already-closed months stay unattributed."),
        }

    return {"status": HEALTHY, "detail": None}


@router.get("/cur/health")
def cur_health(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Classify the CUR spend source. org-admin scoped. Thin wrapper
    over cur_health_result() so the route and the diagnostics engine
    share one definition of CUR health."""
    scope.require_org_admin()
    return cur_health_result()


@router.get("/cur/data-through")
def cur_data_through(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """#737 (#726 follow-up): the freshness watermark backing every
    spend figure — the newest usage_hour in cur_user_spend (CUR is
    the sole spend source, #720). The spend pages stamp "spend
    current as of <data_through>" from this so the timestamp is
    server-derived, never a client-side guess. null when no CUR
    spend has landed yet (page shows the empty state instead).
    org-admin scoped (same as the spend surfaces)."""
    scope.require_org_admin()
    latest = db.query(func.max(CurUserSpend.usage_hour)).scalar()
    return {"data_through": latest.isoformat() if latest else None}
