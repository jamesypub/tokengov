"""
github_sync — fetches merged PR metadata into github_activity.

Runs every 10 minutes when V&C master flag is on. Skips entirely
otherwise (the flag check is the first thing the loop does).

Token resolution is PER REPO (#1043), via _resolve_repo_token:
 - public repo (probed)        → anonymous, no token (token_kind=public)
 - private + per-repo override  → that repo's PAT     (token_kind=override)
 - private + org default        → the org PAT below    (token_kind=org)
 - private + no token resolves  → paused               (token_kind=missing)
The org default PAT (tier 2) is read by _read_token:
 1. AWS Secrets Manager `tg/github/default-pat` — JSON {token, ...}
 2. admin_config row `github_default_pat` — JSON or raw plaintext
The seed placeholder `SEED_PLACEHOLDER_NEEDS_ROTATE` reads as missing.
A repo is NEVER handed the org default until a probe confirms it is
private — the cross-tenant fail-safe. There is no global no-token skip:
a missing org token only pauses private-without-token repos; public
repos still sync anonymously.

Per-repo flow:
  - (auto) GET /repos/{repo} unauthenticated to classify public/private.
  - GET /repos/{repo}/pulls?state=closed&sort=updated (paginated).
  - For each PR with merged_at != null: upsert into github_activity
    keyed on (repo, pr_number).
  - Update github_repos.last_sync_at + sync_status on success.
  - 401 / 403-without-ratelimit → sync_status='auth_failed';
    403/429 rate-limit → 'rate_limited' (transient, anonymous is
    60 req/hr); 404 → 'not_found' (private or nonexistent).

Idempotent: re-runs of the same PR overwrite labels/title but
preserve `created_at`. The merged_at timestamp is GitHub's, so
deterministic.
"""
from __future__ import annotations
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from db.session import get_db
from db.models import (
    AdminConfig, GithubActivity, GithubRepo,
    JiraSite, PrJiraRef,
)

log = logging.getLogger("worker.github_sync")

SM_NAME = "tg/github/default-pat"
GH_API = "https://api.github.com"


def _api_path(repo_key: str) -> str:
    """#1050: derive the GitHub REST path `owner/name` from a stored
    repo key. #1042 stores github.com rows host-first
    (`github.com/owner/name`); the REST API expects `owner/name` only,
    so a verbatim key 404s and the public probe mis-flags the repo as
    private. Strip a leading `github.com/`. A bare legacy `owner/name`
    key is returned unchanged. (Non-github hosts are never fetched —
    the run() filter keeps them paused — so this only ever yields a
    github.com path.) The DB key (row.repo / github_activity.repo) is
    UNCHANGED; only the outbound API path uses this.
    """
    if repo_key.startswith("github.com/"):
        return repo_key[len("github.com/"):]
    return repo_key


def _read_token() -> str | None:
    try:
        import boto3
        sm = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        try:
            r = sm.get_secret_value(SecretId=SM_NAME)
            payload = json.loads(r.get("SecretString") or "{}")
            tok = payload.get("token")
            if tok and tok != "SEED_PLACEHOLDER_NEEDS_ROTATE":
                return tok
        except sm.exceptions.ResourceNotFoundException:
            pass
        except Exception as e:
            log.warning("SM read failed: %s", e)
    except Exception as e:
        log.debug("boto3 unavailable: %s", e)

    with get_db() as db:
        row = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == "github_default_pat")
            .first()
        )
        if not row or not row.value:
            return None
        try:
            payload = json.loads(row.value)
            tok = payload.get("token")
        except Exception:
            tok = row.value
        if not tok or tok == "SEED_PLACEHOLDER_NEEDS_ROTATE":
            return None
        return tok


