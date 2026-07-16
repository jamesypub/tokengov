"""
GitHub Integrations endpoints — issue #213.

Token storage: AWS Secrets Manager at `tg/github/default-pat`
when AWS creds are available; falls back to plaintext in
admin_config.github_default_pat for local dev when SM is not
reachable. The fallback path is chatty in logs so we don't
silently store tokens in DB on a misconfig.
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import (
    AdminConfig, GithubActivity, GithubRepo,
)
from api.auth import Scope, get_caller_email

logger = logging.getLogger(__name__)
router = APIRouter()

SM_NAME = "tg/github/default-pat"
DEFAULT_LABEL_MAP = {
    "story": ["feature", "enhancement", "epic",
              "type:feat", "user-story"],
    "bug":   ["bug", "defect", "regression",
              "hotfix", "type:bug"],
    "task":  ["chore", "docs", "refactor",
              "tech-debt", "deps"],
}


def _db():
    with get_db() as db:
        yield db


def _scope(
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


def _sm_client():
    try:
        import boto3
        return boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
    except Exception as e:
        logger.warning("Secrets Manager unavailable: %s", e)
        return None


def _read_token() -> tuple[str | None, datetime | None]:
    """Returns (token, connected_at). Tries SM first."""
    sm = _sm_client()
    if sm is not None:
        try:
            r = sm.get_secret_value(SecretId=SM_NAME)
            payload = json.loads(r.get("SecretString") or "{}")
            tok = payload.get("token")
            connected_at = payload.get("connected_at")
            ts = (
                datetime.fromisoformat(connected_at)
                if connected_at else None
            )
            return tok, ts
        except sm.exceptions.ResourceNotFoundException:
            pass
        except Exception as e:
            logger.warning("SM read failed, falling back: %s", e)

    with get_db() as db:
        row = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == "github_default_pat")
            .first()
        )
        raw_value = row.value if row else None
        raw_updated = row.updated_at if row else None
    if not raw_value:
        return None, None
    try:
        payload = json.loads(raw_value)
        tok = payload.get("token")
        ts = (
            datetime.fromisoformat(payload["connected_at"])
            if payload.get("connected_at") else None
        )
        return tok, ts
    except Exception:
        return raw_value, raw_updated


def _write_token(token: str, email: str):
    payload = {
        "token": token,
        "connected_at": datetime.now(timezone.utc).isoformat(),
        "rotated_by": email,
    }
    sm = _sm_client()
    if sm is not None:
        try:
            try:
                sm.put_secret_value(
                    SecretId=SM_NAME,
                    SecretString=json.dumps(payload),
                )
            except sm.exceptions.ResourceNotFoundException:
                sm.create_secret(
                    Name=SM_NAME,
                    SecretString=json.dumps(payload),
                    Description=(
                        "tg-admin GitHub PAT for V&C "
                        "integrations (issue #213)"
                    ),
                )
            return
        except Exception as e:
            logger.warning(
                "SM write failed, falling back to DB: %s", e)

    with get_db() as db:
        row = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == "github_default_pat")
            .first()
        )
        if row:
            row.value = json.dumps(payload)
        else:
            db.add(AdminConfig(
                key="github_default_pat",
                value=json.dumps(payload),
            ))
        db.flush()


def _delete_token():
    sm = _sm_client()
    if sm is not None:
        try:
            sm.delete_secret(
                SecretId=SM_NAME,
                ForceDeleteWithoutRecovery=True,
            )
        except Exception as e:
            logger.warning("SM delete failed: %s", e)
    with get_db() as db:
        db.query(AdminConfig).filter(
            AdminConfig.key == "github_default_pat",
        ).delete()
        db.flush()


def _probe(token: str):
    """Cheap GitHub auth probe: GET /user."""
    import urllib.request, urllib.error
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"token {token}",
            "User-Agent": "tg-admin",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise HTTPException(
                400,
                f"GitHub rejected token: {e.code}",
            )
        raise
    except Exception as e:
        raise HTTPException(
            502, f"connectivity probe failed: {e}",
        )


@router.get("/integrations/github/token")
def get_github_token(
    scope: Scope = Depends(_scope),
):
    scope.require_org_admin()
    tok, ts = _read_token()
    if not tok:
        return {
            "connected": False, "last4": None,
            "connected_at": None, "repo_count": 0,
        }
    last4 = tok[-4:] if len(tok) > 4 else None
    with get_db() as db:
        repo_count = db.query(GithubRepo).count()
    return {
        "connected": True,
        "last4": last4,
        "connected_at": ts.isoformat() if ts else None,
        "repo_count": repo_count,
        "is_seed_placeholder":
            tok == "SEED_PLACEHOLDER_NEEDS_ROTATE",
    }


@router.put("/integrations/github/token", status_code=204)
def put_github_token(
    body: dict,
    scope: Scope = Depends(_scope),
):
    scope.require_org_admin()
    token = (body or {}).get("token", "").strip()
    if not token:
        raise HTTPException(400, "token required")
    if token != "SEED_PLACEHOLDER_NEEDS_ROTATE":
        _probe(token)
    _write_token(token, scope.email)
    return None


@router.delete("/integrations/github/token", status_code=204)
def delete_github_token(
    scope: Scope = Depends(_scope),
):
    scope.require_org_admin()
    _delete_token()
    return None


import re
from api.repo_url import normalize_repo, RepoParseError
# Legacy 2-segment shorthand check, kept for reference; the canonical
# accept-grammar now lives in api.repo_url.normalize_repo (#1042).
_REPO_RE = re.compile(r'^[\w.\-]+/[\w.\-]+$')


def _pat_last4(r) -> str | None:
    """#1043: a non-secret hint for the per-repo override token. The
    full token is NEVER returned to the client — only the last 4 of the
    dev plaintext fallback, or a generic marker when it lives in SM."""
    plain = getattr(r, "pat_plain", None)
    if plain and plain != "SEED_PLACEHOLDER_NEEDS_ROTATE":
        return plain[-4:]
    if getattr(r, "pat_secret_arn", None):
        return "????"  # stored in Secrets Manager; last4 not surfaced
    return None


def _repo_row(r, db) -> dict:
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ) - timedelta(days=30)
    pr_count = (
        db.query(GithubActivity)
        .filter(
            GithubActivity.repo == r.repo,
            GithubActivity.merged_at >= cutoff,
        )
        .count()
    )
    host = r.host or "github.com"
    return {
        "repo": r.repo,
        "host": host,
        "path": r.path or r.repo,
        "is_github": host == "github.com",
        "team_id": r.team_id,
        # #1043: token_kind is the DERIVED status (public|override|org|
        # missing|unprobed); token_mode is the admin's choice
        # (auto|override|public). pat_last4 is a non-secret hint.
        "token_kind": r.token_kind,
        "token_mode": getattr(r, "token_mode", "auto") or "auto",
        "pat_last4": _pat_last4(r),
        "is_public": getattr(r, "is_public", None),
        "sync_status": r.sync_status,
        "last_sync_at": (
            r.last_sync_at.isoformat()
            if r.last_sync_at else None
        ),
        "pr_count_30d": pr_count,
    }


def _org_default_present() -> bool:
    """#1043: is an org-default PAT configured? (token, _) tuple."""
    try:
        tok, _ = _read_token()
        return bool(tok)
    except Exception:  # noqa: BLE001
        return False


