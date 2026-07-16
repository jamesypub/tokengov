"""
Jira Integrations endpoints — issue #364.

Mirrors the GitHub integration shape: token in Secrets Manager
(`tg/jira/<site-host>/api-token`) when AWS creds are
available, plaintext fallback in `jira_sites.api_token_plain`
for local dev. Read-only Jira scopes only.

The `test` endpoint hits Jira's `/rest/api/3/myself` to confirm
auth works without making any change. The `sync-now` endpoint
runs the worker job synchronously so the admin UI can show
immediate feedback.
"""
from __future__ import annotations
import base64
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import JiraIssue, JiraSite, PrJiraRef
from api.auth import Scope, get_caller_email

logger = logging.getLogger(__name__)
router = APIRouter()


def _db():
    with get_db() as db:
        yield db


def _scope(
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


def _site_host(site_url: str) -> str:
    return urlsplit(site_url).hostname or site_url


def _secret_id(site_url: str) -> str:
    return f"tg/jira/{_site_host(site_url)}/api-token"


def _sm_client():
    try:
        import boto3
        return boto3.client(
            "secretsmanager",
            region_name=os.environ.get(
                "AWS_REGION", "us-east-1"),
        )
    except Exception as e:
        logger.warning(
            "Secrets Manager unavailable for jira: %s", e)
        return None


def _write_token_for_site(
    site: JiraSite, token: str, email: str,
):
    """Stores token via SM when available; plaintext fallback
    for local dev. Sets api_token_secret_arn or
    api_token_plain accordingly. Caller must commit."""
    payload = {
        "token": token,
        "rotated_by": email,
        "rotated_at": datetime.now(timezone.utc).isoformat(),
    }
    sm = _sm_client()
    if sm is not None:
        sec_id = _secret_id(site.site_url)
        try:
            try:
                sm.put_secret_value(
                    SecretId=sec_id,
                    SecretString=json.dumps(payload),
                )
            except sm.exceptions.ResourceNotFoundException:
                sm.create_secret(
                    Name=sec_id,
                    SecretString=json.dumps(payload),
                    Description=(
                        "tg-admin Jira API token "
                        "(issue #364)"
                    ),
                )
            site.api_token_secret_arn = sec_id
            site.api_token_plain = None
            return
        except Exception as e:
            logger.warning(
                "Jira SM write failed, falling back: %s", e)
    site.api_token_plain = token
    site.api_token_secret_arn = None


def _delete_secret_for_site(site: JiraSite):
    sm = _sm_client()
    if sm is not None and site.api_token_secret_arn:
        try:
            sm.delete_secret(
                SecretId=site.api_token_secret_arn,
                ForceDeleteWithoutRecovery=True,
            )
        except Exception as e:
            logger.warning("Jira SM delete failed: %s", e)


def _read_token_for_site(site: JiraSite) -> str | None:
    arn = site.api_token_secret_arn
    if arn:
        sm = _sm_client()
        if sm is not None:
            try:
                r = sm.get_secret_value(SecretId=arn)
                payload = r.get("SecretString") or "{}"
                try:
                    obj = json.loads(payload)
                    return (
                        obj.get("token")
                        or obj.get("api_token")
                    )
                except Exception:
                    return payload
            except Exception as e:
                logger.warning(
                    "Jira SM read failed for %s: %s",
                    site.site_url, e)
    return site.api_token_plain


def _basic_auth(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _probe(site_url: str, auth_email: str, token: str):
    auth = _basic_auth(auth_email, token)
    req = urllib.request.Request(
        site_url.rstrip("/") + "/rest/api/3/myself",
        headers={
            "Authorization": auth,
            "Accept": "application/json",
            "User-Agent": "tg-admin",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
            return {
                "ok": True,
                "account_id": data.get("accountId"),
                "display_name": data.get("displayName"),
            }
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise HTTPException(
                400, f"Jira rejected token: {e.code}",
            )
        raise HTTPException(
            502, f"Jira returned HTTP {e.code}",
        )
    except Exception as e:
        raise HTTPException(
            502, f"connectivity probe failed: {e}",
        )


def _site_to_dict(s: JiraSite, db: Session) -> dict:
    issue_count = (
        db.query(JiraIssue)
        .count()
    )
    ref_count = (
        db.query(PrJiraRef)
        .count()
    )
    try:
        projects = json.loads(s.projects or "[]")
    except Exception:
        projects = []
    return {
        "id":            s.id,
        "site_url":      s.site_url,
        "host":          _site_host(s.site_url),
        "auth_email":    s.auth_email,
        "projects":      projects,
        "sync_status":   s.sync_status,
        "last_sync_at": (
            s.last_sync_at.isoformat()
            if s.last_sync_at else None
        ),
        "added_by":      s.added_by,
        "added_at": (
            s.added_at.isoformat()
            if s.added_at else None
        ),
        "token_storage": (
            "secrets_manager"
            if s.api_token_secret_arn else
            ("plaintext" if s.api_token_plain else "missing")
        ),
        "issue_count": issue_count,
        "ref_count":   ref_count,
    }


def _validate_site_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise HTTPException(400, "site_url required")
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") \
            or not parts.hostname:
        raise HTTPException(
            400, "site_url must be a full https URL")
    return url


def _validate_projects(projects) -> list[str]:
    if projects is None:
        return []
    if not isinstance(projects, list):
        raise HTTPException(
            400, "projects must be a JSON array of strings")
    out: list[str] = []
    for p in projects:
        if not isinstance(p, str):
            raise HTTPException(
                400, "projects must contain strings only")
        p = p.strip().upper()
        if not p:
            continue
        if not p.replace("_", "").isalnum():
            raise HTTPException(
                400, f"invalid project key: {p}")
        out.append(p)
    return out


@router.post("/integrations/jira", status_code=201)
def create_jira_site(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    body = body or {}
    url = _validate_site_url(body.get("site_url"))
    email = (body.get("auth_email") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(400, "auth_email required")
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(400, "token required")
    projects = _validate_projects(body.get("projects"))

    existing = (
        db.query(JiraSite)
        .filter(JiraSite.site_url == url)
        .first()
    )
    if existing:
        raise HTTPException(
            409, f"site already configured: {url}")

    # Probe before persisting so a bad token is rejected.
    _probe(url, email, token)

    row = JiraSite(
        site_url=url,
        auth_email=email,
        projects=json.dumps(projects),
        sync_status="ok",
        added_by=scope.email,
    )
    db.add(row)
    db.flush()
    _write_token_for_site(row, token, scope.email)
    db.flush()
    return _site_to_dict(row, db)


@router.get("/integrations/jira")
def list_jira_sites(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    rows = (
        db.query(JiraSite)
        .order_by(JiraSite.id)
        .all()
    )
    return [_site_to_dict(r, db) for r in rows]


@router.patch("/integrations/jira/{site_id}")
def update_jira_site(
    site_id: int,
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    row = (
        db.query(JiraSite)
        .filter(JiraSite.id == site_id)
        .first()
    )
    if not row:
        raise HTTPException(404, f"site not found: {site_id}")
    body = body or {}
    if "projects" in body:
        row.projects = json.dumps(
            _validate_projects(body["projects"]))
    if "auth_email" in body:
        em = (body["auth_email"] or "").strip()
        if not em or "@" not in em:
            raise HTTPException(400, "auth_email required")
        row.auth_email = em
    if "token" in body and body["token"]:
        token = body["token"].strip()
        _probe(row.site_url, row.auth_email, token)
        _write_token_for_site(row, token, scope.email)
    db.flush()
    return _site_to_dict(row, db)


@router.delete("/integrations/jira/{site_id}", status_code=200)
def delete_jira_site(
    site_id: int,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    row = (
        db.query(JiraSite)
        .filter(JiraSite.id == site_id)
        .first()
    )
    if not row:
        raise HTTPException(404, f"site not found: {site_id}")
    _delete_secret_for_site(row)
    db.delete(row)
    db.flush()
    return {"detail": "deleted"}


@router.post("/integrations/jira/{site_id}/test")
def test_jira_site(
    site_id: int,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    row = (
        db.query(JiraSite)
        .filter(JiraSite.id == site_id)
        .first()
    )
    if not row:
        raise HTTPException(404, f"site not found: {site_id}")
    token = _read_token_for_site(row)
    if not token:
        raise HTTPException(
            400, "no token stored for this site")
    return _probe(row.site_url, row.auth_email, token)


@router.post("/integrations/jira/{site_id}/sync-now")
def sync_jira_site_now(
    site_id: int,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Runs jira_sync synchronously, wrapped in JobRun so the
    Jobs page shows it. The job processes ALL sites in one
    pass — site_id is accepted for symmetry with the GitHub
    flow but not used to scope the underlying job."""
    scope.require_org_admin()
    row = (
        db.query(JiraSite)
        .filter(JiraSite.id == site_id)
        .first()
    )
    if not row:
        raise HTTPException(404, f"site not found: {site_id}")
    from worker.jobs.jira_sync import run as run_sync
    from worker.job_runner import job as wrap_job
    wrapped = wrap_job("jira_sync", run_sync)
    result = wrapped(triggered_by=scope.email)
    return {"detail": result.get("detail")}