def _read_repo_override_token(row) -> str | None:
    """#1043: per-repo override PAT. SM secret (pat_secret_arn) first,
    then the dev/local-compose plaintext fallback (pat_plain), mirroring
    JiraSite. Returns None if neither resolves."""
    arn = getattr(row, "pat_secret_arn", None)
    if arn:
        try:
            import boto3
            sm = boto3.client(
                "secretsmanager",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
            )
            r = sm.get_secret_value(SecretId=arn)
            payload = json.loads(r.get("SecretString") or "{}")
            tok = payload.get("token")
            if tok and tok != "SEED_PLACEHOLDER_NEEDS_ROTATE":
                return tok
        except Exception as e:  # noqa: BLE001
            log.warning(
                "github_sync: repo override SM read failed for %s: %s",
                getattr(row, "repo", "?"), e)
    plain = getattr(row, "pat_plain", None)
    if plain and plain != "SEED_PLACEHOLDER_NEEDS_ROTATE":
        return plain
    return None


def _probe_public(repo_full_name: str) -> bool | None:
    """#1043: unauthenticated GET /repos/{owner}/{name} to classify
    visibility. 200 → public (True); 404 → private or nonexistent →
    needs auth (False). None on any other/transient error (caller leaves
    is_public unset so it re-probes next run). Read-only, host is always
    api.github.com — no SSRF surface."""
    try:
        with _gh_request(f"/repos/{repo_full_name}", None):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        # 403/429 (rate limit) or 5xx — can't classify now.
        return None
    except Exception:  # noqa: BLE001
        return None


def _resolve_repo_token(row) -> dict:
    """#1043: per-repo 3-tier token resolution. Returns
    {token, token_kind, paused, is_public} and mutates row's
    is_public/last_probed_at when it probes. NEVER hands the org-default
    token to a repo until a probe has confirmed it is PRIVATE — an
    unprobed row (is_public None, every backfilled row) is probed first.
    This probe-before-org-default order is the cross-tenant fail-safe:
    without it a private row at the column default would sync with
    whatever org PAT is set, possibly another owner's.

    Modes (admin-chosen row.token_mode):
      public   → always anonymous.
      override → always the per-repo PAT (missing+paused if absent).
      auto     → probe; public→anon, private→org default (else missing).
    """
    mode = getattr(row, "token_mode", None) or "auto"
    # #1050: probe the GitHub API path (owner/name), NOT the stored
    # host-prefixed key — a `github.com/owner/name` key 404s and the
    # repo gets mis-flagged private.
    repo_full = _api_path(row.repo)

    if mode == "public":
        return {"token": None, "token_kind": "public",
                "paused": False, "is_public": True}

    if mode == "override":
        tok = _read_repo_override_token(row)
        if tok:
            return {"token": tok, "token_kind": "override",
                    "paused": False, "is_public": row.is_public}
        return {"token": None, "token_kind": "missing",
                "paused": True, "is_public": row.is_public}

    # mode == auto (default).
    vis = row.is_public
    if vis is None:
        vis = _probe_public(repo_full)
        row.is_public = vis
        row.last_probed_at = datetime.now(timezone.utc)
    if vis is True:
        return {"token": None, "token_kind": "public",
                "paused": False, "is_public": True}
    if vis is False:
        # Private (confirmed) → only NOW consider the org default.
        org = _read_token()
        if org:
            return {"token": org, "token_kind": "org",
                    "paused": False, "is_public": False}
        return {"token": None, "token_kind": "missing",
                "paused": True, "is_public": False}
    # vis still None (probe inconclusive — rate-limited/transient). Do
    # NOT fall through to the org token; leave unprobed + paused this
    # run, re-probe next time. Fail-safe over fail-open.
    return {"token": None, "token_kind": "unprobed",
            "paused": True, "is_public": None}


def _is_rate_limited(e: "urllib.error.HTTPError") -> bool:
    """#1043: distinguish a GitHub rate-limit (403/429 with
    X-RateLimit-Remaining: 0 or a Retry-After header) from a genuine
    auth failure (401, or 403 without the rate-limit signal). Anonymous
    public sync runs at 60 req/hr so this is a real, transient state —
    not auth_failed."""
    if e.code not in (403, 429):
        return False
    try:
        remaining = e.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.strip() == "0":
            return True
        if e.headers.get("Retry-After") is not None:
            return True
    except Exception:  # noqa: BLE001
        pass
    return e.code == 429


