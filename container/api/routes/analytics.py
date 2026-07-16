from __future__ import annotations
import json
import os
import re
import time
import boto3
from botocore.exceptions import ClientError
from datetime import date, datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import AnalyticsCache, User
from api.auth import get_caller_email, Scope
import api.auth as _auth
from api.aws_errors import EXPIRED_CRED_CODES as _EXPIRED_CRED_CODES
from api.aws_session import get_aws_session

router = APIRouter()
REGION              = os.environ.get("AWS_REGION", "us-east-1")
ATHENA_WORKGROUP    = os.environ.get("ATHENA_WORKGROUP", "tg-cur-analytics")
ATHENA_DATABASE     = os.environ.get("ATHENA_DATABASE", "tg_cur")
ATHENA_RESULTS_BUCKET = os.environ.get("ATHENA_RESULTS_BUCKET", "")
# CUR_TABLE_NAME mirrors the CurTableName CFN param on
# tg-cur-athena (default 'data' — BCM CUR 2.0 leaf folder).
# Used for diagnostic messages only; the saved-query SQL
# itself is rendered server-side at CFN deploy time.
CUR_TABLE_NAME      = os.environ.get("CUR_TABLE_NAME", "data")
CACHE_TTL_SEC       = 15 * 60

# Part 2 (date range across all reports): named queries carry a
# {{DATE_FILTER}} token in their WHERE clause. /analytics/run swaps it
# for a concrete SQL predicate before running:
#   - no range  → month-to-date (preserves the historic default);
#   - range     → line_item_usage_start_date in [start, end).
# Queries WITHOUT the token (the windowed daily-trend / monthly-history
# reports, whose window IS their purpose) run verbatim — substitution
# is a no-op when the token is absent (back-compat).
_DATE_FILTER_TOKEN = "{{DATE_FILTER}}"
# #1122: any {{TOKEN}} placeholder. After all KNOWN substitutions, a
# residual match means a query carries a placeholder THIS server version
# doesn't know how to fill — almost always because the deployed app
# image is older than the report definitions (CFN named queries) it's
# serving. We fail loud (naming the token + cause) BEFORE the literal
# {{…}} reaches Athena and comes back as a cryptic `mismatched input '{'`
# (the demo0 break). Generic scan so a FUTURE token is caught too, not
# just {{DATE_FILTER}}.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
# An ISO calendar date, YYYY-MM-DD. This is the injection guard: the
# start/end values are substituted into the Athena SQL string, so they
# MUST match this exactly — never interpolate raw user input.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_FILTER_DEFAULT = (
    "bill_billing_period_start_date >= "
    "DATE_TRUNC('month', CURRENT_DATE)"
)


def _validate_iso_date(value: str, field: str) -> date:
    """Parse an ISO YYYY-MM-DD date or raise HTTP 400.

    Two-stage: the regex rejects anything not exactly YYYY-MM-DD (the
    SQL-injection guard — the value is substituted into the query
    string), then date.fromisoformat rejects impossible calendar dates
    (e.g. 2026-02-31) that the regex alone would pass.
    """
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        raise HTTPException(
            400,
            f"{field} must be an ISO date (YYYY-MM-DD)",
        )
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"{field} is not a valid date: {value}")


def _build_date_filter(start, end) -> str:
    """Return the SQL predicate to substitute for {{DATE_FILTER}}.

    No range (both blank) → month-to-date default. A range requires
    BOTH start and end, each a valid ISO date, with start <= end;
    anything else is HTTP 400 (never silently coerced — a half-given
    range is a client bug, not MTD). The range is half-open
    [start, end) on line_item_usage_start_date so an admin who picks
    "all of May" (start=2026-05-01, end=2026-06-01) gets exactly May.
    """
    has_start = bool(start)
    has_end = bool(end)
    if not has_start and not has_end:
        return _DATE_FILTER_DEFAULT
    if has_start != has_end:
        raise HTTPException(
            400, "start and end must be provided together")
    d_start = _validate_iso_date(start, "start")
    d_end = _validate_iso_date(end, "end")
    if d_start > d_end:
        raise HTTPException(400, "start must be on or before end")
    # Half-open [start, end): include the start day, exclude the end
    # day's 00:00 — but the picker's end is inclusive of that calendar
    # day to a human, so the API has already been told the convention
    # (UI sends end = the day AFTER the last wanted day is NOT assumed;
    # see the UI note). We treat end as exclusive of its own midnight,
    # i.e. the range covers [start 00:00, end 00:00). The UI labels the
    # inclusive span; callers wanting the end day included pass end+1.
    return (
        "line_item_usage_start_date >= "
        f"TIMESTAMP '{d_start.isoformat()} 00:00:00' "
        "AND line_item_usage_start_date < "
        f"TIMESTAMP '{d_end.isoformat()} 00:00:00'"
    )