def _probe_public_visibility(repo_full_name: str) -> bool | None:
    """#1043: add-time unauthenticated GET /repos/{owner}/{name}.
    200 → public (True), 404 → private/nonexistent (False), any other
    error → None (unprobed; the worker re-probes on Sync). Read-only,
    host is always api.github.com — no SSRF surface."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo_full_name}",
        headers={
            "User-Agent": "tg-admin",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        return None
    except Exception:  # noqa: BLE001
        return None


@router.post("/integrations/github/repos", status_code=201)
def create_github_repo(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    raw = (body or {}).get("repo", "")
    # #1042: URL-first — accept a full URL / SCP-SSH / owner-name and
    # normalize to a canonical host/path identity. A genuinely
    # unparseable string gets a SPECIFIC 400, not "must be owner/name".
    try:
        norm = normalize_repo(raw)
    except RepoParseError as e:
        raise HTTPException(400, str(e))
    canonical = norm["canonical"]
    # PK / display key:
    #   * github.com → the BARE `owner/name` (norm.path). The sync
    #     worker uses `repo` directly as the GitHub REST path AND as the
    #     github_activity join key, and existing rows are already keyed
    #     this way — so github rows stay bare for sync + parity.
    #   * non-github → the canonical `host/path` (there is no bare form;
    #     these are never synced, so the key is identity/display only).
    repo_key = norm["path"] if norm["is_github"] else canonical
    # Dedup against the legacy bare key, the canonical, and the new key.
    existing = db.query(GithubRepo).filter(
        GithubRepo.repo.in_({repo_key, canonical, norm["path"]}),
    ).first()
    if existing:
        raise HTTPException(
            409, f"repo already tracked: {existing.repo}")
    team_id = (body or {}).get("team_id") or None
    # Non-github hosts are stored but visibly do nothing yet: sync is
    # github.com-gated (#1042 non-goal). paused + missing token so the
    # UI shows it's parked, and no code path pretends to fetch it.
    if not norm["is_github"]:
        row = GithubRepo(
            repo=repo_key, host=norm["host"], path=norm["path"],
            team_id=team_id, added_by=scope.email,
            sync_status="paused", token_kind="missing",
            token_mode="auto",
        )
        db.add(row)
        db.flush()
        return _repo_row(row, db)
    # #1043: github.com row — start unprobed. Best-effort add-time
    # visibility probe so the UI shows public/needs-token immediately;
    # the worker re-probes + resolves the token tier on each Sync, so a
    # failed/inconclusive probe here just leaves it unprobed (no harm).
    is_pub = _probe_public_visibility(repo_key)
    if is_pub is True:
        kind, status = "public", "ok"
    elif is_pub is False:
        # private — does the org default exist? (kind stays a hint;
        # the worker makes the authoritative call on Sync.)
        kind = "org" if _org_default_present() else "missing"
        status = "ok" if kind == "org" else "paused"
    else:
        kind, status = "unprobed", "ok"
    row = GithubRepo(
        repo=repo_key,
        host=norm["host"],
        path=norm["path"],
        team_id=team_id,
        added_by=scope.email,
        sync_status=status,
        token_kind=kind,
        token_mode="auto",
        is_public=is_pub,
        last_probed_at=(
            datetime.now(timezone.utc) if is_pub is not None else None
        ),
    )
    db.add(row)
    db.flush()
    return _repo_row(row, db)


@router.patch(
    "/integrations/github/repos/{repo:path}",
)
def update_github_repo(
    repo: str,
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    row = db.query(GithubRepo).filter(
        GithubRepo.repo == repo,
    ).first()
    if not row:
        raise HTTPException(404, f"repo not found: {repo}")
    body = body or {}
    # team_id is only updated when the key is present (so a token-only
    # PATCH doesn't clear the team).
    if "team_id" in body:
        row.team_id = body.get("team_id") or None
    # #1043: per-repo token tier. token_mode = auto|override|public.
    # On override, a `token` sets the per-repo PAT (dev: pat_plain;
    # SM storage is the org-token shape, out of this PATCH's scope).
    # Switching away from override clears the stored PAT. Changing mode
    # resets token_kind to unprobed so the next Sync re-resolves.
    mode = body.get("token_mode")
    if mode is not None:
        if mode not in ("auto", "override", "public"):
            raise HTTPException(
                400, "token_mode must be auto|override|public")
        row.token_mode = mode
        if mode != "override":
            row.pat_plain = None
            row.pat_secret_arn = None
        # Force a re-probe / re-resolve on the next run.
        row.is_public = None
        row.last_probed_at = None
        row.token_kind = "unprobed"
        row.sync_status = "ok"
    if "token" in body:
        tok = (body.get("token") or "").strip()
        if tok:
            # Setting a per-repo token implies override mode.
            row.token_mode = "override"
            row.pat_plain = tok
            row.token_kind = "override"
            row.sync_status = "ok"
        else:
            row.pat_plain = None
            row.pat_secret_arn = None
    db.flush()
    return _repo_row(row, db)


@router.delete(
    "/integrations/github/repos/{repo:path}",
    status_code=200,
)
def delete_github_repo(
    repo: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    row = db.query(GithubRepo).filter(
        GithubRepo.repo == repo,
    ).first()
    if not row:
        raise HTTPException(404, f"repo not found: {repo}")
    db.delete(row)
    db.flush()
    return {"detail": "deleted"}


@router.get("/integrations/github/repos")
def list_github_repos(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    rows = (
        db.query(GithubRepo)
        .order_by(GithubRepo.repo)
        .all()
    )
    return [_repo_row(r, db) for r in rows]


@router.post("/integrations/github/sync")
def sync_github(
    body: dict | None = None,
    scope: Scope = Depends(_scope),
):
    """Synchronously runs github_sync (one repo or all). Wraps
    via the JobRun-logging wrapper so the resulting row appears
    in the Jobs page like a scheduled run."""
    scope.require_org_admin()
    from worker.jobs.github_sync import run as run_sync
    from worker.job_runner import job as wrap_job
    repo = (body or {}).get("repo")

    def _bound():
        return run_sync(repo=repo) if repo else run_sync()

    wrapped = wrap_job("github_sync", _bound)
    result = wrapped(triggered_by=scope.email)
    return {"detail": result.get("detail")}


@router.get("/integrations/github/label-map")
def get_label_map(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == "github_label_map")
        .first()
    )
    if not row or not row.value:
        return DEFAULT_LABEL_MAP
    try:
        return json.loads(row.value)
    except Exception:
        return DEFAULT_LABEL_MAP


@router.put("/integrations/github/label-map")
def put_label_map(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Persist label map. If `?reclassify=1`, kick off a full
    pr_classify run synchronously after saving. Returns
    affected_pr_count + a runtime estimate the UI uses for the
    save-bar messaging."""
    scope.require_org_admin()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected JSON object")
    label_map = body.get("label_map", body)
    for k in ("story", "bug", "task"):
        if k not in label_map or not isinstance(label_map[k], list):
            raise HTTPException(
                400, f"missing or invalid key: {k}",
            )
    payload = json.dumps({
        "story": label_map["story"],
        "bug":   label_map["bug"],
        "task":  label_map["task"],
    })
    row = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == "github_label_map")
        .first()
    )
    if row:
        row.value = payload
    else:
        db.add(AdminConfig(
            key="github_label_map", value=payload,
        ))
    db.flush()
    affected = db.query(GithubActivity).count()
    out = {
        "affected_pr_count": affected,
        "reclass_estimated_seconds": max(1, affected // 100),
    }
    if body.get("reclassify"):
        from worker.jobs.pr_classify import run as run_classify
        from worker.job_runner import job as wrap_job
        wrapped = wrap_job(
            "pr_classify",
            lambda: run_classify(full_rebuild=True),
        )
        result = wrapped(triggered_by=scope.email)
        out["reclassify_run_detail"] = result.get("detail")
    return out


@router.post("/integrations/github/preview-classification")
def preview_classification(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Runs the classifier with the caller-supplied (unsaved)
    label_map against the 25 most-recent merged PRs. Returns
    one trace per PR. The save-bar's "live preview" calls this
    on every keystroke (debounced client-side)."""
    scope.require_org_admin()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected JSON object")
    label_map = body.get("label_map") or DEFAULT_LABEL_MAP
    for k in ("story", "bug", "task"):
        if not isinstance(label_map.get(k), list):
            raise HTTPException(
                400, f"label_map.{k} must be a list",
            )
    from worker.jobs.pr_classify import (
        classify_one,
    )
    prs = (
        db.query(GithubActivity)
        .order_by(GithubActivity.merged_at.desc())
        .limit(25)
        .all()
    )
    # Build the same in-memory issue label index as the worker.
    needed_repos: set[str] = set()
    for pr in prs:
        try:
            for ref in json.loads(pr.issue_refs or "[]"):
                needed_repos.add(ref["repo"])
        except Exception:
            pass
    repo_index: dict[tuple[str, int], list[str]] = {}
    if needed_repos:
        rows = (
            db.query(GithubActivity)
            .filter(GithubActivity.repo.in_(needed_repos))
            .all()
        )
        for r in rows:
            try:
                lbls = json.loads(r.labels or "[]")
            except Exception:
                lbls = []
            repo_index[(r.repo, r.pr_number)] = lbls

    def lookup(repo, number):
        return repo_index.get((repo, int(number)))

    traces = []
    for pr in prs:
        try:
            pr_dict = {
                "repo": pr.repo,
                "pr_number": pr.pr_number,
                "labels": json.loads(pr.labels or "[]"),
                "issue_refs": json.loads(pr.issue_refs or "[]"),
            }
        except Exception:
            pr_dict = {
                "repo": pr.repo,
                "pr_number": pr.pr_number,
                "labels": [],
                "issue_refs": [],
            }
        verdict = classify_one(pr_dict, lookup, label_map)
        traces.append({
            "repo": pr.repo,
            "pr_number": pr.pr_number,
            "title": pr.title,
            "labels": pr_dict["labels"],
            "issue_refs": pr_dict["issue_refs"],
            "pr_class": verdict["pr_class"],
            "classified_by": verdict["classified_by"],
            "probe_trace": verdict["probe_trace"],
        })
    return {"traces": traces}


@router.get("/integrations/github/classification-coverage")
def coverage_stat(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    from datetime import timedelta
    from db.models import PrClassification
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    total = (
        db.query(GithubActivity)
        .filter(GithubActivity.merged_at >= cutoff)
        .count()
    )
    if total == 0:
        return {
            "total_30d": 0, "classified_30d": 0,
            "coverage_pct": 0,
            "mix": {"story": 0, "bug": 0, "task": 0},
            "probe_attribution": [],
        }
    cls_rows = (
        db.query(PrClassification)
        .join(
            GithubActivity,
            (GithubActivity.repo == PrClassification.repo)
            & (GithubActivity.pr_number == PrClassification.pr_number),
        )
        .filter(GithubActivity.merged_at >= cutoff)
        .all()
    )
    classified = len(cls_rows)
    mix = {"story": 0, "bug": 0, "task": 0}
    by_probe: dict[str, int] = {}
    for r in cls_rows:
        mix[r.pr_class] = mix.get(r.pr_class, 0) + 1
        by_probe[r.classified_by] = by_probe.get(r.classified_by, 0) + 1
    probes = [
        {
            "probe": k, "count": v,
            "pct": int(round(100 * v / total)),
        }
        for k, v in sorted(
            by_probe.items(), key=lambda x: -x[1])
    ]
    return {
        "total_30d": total,
        "classified_30d": classified,
        "coverage_pct": int(round(100 * classified / total)),
        "mix": mix,
        "probe_attribution": probes,
    }
