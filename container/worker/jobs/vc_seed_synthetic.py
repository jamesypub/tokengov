"""
vc_seed_synthetic — Layer B of the V&C demo seed (#219, #251).

Generates synthetic PRs for the 9 teams not covered by Layer A,
plus injects synthetic spend onto the 5 Layer A teams. Lives as
a worker job so /api/jobs/run can invoke it on local + remote
(ECS) installs without docker compose tricks.

Run AFTER vc_seed (Layer A). Idempotent — synthetic-only rows
(repo prefix `tenant/`, login prefix `gh-`, model_id "synthetic")
are wiped at the start of every run.
"""
from __future__ import annotations
import json
import logging
import math
import random
from datetime import datetime, timedelta, timezone

from db.session import get_db
from db.models import (
    GithubActivity, GithubRepo, LinkedAccount,
    PrClassification, CurUserSpend, User,
)

log = logging.getLogger("worker.vc_seed_synthetic")


SYNTHETIC_TEAMS = [
    {"team_id": "team-1.2",   "archetype": "fast-cheap"},
    {"team_id": "team-1.3",   "archetype": "slow-expensive"},
    {"team_id": "team-2.1",   "archetype": "bug-heavy"},
    {"team_id": "team-2.2",   "archetype": "balanced"},
    {"team_id": "team-2.3",   "archetype": "fast-cheap"},
    {"team_id": "team-3.1",   "archetype": "balanced"},
    {"team_id": "team-3.2",   "archetype": "slow-expensive"},
    {"team_id": "team-3.3",   "archetype": "bug-heavy"},
    {"team_id": "team-1.1.2", "archetype": "balanced"},
]

# Extra (ornamental) repo count per team — these land in
# github_repos so the V&C "repos" column shows variety
# instead of a uniform "1 repo" across every row (#252).
# Synthetic PRs / spend only land on the *primary* repo
# (`tenant/<tid>-app`); secondary repos exist purely for
# count display, mirroring real teams that own multiple
# microservices but only one drives PR volume in the
# window. Total repos per team = 1 (primary) + extras.
EXTRA_REPOS_BY_TEAM = {
    "team-1":     2,
    "team-2":     1,
    "team-3":     0,
    "team-1.1":   2,
    "team-1.1.1": 1,
    "team-1.2":   2,
    "team-1.3":   0,
    "team-2.1":   1,
    "team-2.2":   2,
    "team-2.3":   0,
    "team-3.1":   1,
    "team-3.2":   2,
    "team-3.3":   1,
    "team-1.1.2": 0,
}


ARCHETYPES = {
    "fast-cheap": {
        "cycle_mu": math.log(12),
        "cycle_sigma": 0.55,
        "weekly_mean": 18,
        "class_mix": {"story": 0.7, "task": 0.2, "bug": 0.1},
        "rate_per_hour": 1.5,
        "rate_jitter":   0.30,
    },
    "slow-expensive": {
        "cycle_mu": math.log(96),
        "cycle_sigma": 0.55,
        "weekly_mean": 7,
        "class_mix": {"story": 0.4, "task": 0.1, "bug": 0.5},
        "rate_per_hour": 1.0,
        "rate_jitter":   0.30,
    },
    "bug-heavy": {
        "cycle_mu": math.log(36),
        "cycle_sigma": 0.55,
        "weekly_mean": 12,
        "class_mix": {"story": 0.25, "task": 0.15, "bug": 0.6},
        "rate_per_hour": 1.2,
        "rate_jitter":   0.25,
    },
    "balanced": {
        "cycle_mu": math.log(24),
        "cycle_sigma": 0.55,
        "weekly_mean": 14,
        "class_mix": {"story": 0.4, "task": 0.3, "bug": 0.3},
        "rate_per_hour": 1.25,
        "rate_jitter":   0.25,
    },
}