def _substitute_date_filter(sql: str, start, end) -> str:
    """Swap {{DATE_FILTER}} in `sql` for the resolved predicate.

    No-op (returns sql unchanged) when the token is absent, so queries
    that don't opt into the range still run verbatim. _build_date_filter
    is still called first so an INVALID range is rejected even for a
    token-less query (a 400 on bad input is more useful than silently
    ignoring it).
    """
    predicate = _build_date_filter(start, end)
    result_sql = sql
    if _DATE_FILTER_TOKEN in sql:
        result_sql = sql.replace(_DATE_FILTER_TOKEN, predicate)
    # #1122: after all KNOWN substitutions, refuse to send SQL that still
    # carries an unfilled {{…}} placeholder — it would hit Athena as a
    # cryptic `mismatched input '{'`. This is the single chokepoint every
    # /analytics/run path flows through, so the guard covers them all.
    # Runs on BOTH branches above: a token-less query could still carry a
    # DIFFERENT unknown placeholder (the version-skew case).
    leftover = _PLACEHOLDER_RE.findall(result_sql)
    if leftover:
        tokens = ", ".join("{{%s}}" % t for t in leftover)
        raise HTTPException(
            500,
            "This report uses a template placeholder this server "
            f"version does not support ({tokens}). The deployed app "
            "image is older than the report definitions — update the "
            "container image to the version matching these Cost Reports.",
        )
    return result_sql


def _cache_key(query_id: str, start, end) -> str:
    """Range-aware cache key. Default (no range) keeps the bare
    query_id so historic cached MTD results still hit; a range appends
    |start|end so two different windows don't collide (the correctness
    bug a query_id-only key would cause)."""
    if not start and not end:
        return query_id
    return f"{query_id}|{start or ''}|{end or ''}"


# Part 3 (owner decision): pin the per-user/model token+cost report
# FIRST in the Cost Reports list and as the default-selected view, via
# an explicit featured-first sort key (NOT an alphabetical-name hack —
# a future earlier-named report would otherwise jump ahead). Add names
# to the tuple, in order, to pin more.
FEATURED_QUERIES = ("tg-bedrock-tokens-spend-by-user-model",)


def _query_sort_key(q):
    name = q.get("name", "")
    featured_rank = (
        FEATURED_QUERIES.index(name)
        if name in FEATURED_QUERIES
        else len(FEATURED_QUERIES)
    )
    return (
        featured_rank,
        0 if q.get("group") == "cur" else 1,
        name.lower(),
    )


def _db():
    with get_db() as db:
        yield db


