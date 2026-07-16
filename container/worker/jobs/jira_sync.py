"""
jira_sync — fetches Jira issue metadata into jira_issues.

Runs at TG_JIRA_SYNC_INTERVAL_MIN (default 15). Reads
unique issue keys from pr_jira_refs and pulls them in
batches of <=100 via /rest/api/3/search using JQL
`key in (KEY1,KEY2,...)`. Stale-cache: rows whose
last_synced_at is < (now - 1h) are refetched; rows
that never synced are always pulled.

Auth: HTTP Basic with the site's `auth_email` + an
API token. Token is read from Secrets Manager
(`tg/jira/<site-host>/api-token`) when AWS creds are
available, falling back to `jira_sites.api_token_plain`
for local dev. Read-only scopes only — this job never
writes to Jira.

Status semantics on jira_sites.sync_status:
  ok          — last tick succeeded
  paused      — server returned 429; next tick respects Retry-After
  auth_failed — server returned 401/403 on /myself or /search
"""
from __future__ import annotations
import base64
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from db.session import get_db
from db.models import JiraIssue, JiraSite, PrJiraRef
from db.jira_feature import is_jira_enabled

log = logging.getLogger("worker.jira_sync")

PAGE_SIZE = 100        # Jira allows 50–100 per /search call
HARD_CAP = 1000        # bound per-tick fetch to avoid runaway
STALE_AFTER = timedelta(hours=1)

# Synthetic sites created by the jira_synth_seed worker job carry
# this added_by marker. They have no real Atlassian endpoint or
# token, so jira_sync must skip them outright — otherwise the
# no-token branch flips sync_status to auth_failed and the V&C
# Jira surface (which gates on sync_status='ok') goes dark.
SYNTH_ADDED_BY = "jira_synth_seed"


def _site_host(site_url: str) -> str:
    parts = urlsplit(site_url)
    return parts.hostname or site_url


def _read_site_token(
    arn: str | None,
    plaintext: str | None,
    site_url: str,
) -> str | None:
    if arn:
        try:
            import boto3
            sm = boto3.client(
                "secretsmanager",
                region_name=os.environ.get(
                    "AWS_REGION", "us-east-1"),
            )
            r = sm.get_secret_value(SecretId=arn)
            payload = r.get("SecretString") or "{}"
            try:
                obj = json.loads(payload)
                tok = obj.get("token") or obj.get("api_token")
            except Exception:
                tok = payload
            if tok:
                return tok
        except Exception as e:
            log.warning(
                "Jira SM read failed for %s: %s",
                site_url, e,
            )
    if plaintext:
        log.warning(
            "Jira site %s using plaintext token fallback "
            "— rotate to Secrets Manager before pilot.",
            site_url,
        )
        return plaintext
    return None