def _gh_request(path: str, token: str | None):
    headers = {
        "User-Agent": "tg-admin",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(
        GH_API + path, headers=headers,
    )
    return urllib.request.urlopen(req, timeout=15)


# #656: the closed-PR fetch used to grab ONE page of 50 and stop —
# any repo with >50 closed PRs in the window silently lost page 2+.
# Now we paginate (per_page=100, GitHub max) following the Link
# `rel="next"` header, with an early stop once we cross the
# already-synced horizon (PRs sorted updated desc), so steady-state
# runs do bounded work instead of walking full history every tick.
PER_PAGE = 100
# Safety cap so a first sync (no horizon) or a pathological repo
# can't walk unbounded. 50 pages × 100 = 5000 closed PRs — far
# beyond any pilot repo's window; logged if hit.
MAX_PAGES = 50
# Horizon margin: stop paginating at PRs updated before
# last_sync_at − this, to absorb late `updated` bumps on PRs that
# merged just before the previous sync (#656 OQ — 1 day).
HORIZON_MARGIN = timedelta(days=1)

_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def _parse_next_link(link_header: str | None) -> str | None:
    """Extract the `rel="next"` URL from a GitHub Link header, or
    None when there's no next page. Returns the full URL as GitHub
    emits it (absolute), which _fetch_page passes straight to
    urlopen."""
    if not link_header:
        return None
    m = _LINK_NEXT_RE.search(link_header)
    return m.group(1) if m else None


def _gh_request_url(url: str, token: str | None):
    """Like _gh_request but takes an absolute URL (for following
    Link headers, which are already-absolute api.github.com URLs)."""
    headers = {
        "User-Agent": "tg-admin",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=15)


def _fetch_closed_prs(
    repo_full_name: str,
    token: str | None,
    horizon: datetime | None,
) -> list[dict]:
    """Fetch closed PRs (sorted updated desc), following pagination
    until either there's no `rel="next"` page or we cross `horizon`
    (a PR whose `updated_at` is older than horizon — everything
    beyond is already synced). horizon=None (first sync) walks until
    no next page, bounded by MAX_PAGES."""
    url = (
        GH_API + f"/repos/{repo_full_name}/pulls?"
        f"state=closed&sort=updated&direction=desc&"
        f"per_page={PER_PAGE}"
    )
    out: list[dict] = []
    pages = 0
    while url and pages < MAX_PAGES:
        pages += 1
        with _gh_request_url(url, token) as r:
            page = json.loads(r.read().decode("utf-8"))
            link = r.headers.get("Link")
        out.extend(page)
        # Early stop: page is updated-desc, so once the LAST item on
        # this page is older than the horizon, the next page is all
        # older still — nothing new to fetch.
        if horizon is not None and page:
            last = page[-1].get("updated_at")
            if last:
                last_dt = datetime.fromisoformat(
                    last.replace("Z", "+00:00"))
                if last_dt < horizon:
                    break
        url = _parse_next_link(link)
    if pages >= MAX_PAGES and url:
        log.warning(
            "github_sync %s hit MAX_PAGES=%d (more pages exist) — "
            "consider a tighter window", repo_full_name, MAX_PAGES)
    return out


_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+)-(\d+)\b")


def _jira_project_keys(db) -> set[str]:
    """Allowlist of project keys from configured jira_sites
    (union across sites). Falls back to TG_JIRA_PROJECTS env
    for local dev without a configured site. Empty set means
    extraction is off."""
    out: set[str] = set()
    try:
        rows = db.query(JiraSite).all()
    except Exception:
        rows = []
    for r in rows:
        try:
            keys = json.loads(r.projects or "[]")
        except Exception:
            keys = []
        for k in keys:
            if isinstance(k, str) and k:
                out.add(k.upper())
    if not out:
        env_csv = os.environ.get("TG_JIRA_PROJECTS", "")
        for k in env_csv.split(","):
            k = k.strip().upper()
            if k:
                out.add(k)
    return out