def _scope(
    request: Request,
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


# #483: the CUR column carrying per-user identity. Present only
# when the "Include caller identity (IAM principal) allocation
# data" billing toggle is on (our own tg-cur-athena.yaml sets
# INCLUDE_IAM_PRINCIPAL_DATA=TRUE; a customer-reused CUR may
# not). Athena lowercases/underscores CUR column names.
_PRINCIPAL_COLUMN = "line_item_iam_principal"


def _principal_data_present(columns, rows) -> bool:
    """True if a query's result carries usable per-user identity.

    Two ways it can be missing: the column isn't selected at all,
    or it's selected but every value is blank (CUR exists without
    the IAM-principal allocation toggle). Either → the UI warns.
    A result that doesn't surface the column at all (most saved
    queries are aggregates that don't select it) is treated as
    present=True so we only warn on a genuine, detectable gap.
    """
    try:
        idx = columns.index(_PRINCIPAL_COLUMN)
    except ValueError:
        return True  # column not in this query → can't assess
    if not rows:
        return True  # no rows yet (CUR backfilling) → don't warn
    return any((r[idx] or "").strip() for r in rows
               if idx < len(r))


def _key_email_map(db: Session) -> dict:
    """{iam_user_name -> email} for every user who recorded a Bedrock
    API key (users.bedrock_key_user). Layer-2 display rewrite: Cost
    Reports + Activity run Athena directly and GROUP BY the raw
    line_item_iam_principal, so they'd show the opaque `user/<name>`
    key principal. This map lets _apply_key_map() rewrite a mapped
    key-user cell to the owning developer's email at DISPLAY time
    (chosen over a SQL JOIN: the map lives in Postgres, not Athena, so
    a join would mean exporting CUR-adjacent data to Athena). Empty
    when no key is mapped → the rewrite is a no-op."""
    return {
        name: em
        for (name, em) in db.query(
            User.bedrock_key_user, User.email
        ).filter(User.bedrock_key_user.isnot(None)).all()
        if name and em
    }


def _apply_key_map(columns, rows, key_map: dict):
    """Rewrite any line_item_iam_principal cell that is a mapped
    Bedrock-key IAM-user (`user/<name>` or the bare `<name>`) to the
    owning developer's email — the Layer-2 display-time attribution
    Applied to BOTH the fresh Athena result and a cache hit,
    so an admin's mapping edit takes effect immediately (the cache
    stores the raw principal; the rewrite is re-applied on every read).

    THE MATCHING RULE: rewrite ONLY a principal whose IAM-user name is
    literally in `key_map`. The map IS the discriminator — a service
    role / `AWSReservedSSO_*` session / any other principal that isn't
    a mapped key is left untouched (never a "not an email → email"
    guess). Unmapped key-users keep their raw principal. Mutates `rows`
    in place (the cells are lists) and returns rows for chaining.
    No-op when key_map is empty or the query doesn't select the
    principal column."""
    if not key_map:
        return rows
    try:
        idx = columns.index(_PRINCIPAL_COLUMN)
    except ValueError:
        return rows  # this query doesn't surface the principal column
    for r in rows:
        if idx >= len(r):
            continue
        cell = (r[idx] or "").strip()
        # CUR emits the principal as the full ARN's `user/<name>` tail
        # or (already-projected) the bare name; strip an `arn:…:user/`
        # or leading `user/` so we match on the IAM-user NAME the admin
        # stored, regardless of which form this query surfaced.
        name = cell
        if ":user/" in name:
            name = name.split(":user/", 1)[1]
        elif name.startswith("user/"):
            name = name[len("user/"):]
        mapped = key_map.get(name)
        if mapped:
            r[idx] = mapped
    return rows


def _cache_get(db: Session, query_id: str):
    # query_id here is the range-aware cache key (_cache_key) — for the
    # default MTD window it's the bare named-query id, for a picked
    # range it's "<id>|<start>|<end>". The AnalyticsCache.query_id
    # column stores whichever key applies.
    row = db.query(AnalyticsCache).filter(AnalyticsCache.query_id == query_id).first()
    if not row:
        return None
    age = (datetime.now(timezone.utc) - row.cached_at.replace(tzinfo=timezone.utc)).total_seconds()
    if age > CACHE_TTL_SEC:
        return None
    columns = json.loads(row.columns or "[]")
    rows = json.loads(row.rows or "[]")
    return {
        "columns":   columns,
        "rows":      rows,
        "row_count": row.row_count or 0,
        "execution_id": row.execution_id,
        "cached":    True,
        "cache_age_sec": int(age),
        "cache_ttl_sec": CACHE_TTL_SEC,
        "principal_data_present": _principal_data_present(
            columns, rows),
    }


def _cache_put(db: Session, query_id: str, columns, rows, execution_id):
    row = db.query(AnalyticsCache).filter(AnalyticsCache.query_id == query_id).first()
    data = {
        "columns":      json.dumps(columns),
        "rows":         json.dumps(rows),
        "row_count":    len(rows),
        "execution_id": execution_id,
        "cached_at":    datetime.now(timezone.utc),
    }
    if row:
        for k, v in data.items():
            setattr(row, k, v)
    else:
        db.add(AnalyticsCache(query_id=query_id, **data))
    db.flush()


@router.get("/analytics/queries")
def list_queries(
    scope: Scope = Depends(_scope),
):
    scope.require_org_admin()
    # #590: every Athena call runs under the api task role
    # (tg-app), not the host's `~/.aws/` profile. The user's
    # identity is recorded in the audit trail; the AWS identity
    # is uniformly the task role.
    ath = get_aws_session().client("athena")
    try:
        ids = []
        try:
            pages = ath.get_paginator(
                "list_named_queries"
            ).paginate(WorkGroup=ATHENA_WORKGROUP)
            for page in pages:
                ids.extend(page.get("NamedQueryIds", []))
        except ClientError as e:
            # The optional tg-cur-athena stack provisions the
            # workgroup; if absent, surface a structured flag
            # so the SPA can render "CUR not configured" with
            # a link to INSTALL.md instead of a 500. (#181)
            code = e.response.get(
                "Error", {}).get("Code", "")
            msg = e.response.get(
                "Error", {}).get("Message", "")
            if code == "InvalidRequestException" and \
               "WorkGroup is not found" in msg:
                return {
                    "queries": [],
                    "cur_not_configured": True,
                    "workgroup": ATHENA_WORKGROUP,
                }
            raise
        queries = []
        for qid in ids:
            try:
                q = ath.get_named_query(NamedQueryId=qid)["NamedQuery"]
            except ClientError as e:
                # Tolerate per-query failures (e.g. the named-
                # query was deleted between list + get). But
                # ExpiredToken*/InvalidClientTokenId means the
                # whole boto3 session is dead — re-raise so
                # the central handler can return 503 instead
                # of returning a silently empty list.
                code = e.response.get(
                    "Error", {}).get("Code", "")
                if code in _EXPIRED_CRED_CODES:
                    raise
                continue
            sql = q.get("QueryString", "")
            group = "cur" if "cur_" in sql.lower() else "usage"
            queries.append({
                "query_id":     qid,
                "name":         q.get("Name", ""),
                "description":  q.get("Description", ""),
                "query_string": sql,
                "group":        group,
            })
        queries.sort(key=_query_sort_key)
        return {"queries": queries}
    except ClientError:
        # Let the central exception handler in api/main.py
        # translate ExpiredToken*/InvalidClientTokenId into a
        # friendly 503. After #116 the container auto-refreshes,
        # so a 503 here means the host's cred source is broken.
        # Other ClientError codes return as 502.
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/analytics/run")
def run_query(
    body: dict,
    request: Request,
):
    # This handler takes NO DB-bound dependency (not Depends(_db)
    # and not Depends(_scope) — both hold a request-scoped pooled
    # connection for the WHOLE request). A Cost-Report run polls Athena
    # synchronously for up to 55s; holding ANY pooled connection across
    # that wait drains the pool under normal clicking (QueuePool
    # TimeoutError → every DB route 500s → 502). So auth + the
    # org-admin check + the cache read + the cache write each run in
    # their own short-lived `with get_db()` block, and the Athena poll
    # runs while holding ZERO pooled connections.
    with get_db() as db:
        # Reference _validate_request through the module (not a bound
        # import) so a test that monkeypatches api.auth._validate_request
        # is honored.
        email, _method = _auth._validate_request(request, db)
        Scope(email, db).require_org_admin()
    query_id = body.get("query_id")
    refresh  = bool(body.get("refresh"))
    if not query_id:
        raise HTTPException(400, "query_id required")

    # Part 2: optional user-selected date range. Validate NOW (before
    # cache lookup or Athena) so a bad range is a fast 400, never an
    # SQL-substituted string. _build_date_filter does the ISO + order
    # checks and raises 400; we keep the raw values for the cache key.
    start = (body.get("start") or "").strip() or None
    end   = (body.get("end") or "").strip() or None
    _build_date_filter(start, end)  # raises 400 on an invalid range
    cache_key = _cache_key(query_id, start, end)

    if not refresh:
        # Fast cache read — connection released immediately after.
        # Layer-2: re-apply the email↔key display rewrite on
        # read (the cache stores the RAW principal), so a mapped
        # key-user shows as the developer's email even from cache and
        # an admin's mapping edit takes effect without waiting for the
        # cache to expire.
        with get_db() as db:
            cached = _cache_get(db, cache_key)
            if cached:
                _apply_key_map(
                    cached.get("columns", []),
                    cached.get("rows", []),
                    _key_email_map(db))
        if cached:
            return cached

    if not ATHENA_RESULTS_BUCKET:
        # tg-cur-deploy.sh is optional — it's the deploy step that
        # sets ATHENA_RESULTS_BUCKET. Until it runs, Cost Reports
        # has nothing to query against. Return a friendly 503 (not
        # a 500) so the UI can render a "Cost Reports not configured
        # — run tg-cur-deploy.sh" message instead of a stack trace.
        # See #92 for the original taxonomy.
        raise HTTPException(
            503,
            "Cost Reports not configured. Run tg-cur-deploy.sh "
            "to enable Athena-backed CUR analytics.",
        )

    # #590: see comment at the head of /analytics/queries
    # — Athena queries always run under the api task role (tg-app).
    ath = get_aws_session().client("athena")
    try:
        named = ath.get_named_query(NamedQueryId=query_id)["NamedQuery"]
    except ClientError as e:
        # Let expired-cred errors bubble up to the central
        # exception handler (→ 503). Real "query not found"
        # cases stay 404.
        code = e.response.get(
            "Error", {}).get("Code", "")
        if code in _EXPIRED_CRED_CODES:
            raise
        raise HTTPException(404, str(e))

    # Part 2: swap {{DATE_FILTER}} for the resolved predicate. No-op
    # for queries without the token (they keep their own window).
    query_string = _substitute_date_filter(
        named["QueryString"], start, end)

    try:
        exec_resp = ath.start_query_execution(
            QueryString=query_string,
            QueryExecutionContext={"Database": ATHENA_DATABASE},
            WorkGroup=ATHENA_WORKGROUP,
            ResultConfiguration={"OutputLocation": ATHENA_RESULTS_BUCKET},
        )
    except ClientError:
        # Expired or any other AWS error: central handler.
        raise

    execution_id = exec_resp["QueryExecutionId"]
    deadline = time.time() + 55  # generous — no Lambda timeout
    state = "QUEUED"
    while time.time() < deadline:
        status = ath.get_query_execution(QueryExecutionId=execution_id)
        state = status["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status["QueryExecution"]["Status"].get("StateChangeReason", state)
            # Friendlier message for the common cause: CUR
            # data hasn't landed yet. The Glue table
            # populates after the crawler runs (~24-48h after
            # tg-cur-athena deploy). Source DB + table from
            # env so the message matches whichever names this
            # stack actually deployed (defaults: tg_cur.data).
            if "TABLE_NOT_FOUND" in reason or \
               "does not exist" in reason.lower():
                raise HTTPException(
                    503,
                    f"CUR data not ready yet. The Glue "
                    f"table {ATHENA_DATABASE}."
                    f"{CUR_TABLE_NAME} populates 24-48h "
                    f"after tg-cur-athena deploy, once AWS "
                    f"Billing delivers the first CUR "
                    f"export. (Athena: {reason})",
                )
            raise HTTPException(
                500, f"Query {state}: {reason}")
        time.sleep(1)

    if state != "SUCCEEDED":
        return {"columns": [], "rows": [], "row_count": 0, "execution_id": execution_id, "still_running": True, "cached": False}

    results = ath.get_query_results(QueryExecutionId=execution_id, MaxResults=1000)
    result_rows = results.get("ResultSet", {}).get("Rows", [])
    columns = [c.get("VarCharValue", "") for c in result_rows[0].get("Data", [])] if result_rows else []
    rows = [[c.get("VarCharValue", "") for c in r.get("Data", [])] for r in result_rows[1:]]

    out = {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "execution_id": execution_id,
        "cached": False,
        # #483: per-user attribution needs the CUR's
        # line_item_iam_principal column (the "Include caller
        # identity (IAM principal) allocation data" billing
        # toggle — see _principal_data_present). When a query
        # surfaces that column but every value is blank, the CUR
        # exists without principal data; the UI warns instead of
        # showing a silently-unattributed report.
        "principal_data_present": _principal_data_present(
            columns, rows),
    }
    # Persist to the cache in a fresh short-lived session (the
    # Athena poll above ran with no DB connection held; re-open one only
    # now, for the fast write). Keyed on the range-aware cache key so a
    # picked range doesn't collide with the default-MTD cache entry.
    # principal_data_present is recomputed from cached columns/rows on
    # read — no AnalyticsCache schema change.
    # Cache the RAW rows (principal unchanged), THEN apply the Layer-2
    # email↔key display rewrite to the response only — so the
    # cache stays source-of-truth-raw and the rewrite re-derives from
    # the current mapping on every read (fresh + cached alike).
    with get_db() as db:
        _cache_put(db, cache_key, columns, rows, execution_id)
        _apply_key_map(out["columns"], out["rows"], _key_email_map(db))
    return out