def _basic_auth(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _jira_request(
    site_url, path, auth, method="GET",
):
    headers = {
        "Authorization": auth,
        "Accept": "application/json",
        "User-Agent": "tg-admin",
    }
    full = site_url.rstrip("/") + path
    req = urllib.request.Request(
        full, headers=headers, method=method,
    )
    return urllib.request.urlopen(req, timeout=20)


def _parse_dt(raw: str | None):
    if not raw:
        return None
    try:
        # Jira returns "2024-01-02T03:04:05.000+0000"; the
        # naive +HHMM (no colon) needs normalising for
        # fromisoformat in py<3.11.
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        if len(raw) >= 5 and raw[-5] in "+-" and \
                raw[-3] != ":":
            raw = raw[:-2] + ":" + raw[-2:]
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _email_from_assignee(node: dict | None) -> str | None:
    if not node:
        return None
    # `emailAddress` may be redacted by Jira's privacy
    # settings; record null + log warning.
    e = node.get("emailAddress")
    if e:
        return e
    return None


def _flatten_issue(issue: dict) -> dict | None:
    fields = issue.get("fields") or {}
    issue_type = ((fields.get("issuetype") or {}).get("name")
                  or "Unknown")
    status = (fields.get("status") or {}).get("name") or ""
    cat = ((fields.get("status") or {}).get(
        "statusCategory") or {}).get("key") or ""
    assignee = _email_from_assignee(fields.get("assignee"))
    reporter = _email_from_assignee(fields.get("reporter"))
    parent = fields.get("parent") or {}
    parent_key = parent.get("key")
    sprint_id = None
    sprint_name = None
    sprints = (
        fields.get("sprint")
        or fields.get("customfield_10020")
        or []
    )
    if isinstance(sprints, list) and sprints:
        s = sprints[-1]
        if isinstance(s, dict):
            sprint_id = s.get("id")
            sprint_name = s.get("name")
    sp = (
        fields.get("story_points")
        or fields.get("customfield_10016")
    )
    fix_versions = [
        v.get("name") for v in (fields.get("fixVersions") or [])
        if isinstance(v, dict) and v.get("name")
    ]
    labels = list(fields.get("labels") or [])
    created = _parse_dt(fields.get("created"))
    updated = _parse_dt(fields.get("updated"))
    if not created or not updated:
        return None
    return {
        "issue_key":       issue.get("key"),
        "issue_type":      issue_type,
        "summary":         fields.get("summary") or "",
        "status":          status,
        "status_category": cat,
        "priority": (
            (fields.get("priority") or {}).get("name")
        ),
        "assignee_email":  assignee,
        "reporter_email":  reporter,
        "parent_epic_key": parent_key,
        "sprint_id":       sprint_id,
        "sprint_name":     sprint_name,
        "story_points":    (
            float(sp) if isinstance(sp, (int, float)) else None
        ),
        "fix_versions":    json.dumps(fix_versions),
        "labels":          json.dumps(labels),
        "resolved_at":     _parse_dt(fields.get("resolutiondate")),
        "jira_created_at": created,
        "jira_updated_at": updated,
    }


def _upsert_issue(db, flat: dict) -> bool:
    existing = (
        db.query(JiraIssue)
        .filter(JiraIssue.issue_key == flat["issue_key"])
        .first()
    )
    if existing:
        for k, v in flat.items():
            setattr(existing, k, v)
        existing.last_synced_at = datetime.now(timezone.utc)
        return False
    db.add(JiraIssue(**flat, last_synced_at=datetime.now(
        timezone.utc)))
    return True


def _keys_to_fetch(db) -> list[str]:
    """Issue keys present in pr_jira_refs that either have
    no jira_issues row or whose row is older than STALE_AFTER.
    """
    cutoff = datetime.now(timezone.utc) - STALE_AFTER
    refs = db.query(PrJiraRef.issue_key).distinct().all()
    candidates = {r[0] for r in refs}
    if not candidates:
        return []
    have = {
        r.issue_key: r.last_synced_at
        for r in db.query(JiraIssue).all()
    }
    out: list[str] = []
    for key in sorted(candidates):
        ts = have.get(key)
        if ts is None or ts < cutoff:
            out.append(key)
    return out[:HARD_CAP]


def _search_batch(
    site_url: str, auth: str, keys: list[str],
) -> tuple[list[dict], int, str | None]:
    """Returns (issues, http_status_for_rate_limit_marker,
    retry_after_seconds_str). 0 status means "ok"."""
    jql = "key in (" + ",".join(keys) + ")"
    path = (
        "/rest/api/3/search?jql=" + quote(jql)
        + f"&maxResults={PAGE_SIZE}"
    )
    try:
        with _jira_request(site_url, path, auth) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            ra = e.headers.get("Retry-After") if e.headers else None
            return ([], 429, ra)
        if e.code in (401, 403):
            return ([], e.code, None)
        raise
    return (data.get("issues") or [], 0, None)


def _myself_probe(site_url: str, auth: str) -> int:
    """Returns HTTP-ish status: 200 ok, 401/403 fail, 0 unknown."""
    try:
        with _jira_request(site_url, "/rest/api/3/myself",
                           auth) as r:
            return r.status if r.status else 200
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _set_status(db, site_id: int, status: str):
    s = db.query(JiraSite).filter(JiraSite.id == site_id).first()
    if s:
        s.sync_status = status
        s.last_sync_at = datetime.now(timezone.utc)


def _do_daily_probe(last_sync_at) -> bool:
    """Once-per-day check unless last_sync_at is None.
    Cheap heuristic — last_sync_at older than 23h triggers."""
    if last_sync_at is None:
        return True
    age = datetime.now(timezone.utc) - last_sync_at
    return age >= timedelta(hours=23)


def run() -> dict:
    """Sync stale Jira issues for all configured sites."""
    # #447: the Jira feature is gated behind the runtime
    # admin_config flag jira_enabled (default OFF). When off,
    # skip the whole sync — mirroring the jobs_pause early-out.
    # The job_runner still records a row, so the skip is visible.
    with get_db() as db:
        if not is_jira_enabled(db):
            return {
                "detail": "skipped: jira feature disabled "
                          "(admin_config.jira_enabled)",
                "skipped": True,
                "skip_reason": "jira_disabled",
            }
    with get_db() as db:
        # Materialise plain values up front so the rest of the
        # job runs without a held session — keeps reasoning
        # simple and avoids DetachedInstanceError when boto3
        # / urllib happen between get_db() blocks.
        sites = [
            {
                "id": s.id,
                "site_url": s.site_url,
                "auth_email": s.auth_email,
                "api_token_secret_arn": s.api_token_secret_arn,
                "api_token_plain": s.api_token_plain,
                "last_sync_at": s.last_sync_at,
                "added_by": s.added_by,
            }
            for s in db.query(JiraSite).all()
        ]
    if not sites:
        return {
            "detail": "skipped: no jira_sites configured",
            "skipped": True,
            "skip_reason": "no_sites",
        }

    fetched_total = 0
    upserted_total = 0
    site_summaries: list[str] = []
    paused_sites: list[str] = []
    synthetic_skipped = 0

    for site in sites:
        site_url = site["site_url"]

        # Synthetic seed sites have no live endpoint/token. Skip
        # them entirely — no probe, no token read, no status
        # write — so jira_synth_seed's sync_status='ok' survives.
        if site["added_by"] == SYNTH_ADDED_BY:
            synthetic_skipped += 1
            site_summaries.append(
                f"{_site_host(site_url)}=synthetic_skip")
            continue

        token = _read_site_token(
            site["api_token_secret_arn"],
            site["api_token_plain"],
            site_url,
        )
        if not token:
            log.warning(
                "jira_sync: no token for %s, marking auth_failed",
                site_url,
            )
            with get_db() as db:
                _set_status(db, site["id"], "auth_failed")
            site_summaries.append(
                f"{_site_host(site_url)}=no_token")
            continue
        auth = _basic_auth(site["auth_email"], token)

        if _do_daily_probe(site["last_sync_at"]):
            mp = _myself_probe(site_url, auth)
            if mp in (401, 403):
                with get_db() as db:
                    _set_status(db, site["id"], "auth_failed")
                site_summaries.append(
                    f"{_site_host(site_url)}=auth_failed")
                continue

        with get_db() as db:
            keys = _keys_to_fetch(db)
        if not keys:
            with get_db() as db:
                _set_status(db, site["id"], "ok")
            site_summaries.append(
                f"{_site_host(site_url)}=0")
            continue

        site_fetched = 0
        site_upserted = 0
        rate_limited = False
        auth_failed = False
        idx = 0
        while idx < len(keys):
            batch = keys[idx:idx + PAGE_SIZE]
            issues, status, retry_after = _search_batch(
                site_url, auth, batch,
            )
            if status == 429:
                rate_limited = True
                log.warning(
                    "jira_sync: 429 from %s (Retry-After=%s)",
                    site_url, retry_after,
                )
                break
            if status in (401, 403):
                auth_failed = True
                log.warning(
                    "jira_sync: %d on /search for %s",
                    status, site_url,
                )
                break
            site_fetched += len(issues)
            with get_db() as db:
                for raw in issues:
                    flat = _flatten_issue(raw)
                    if not flat or not flat.get("issue_key"):
                        continue
                    if _upsert_issue(db, flat):
                        site_upserted += 1
            idx += PAGE_SIZE

        with get_db() as db:
            if auth_failed:
                _set_status(db, site["id"], "auth_failed")
            elif rate_limited:
                _set_status(db, site["id"], "paused")
                paused_sites.append(_site_host(site_url))
            else:
                _set_status(db, site["id"], "ok")

        fetched_total += site_fetched
        upserted_total += site_upserted
        site_summaries.append(
            f"{_site_host(site_url)}={site_upserted}/"
            f"{site_fetched}")

    detail = (
        f"sites={len(sites)} "
        f"upserted={upserted_total} "
        f"fetched={fetched_total} "
        f"per_site=[{','.join(site_summaries)}]"
    )
    if paused_sites:
        detail += f" paused={','.join(paused_sites)}"
    if synthetic_skipped:
        detail += f" synthetic_skipped={synthetic_skipped}"
    return {"detail": detail}