def extract_jira_keys(
    text: str | None,
    allowed: set[str],
) -> list[str]:
    """Find KEY-NUM patterns in `text` filtered by allowed
    project keys. De-dupes preserving order."""
    if not text or not allowed:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _JIRA_KEY_RE.finditer(text):
        proj = m.group(1)
        num = m.group(2)
        if proj not in allowed:
            continue
        key = f"{proj}-{num}"
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _extract_issue_refs(body: str | None, repo: str) -> list[dict]:
    """Find #N references and `org/repo#N` cross-repo refs."""
    if not body:
        return []
    refs: list[dict] = []
    seen: set[tuple[str, int]] = set()
    # cross-repo first so plain `#N` doesn't gobble the suffix
    for m in re.finditer(
        r"(?:closes|fixes|resolves|see)?\s*"
        r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)",
        body, flags=re.IGNORECASE,
    ):
        rr, num = m.group(1), int(m.group(2))
        key = (rr, num)
        if key not in seen:
            seen.add(key)
            refs.append({"repo": rr, "number": num})
    for m in re.finditer(r"(?:^|\s|\()#(\d+)\b", body):
        num = int(m.group(1))
        key = (repo, num)
        if key not in seen:
            seen.add(key)
            refs.append({"repo": repo, "number": num})
    return refs