LAYER_A_TEAMS = [
    "team-1", "team-2", "team-3",
    "team-1.1", "team-1.1.1",
]
LAYER_A_BASE_RATE_PER_HOUR = ARCHETYPES["balanced"]["rate_per_hour"]
LAYER_A_RATE_JITTER        = ARCHETYPES["balanced"]["rate_jitter"]


CLASS_LABEL = {
    "story": "feature",
    "bug":   "bug",
    "task":  "chore",
}


def run() -> dict:
    rng = random.Random(42)
    now = datetime.now(timezone.utc)
    window_days = 90
    floor = now - timedelta(days=window_days)

    with get_db() as db:
        n_act = (
            db.query(GithubActivity)
            .filter(GithubActivity.repo.like("tenant/%"))
            .delete(synchronize_session=False)
        )
        n_cl = (
            db.query(PrClassification)
            .filter(PrClassification.repo.like("tenant/%"))
            .delete(synchronize_session=False)
        )
        n_la = (
            db.query(LinkedAccount)
            .filter(
                LinkedAccount.vendor == "github",
                LinkedAccount.external_handle.like("gh-%"),
            )
            .delete(synchronize_session=False)
        )
        n_qm = (
            db.query(CurUserSpend)
            .filter(CurUserSpend.model_id == "synthetic")
            .delete(synchronize_session=False)
        )
        if n_act or n_cl or n_la or n_qm:
            log.info(
                "reset cleared act=%s cls=%s la=%s qm=%s",
                n_act, n_cl, n_la, n_qm,
            )

    with get_db() as db:
        users = db.query(User).all()
        members_by_team: dict[str, list[str]] = {}
        for u in users:
            if u.team_id and u.email.startswith("team-") \
                    and "-member-" in u.email:
                members_by_team.setdefault(
                    u.team_id, []).append(u.email)
        for tid in members_by_team:
            members_by_team[tid].sort()

    inserted = 0
    linked_n = 0
    spend_rows = 0

    for spec in SYNTHETIC_TEAMS:
        tid = spec["team_id"]
        arch = ARCHETYPES[spec["archetype"]]
        repo = f"tenant/{tid}-app"
        members = members_by_team.get(tid, [])
        if not members:
            log.info("%s: no members; skip", tid)
            continue

        login_for_email: dict[str, str] = {}
        with get_db() as db:
            for email in members:
                local = email.split("@", 1)[0]
                login = "gh-" + local
                login_for_email[email] = login
                existing = (
                    db.query(LinkedAccount)
                    .filter(
                        LinkedAccount.vendor == "github",
                        LinkedAccount.external_handle == login,
                    )
                    .first()
                )
                if not existing:
                    db.add(LinkedAccount(
                        email=email,
                        vendor="github",
                        external_handle=login,
                        linked_by="auto",
                        linked_at=now - timedelta(
                            days=rng.randint(0, 60)),
                    ))
                    linked_n += 1

            row = (
                db.query(GithubRepo)
                .filter(GithubRepo.repo == repo)
                .first()
            )
            if row:
                row.team_id = tid
                row.sync_status = "ok"
                row.last_sync_at = now
            else:
                db.add(GithubRepo(
                    repo=repo, team_id=tid,
                    sync_status="ok",
                    last_sync_at=now,
                    added_by="seed-synth",
                ))

        prs_in_team: list[dict] = []
        weekly_mean = arch["weekly_mean"]
        for week_idx in range(window_days // 7):
            week_end = now - timedelta(days=week_idx * 7)
            week_start = week_end - timedelta(days=7)
            count = max(0, int(rng.gauss(
                weekly_mean, math.sqrt(max(weekly_mean, 1)))))
            for _ in range(count):
                r = rng.random()
                cum = 0.0
                cls = "task"
                for c, p in arch["class_mix"].items():
                    cum += p
                    if r <= cum:
                        cls = c
                        break
                hours = rng.lognormvariate(
                    arch["cycle_mu"], arch["cycle_sigma"])
                hours = min(hours, 24 * 30)
                merged_at = week_start + timedelta(
                    seconds=rng.uniform(
                        0,
                        (week_end - week_start).total_seconds(),
                    ))
                if merged_at > now:
                    merged_at = now - timedelta(
                        minutes=rng.randint(5, 60))
                opened_at = merged_at - timedelta(hours=hours)
                email = members[rng.randint(0, len(members) - 1)]
                login = login_for_email[email]
                jitter = 1.0 + rng.uniform(
                    -arch["rate_jitter"], arch["rate_jitter"])
                spend = arch["rate_per_hour"] * hours * jitter
                prs_in_team.append({
                    "merged_at": merged_at,
                    "opened_at": opened_at,
                    "cls": cls,
                    "login": login,
                    "email": email,
                    "spend": spend,
                    "hours": hours,
                })

        with get_db() as db:
            existing_max = (
                db.query(GithubActivity)
                .filter(GithubActivity.repo == repo)
                .all()
            )
            next_n = (
                max(
                    (p.pr_number for p in existing_max),
                    default=0,
                ) + 1
            )
            for i, pr in enumerate(prs_in_team):
                pr_number = next_n + i
                existing = (
                    db.query(GithubActivity)
                    .filter(
                        GithubActivity.repo == repo,
                        GithubActivity.pr_number == pr_number,
                    )
                    .first()
                )
                if existing:
                    continue
                db.add(GithubActivity(
                    repo=repo,
                    pr_number=pr_number,
                    title=(
                        f"[{pr['cls']}] synthetic #{pr_number} "
                        f"on {tid}"
                    ),
                    author_login=pr["login"],
                    author_email=pr["email"],
                    body=(
                        f"Synthetic PR for V&C seed (team {tid}, "
                        f"archetype {spec['archetype']}). "
                        f"Cycle: {pr['hours']:.1f}h."
                    ),
                    labels=json.dumps([CLASS_LABEL[pr["cls"]]]),
                    issue_refs=json.dumps([]),
                    additions=int(rng.uniform(20, 500)),
                    deletions=int(rng.uniform(5, 200)),
                    merged_at=pr["merged_at"],
                    created_at=pr["opened_at"],
                ))
                inserted += 1

        # #643: per-day grain — bucket synthetic spend by the PR's
        # merged_at DATE (was YYYY-MM month), keyed
        # (email, usage_date).
        per_email_day: dict[tuple[str, object], float] = {}
        for pr in prs_in_team:
            day = pr["merged_at"].date()
            per_email_day[(pr["email"], day)] = \
                per_email_day.get((pr["email"], day), 0.0) \
                + pr["spend"]

        with get_db() as db:
            for (email, day), amount in per_email_day.items():
                existing = (
                    db.query(CurUserSpend)
                    .filter(
                        CurUserSpend.email == email,
                        CurUserSpend.usage_hour == day,
                        CurUserSpend.region == "us-east-1",
                        CurUserSpend.model_id == "synthetic",
                    )
                    .first()
                )
                if existing:
                    existing.spend_usd = amount
                else:
                    db.add(CurUserSpend(
                        email=email,
                        usage_hour=day,
                        region="us-east-1",
                        model_id="synthetic",
                        spend_usd=amount,
                        data_source="seed",
                    ))
                    spend_rows += 1

        log.info(
            "%s (%s): %d PRs, $%.0f spend",
            tid, spec["archetype"], len(prs_in_team),
            sum(p["spend"] for p in prs_in_team),
        )

    layer_a_rows = 0
    layer_a_total = 0.0
    with get_db() as db:
        repos_by_team: dict[str, list[str]] = {}
        for r in db.query(GithubRepo).all():
            if r.team_id in LAYER_A_TEAMS:
                repos_by_team.setdefault(
                    r.team_id, []).append(r.repo)

        for tid in LAYER_A_TEAMS:
            members = members_by_team.get(tid, [])
            repos = repos_by_team.get(tid, [])
            if not members or not repos:
                log.info("Layer A %s: no members/repos; skip", tid)
                continue
            all_rows: list = []
            for repo in repos:
                all_rows.extend(
                    db.query(GithubActivity)
                    .filter(
                        GithubActivity.repo == repo,
                        GithubActivity.merged_at >= floor,
                    )
                    .all()
                )
            team_pr_n = len(all_rows)
            if not all_rows:
                continue
            merge_days = {
                ga.merged_at.date() for ga in all_rows
                if ga.merged_at
            }
            density = len(merge_days) / max(window_days, 1)
            cycle_hours_obs = []
            for ga in all_rows:
                if ga.merged_at and ga.created_at:
                    cycle_hours_obs.append(max(
                        0.5,
                        (ga.merged_at - ga.created_at)
                        .total_seconds() / 3600.0,
                    ))
            sum_cycle = sum(cycle_hours_obs) or (24.0 * team_pr_n)
            target_dollar_per_pr = 30.0
            rate = (
                target_dollar_per_pr * team_pr_n
            ) / (
                sum_cycle * max(density, 0.05)
            )
            rate = max(rate, LAYER_A_BASE_RATE_PER_HOUR)
            # #643: per-day grain — key by merged_at DATE.
            per_email_day: dict[tuple[str, object], float] = {}
            team_total = 0.0
            for ga in all_rows:
                if not ga.merged_at or not ga.created_at:
                    hours = 24.0
                else:
                    hours = max(
                        0.5,
                        (ga.merged_at - ga.created_at)
                        .total_seconds() / 3600.0,
                    )
                hours = min(hours, 24 * 30)
                jitter = 1.0 + rng.uniform(
                    -LAYER_A_RATE_JITTER,
                    LAYER_A_RATE_JITTER,
                )
                spend = rate * hours * jitter
                team_total += spend
                email = members[ga.pr_number % len(members)]
                day = ga.merged_at.date()
                per_email_day[(email, day)] = \
                    per_email_day.get((email, day), 0.0) \
                    + spend
            for (email, day), amount in per_email_day.items():
                existing = (
                    db.query(CurUserSpend)
                    .filter(
                        CurUserSpend.email == email,
                        CurUserSpend.usage_hour == day,
                        CurUserSpend.region == "us-east-1",
                        CurUserSpend.model_id == "synthetic",
                    )
                    .first()
                )
                if existing:
                    existing.spend_usd = amount
                else:
                    db.add(CurUserSpend(
                        email=email,
                        usage_hour=day,
                        region="us-east-1",
                        model_id="synthetic",
                        spend_usd=amount,
                        data_source="seed",
                    ))
                    layer_a_rows += 1
            layer_a_total += team_total
            log.info(
                "Layer A %s: %d PRs, $%.0f synthetic spend",
                tid, team_pr_n, team_total,
            )

    extras_added = 0
    with get_db() as db:
        for tid, n_extra in EXTRA_REPOS_BY_TEAM.items():
            for i in range(1, n_extra + 1):
                repo = f"tenant/{tid}-svc-{i}"
                existing = (
                    db.query(GithubRepo)
                    .filter(GithubRepo.repo == repo)
                    .first()
                )
                if existing:
                    existing.team_id = tid
                    existing.sync_status = "ok"
                    existing.last_sync_at = now
                else:
                    db.add(GithubRepo(
                        repo=repo, team_id=tid,
                        sync_status="ok",
                        last_sync_at=now,
                        added_by="seed-synth",
                    ))
                    extras_added += 1
    log.info(
        "ornamental extras: +%d github_repos rows",
        extras_added,
    )

    detail = (
        f"Layer B: {inserted} synthetic PRs, {linked_n} linked, "
        f"{spend_rows} synthetic-team spend rows; "
        f"Layer A spend: +{layer_a_rows} rows, "
        f"${layer_a_total:.0f} total; "
        f"+{extras_added} ornamental repos"
    )
    log.info(detail)
    return {
        "detail": detail,
        "inserted": inserted,
        "linked": linked_n,
        "spend_rows": spend_rows,
        "layer_a_spend_rows": layer_a_rows,
        "layer_a_spend_total": layer_a_total,
        "ornamental_repos": extras_added,
    }
