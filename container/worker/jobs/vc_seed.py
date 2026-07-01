"""
vc_seed — Layer A of the V&C demo seed (#219, #251).

Reads the bundled `tests/fixtures/public_repo_seed.json` cache
(committed at build time) and writes:
  - admin_config: github_default_pat / github_label_map
    (union of per-repo maps + defaults)
  - github_repos rows for the 5 anchor public repos
  - github_activity rows for every merged PR in the cache
  - linked_accounts pairing PR authors to test team members

Idempotent. Re-runs upsert.

Lives as a worker job so the populate script can trigger it via
POST /api/jobs/run for both local-compose and remote (ECS) installs
— the prior shell version used `docker compose exec` and silently
skipped on remote API_BASEs.
"""
from __future__ import annotations
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db.session import get_db
from db.models import (
    AdminConfig, GithubActivity, GithubRepo,
    LinkedAccount, User,
)

log = logging.getLogger("worker.vc_seed")


SEED_REPOS = [
    {"repo": "facebook/react",       "team_id": "team-1"},
    {"repo": "vercel/next.js",       "team_id": "team-2"},
    {"repo": "microsoft/typescript", "team_id": "team-3"},
    {"repo": "microsoft/vscode",     "team_id": "team-1.1"},
    {"repo": "pytorch/pytorch",      "team_id": "team-1.1.1"},
]


LABEL_MAP_DEFAULT = {
    "story": ["feature", "enhancement", "epic",
              "type:feat", "user-story"],
    "bug":   ["bug", "defect", "regression",
              "hotfix", "type:bug"],
    "task":  ["chore", "docs", "refactor",
              "tech-debt", "deps"],
}


def _fixtures_dir() -> Path:
    # Container runtime: /app/tests/fixtures.
    # Pytest from container/: tests/fixtures relative to repo.
    candidates = [
        Path("/app/tests/fixtures"),
        Path(__file__).resolve().parents[2] / "tests" / "fixtures",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def _load_repo_label_map(fixtures: Path, repo: str) -> dict | None:
    fname = repo.replace("/", "__") + ".json"
    p = fixtures / "label_maps" / fname
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _merged_label_map(fixtures: Path) -> dict:
    merged = {k: set(v) for k, v in LABEL_MAP_DEFAULT.items()}
    for spec in SEED_REPOS:
        m = _load_repo_label_map(fixtures, spec["repo"])
        if not m:
            continue
        for cls in ("story", "bug", "task"):
            for lbl in m.get(cls, []):
                merged[cls].add(lbl)
    return {k: sorted(v) for k, v in merged.items()}


def run() -> dict:
    fixtures = _fixtures_dir()
    cache = fixtures / "public_repo_seed.json"
    if not cache.is_file():
        return {
            "detail": (
                f"no public_repo_seed.json at {cache}; "
                "rebuild the container with the fixture committed"
            ),
            "inserted": 0,
            "linked": 0,
        }
    prs_by_repo = json.loads(cache.read_text())

    rng = random.Random(42)

    with get_db() as db:
        # enable_velocity_cost retired in #276 — V&C is
        # always-on; no flag to seed.
        pat = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == "github_default_pat")
            .first()
        )
        placeholder = json.dumps({
            "token": "SEED_PLACEHOLDER_NEEDS_ROTATE",
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "rotated_by": "seed",
        })
        if pat:
            pat.value = placeholder
        else:
            db.add(AdminConfig(
                key="github_default_pat", value=placeholder))

        merged = _merged_label_map(fixtures)
        lmap = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == "github_label_map")
            .first()
        )
        if lmap:
            lmap.value = json.dumps(merged)
        else:
            db.add(AdminConfig(
                key="github_label_map",
                value=json.dumps(merged),
            ))

        for spec in SEED_REPOS:
            row = (
                db.query(GithubRepo)
                .filter(GithubRepo.repo == spec["repo"])
                .first()
            )
            if row:
                row.team_id = spec["team_id"]
                row.sync_status = "ok"
                row.last_sync_at = datetime.now(timezone.utc)
            else:
                db.add(GithubRepo(
                    repo=spec["repo"],
                    team_id=spec["team_id"],
                    sync_status="ok",
                    last_sync_at=datetime.now(timezone.utc),
                    added_by="seed",
                ))

    inserted = 0
    with get_db() as db:
        for repo, prs in prs_by_repo.items():
            for pr in prs:
                if not pr.get("merged_at"):
                    continue
                merged_at = datetime.fromisoformat(
                    pr["merged_at"].replace("Z", "+00:00"))
                opened_at = None
                if pr.get("created_at"):
                    opened_at = datetime.fromisoformat(
                        pr["created_at"].replace("Z", "+00:00"))
                existing = (
                    db.query(GithubActivity)
                    .filter(
                        GithubActivity.repo == repo,
                        GithubActivity.pr_number == pr["number"],
                    )
                    .first()
                )
                row_kwargs = dict(
                    repo=repo,
                    pr_number=pr["number"],
                    title=pr.get("title"),
                    author_login=pr.get("user") or "unknown",
                    body=pr.get("body"),
                    labels=json.dumps(pr.get("labels") or []),
                    issue_refs=json.dumps([]),
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
                    inserted += 1

    linked_n = 0
    ext_n = 0
    with get_db() as db:
        users = db.query(User).all()
        members_by_team: dict[str, list[str]] = {}
        for u in users:
            if u.team_id and u.email.startswith("team-") \
                    and "-member-" in u.email:
                members_by_team.setdefault(u.team_id, []).append(u.email)
        for tid in members_by_team:
            members_by_team[tid].sort()

        seen_logins: set[str] = set()
        for spec in SEED_REPOS:
            tid = spec["team_id"]
            authors = sorted({
                pr["user"] for pr in prs_by_repo.get(spec["repo"], [])
                if pr.get("user")
            })
            authors = [a for a in authors if a not in seen_logins]
            if not authors:
                continue
            seen_logins.update(authors)
            members = members_by_team.get(tid, [])
            if not members:
                continue
            ext_count = max(1, len(authors) // 10)
            ext_idxs = set(rng.sample(
                range(len(authors)),
                k=min(ext_count, len(authors)),
            ))
            for i, login in enumerate(authors):
                if i in ext_idxs:
                    ext_n += 1
                    continue
                email = members[i % len(members)]
                existing = (
                    db.query(LinkedAccount)
                    .filter(
                        LinkedAccount.vendor == "github",
                        LinkedAccount.external_handle == login,
                    )
                    .first()
                )
                if existing:
                    continue
                offset_days = rng.randint(0, 29)
                db.add(LinkedAccount(
                    email=email,
                    vendor="github",
                    external_handle=login,
                    linked_by="auto",
                    linked_at=datetime.now(timezone.utc) -
                              timedelta(days=offset_days),
                ))
                linked_n += 1

    detail = (
        f"Layer A: {inserted} github_activity inserted, "
        f"{linked_n} linked_accounts ({ext_n} external)"
    )
    log.info(detail)
    return {"detail": detail, "inserted": inserted, "linked": linked_n}