def _first_commit_message(
    repo_full_name: str, pr_number: int, token: str | None,
) -> str | None:
    try:
        with _gh_request(
            f"/repos/{repo_full_name}/pulls/"
            f"{pr_number}/commits?per_page=1",
            token,
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.debug("commits fetch failed for %s#%s: %s",
                  repo_full_name, pr_number, e)
        return None
    if not data:
        return None
    commit = (data[0] or {}).get("commit") or {}
    return commit.get("message")


def _record_jira_refs(
    db, repo_full_name: str, pr_number: int,
    title: str | None, body: str | None,
    branch: str | None, gh_number: int | None,
    token: str | None, allowed: set[str],
) -> int:
    """Extract Jira keys from PR fields and upsert pr_jira_refs.
    Returns count of refs written (existing rows are skipped)."""
    sources: list[tuple[str, str | None]] = [
        ("title",  title),
        ("body",   body),
        ("branch", branch),
    ]
    found: dict[str, str] = {}  # key -> source
    for src, txt in sources:
        for k in extract_jira_keys(txt, allowed):
            if k not in found:
                found[k] = src

    # Fallback: fetch first commit message only when nothing
    # else hit. Saves an API call per PR in the common case.
    # #1050: the GitHub call uses the owner/name path; PrJiraRef.repo
    # below stays keyed on the canonical repo_full_name.
    if not found and gh_number is not None:
        msg = _first_commit_message(
            _api_path(repo_full_name), gh_number, token,
        )
        for k in extract_jira_keys(msg, allowed):
            if k not in found:
                found[k] = "commit"

    written = 0
    for issue_key, source in found.items():
        existing = (
            db.query(PrJiraRef)
            .filter(
                PrJiraRef.repo == repo_full_name,
                PrJiraRef.pr_number == pr_number,
                PrJiraRef.issue_key == issue_key,
            )
            .first()
        )
        if existing:
            if existing.source != source:
                existing.source = source
            continue
        db.add(PrJiraRef(
            repo=repo_full_name,
            pr_number=pr_number,
            issue_key=issue_key,
            source=source,
        ))
        written += 1
    return written


def _sync_one_repo(repo_full_name: str, token: str | None) -> dict:
    """Returns {fetched, upserted, status}. `repo_full_name` is the
    stored DB key (canonical host-prefixed for new rows, bare
    owner/name for legacy). #1050: all GitHub API calls use the derived
    `owner/name` path; all DB reads/writes stay keyed on the DB key so
    github_activity history isn't split."""
    api_path = _api_path(repo_full_name)
    # #656: compute the early-stop horizon from the repo's PRIOR
    # last_sync_at (read before we overwrite it below). horizon =
    # last_sync_at − margin; None on first sync (walk bounded by
    # MAX_PAGES).
    horizon: datetime | None = None
    with get_db() as db:
        prior = (
            db.query(GithubRepo.last_sync_at)
            .filter(GithubRepo.repo == repo_full_name)
            .first()
        )
    if prior and prior[0]:
        last_sync = prior[0]
        if last_sync.tzinfo is None:
            last_sync = last_sync.replace(tzinfo=timezone.utc)
        horizon = last_sync - HORIZON_MARGIN

    try:
        data = _fetch_closed_prs(api_path, token, horizon)
    except urllib.error.HTTPError as e:
        # #1043: split the lumped 401/403. A rate-limit (403/429 with
        # the X-RateLimit/Retry-After signal) is transient, NOT bad
        # creds — anonymous public sync hits 60 req/hr. A 404 on an
        # authenticated request is not-found/gone, also distinct from
        # auth failure (private repo the token can't see returns 404).
        if _is_rate_limited(e):
            return {
                "fetched": 0, "upserted": 0,
                "status": "rate_limited",
                "error": f"HTTP {e.code} rate limited",
            }
        if e.code in (401, 403):
            return {
                "fetched": 0, "upserted": 0,
                "status": "auth_failed",
                "error": f"HTTP {e.code}",
            }
        if e.code == 404:
            return {
                "fetched": 0, "upserted": 0,
                "status": "not_found",
                "error": "HTTP 404 (private or nonexistent)",
            }
        raise

    upserted = 0
    with get_db() as db:
        jira_allowed = _jira_project_keys(db)
        for pr in data:
            if not pr.get("merged_at"):
                continue
            num = pr["number"]
            existing = (
                db.query(GithubActivity)
                .filter(
                    GithubActivity.repo == repo_full_name,
                    GithubActivity.pr_number == num,
                )
                .first()
            )
            labels = [
                lab.get("name") for lab in (pr.get("labels") or [])
                if lab.get("name")
            ]
            issue_refs = _extract_issue_refs(
                pr.get("body"), repo_full_name,
            )
            merged_at = datetime.fromisoformat(
                pr["merged_at"].replace("Z", "+00:00"),
            )
            # PR open time → repurposed `created_at` column.
            # We use this for cycle-time stats in pr_cost_rollup.
            opened_at = None
            if pr.get("created_at"):
                opened_at = datetime.fromisoformat(
                    pr["created_at"].replace("Z", "+00:00"),
                )
            user = pr.get("user") or {}
            row_kwargs = dict(
                repo=repo_full_name,
                pr_number=num,
                title=pr.get("title"),
                author_login=user.get("login") or "unknown",
                body=pr.get("body"),
                labels=json.dumps(labels),
                issue_refs=json.dumps(issue_refs),
                additions=pr.get("additions") or 0,
                deletions=pr.get("deletions") or 0,
                merged_at=merged_at,
                created_at=opened_at,
            )
            if existing:
                for k, v in row_kwargs.items():
                    setattr(existing, k, v)
            else:
                db.add(GithubActivity(**row_kwargs))
            upserted += 1

            # Jira refs — title/body/branch in priority order;
            # fetch first commit message only as a fallback to
            # avoid an extra API call per PR. (#364)
            if jira_allowed:
                _record_jira_refs(
                    db, repo_full_name, num,
                    pr.get("title"), pr.get("body"),
                    (pr.get("head") or {}).get("ref"),
                    pr.get("number"), token, jira_allowed,
                )

        repo_row = (
            db.query(GithubRepo)
            .filter(GithubRepo.repo == repo_full_name)
            .first()
        )
        now = datetime.now(timezone.utc)
        if repo_row:
            repo_row.last_sync_at = now
            repo_row.sync_status = "ok"
        else:
            db.add(GithubRepo(
                repo=repo_full_name,
                last_sync_at=now,
                sync_status="ok",
                added_by="github_sync",
            ))
    return {
        "fetched": len(data), "upserted": upserted,
        "status": "ok",
    }


def run(repo: str | None = None) -> dict:
    # #1043: token resolution is now PER REPO — a missing org token no
    # longer skips the whole run. Public repos sync anonymously,
    # private-without-token go paused, and the run continues. The
    # global `skipped: no_pat` early return is gone on purpose.
    with get_db() as db:
        if repo:
            names = [
                r.repo for r in db.query(GithubRepo)
                .filter(GithubRepo.repo == repo).all()
            ]
        else:
            # #1042: real sync is github.com-only. Non-github rows
            # (self-hosted GitLab, etc.) are stored host-first for a
            # future multi-host epic but stay paused — never fetched.
            # A NULL host is a legacy/backfilled github.com row.
            names = [
                r.repo for r in db.query(GithubRepo)
                .filter(
                    (GithubRepo.host == "github.com")
                    | (GithubRepo.host.is_(None))
                )
                .all()
            ]
    if not names:
        # A single-repo run for a not-yet-tracked repo (the manual Sync
        # path discovers it) still needs to fetch — fall back to the
        # bare name with auto resolution below.
        if repo:
            names = [repo]
        else:
            return {"detail": "no repos configured"}

    summary = []
    failed: list[str] = []
    paused: list[str] = []
    total_upserted = 0
    for full in names:
        try:
            # Resolve + persist the probe/kind in ONE session so the row
            # stays attached while the resolver mutates is_public.
            with get_db() as db:
                row = (
                    db.query(GithubRepo)
                    .filter(GithubRepo.repo == full)
                    .first()
                )
                if row is None:
                    # Not-yet-tracked single-repo Sync: auto-resolve a
                    # transient row (not persisted until _sync_one_repo
                    # upserts it on success).
                    row = GithubRepo(repo=full, token_mode="auto")
                res = _resolve_repo_token(row)
                if row in db:  # tracked row → persist derived state
                    row.token_kind = res["token_kind"]
                    row.is_public = res["is_public"]
                    if res["is_public"] is not None:
                        row.last_probed_at = datetime.now(timezone.utc)
                    if res["paused"]:
                        row.sync_status = "paused"
            if res["paused"]:
                paused.append(full)
                continue
            result = _sync_one_repo(full, res["token"])
            summary.append(f"{full}={result['upserted']}")
            total_upserted += result["upserted"]
            status = result["status"]
            if status != "ok":
                failed.append(f"{full}({status})")
                with get_db() as db:
                    rr = (
                        db.query(GithubRepo)
                        .filter(GithubRepo.repo == full)
                        .first()
                    )
                    if rr:
                        rr.sync_status = status
        except Exception as e:  # noqa: BLE001
            log.exception("github_sync %s failed", full)
            failed.append(f"{full}({type(e).__name__})")
    detail = (
        f"upserted={total_upserted} repos={len(names)} "
        f"paused={len(paused)}"
    )
    if failed:
        detail += f" failed={','.join(failed)}"

    # Chain pr_classify + pr_cost_rollup so cycle stats land in
    # team_*_metrics within seconds of fresh activity, not the
    # next 30-min rollup tick. Without this, a fresh install
    # leaves Velocity & Cost → Speed empty for up to 30 min
    # because pr_cost_rollup runs independently. (#215)
    if total_upserted > 0:
        try:
            from worker.jobs.pr_classify import run as _classify
            _classify()
        except Exception:
            log.exception("chained pr_classify failed")
        try:
            from worker.jobs.pr_cost_rollup import run as _rollup
            _rollup()
        except Exception:
            log.exception("chained pr_cost_rollup failed")
    return {"detail": detail}
