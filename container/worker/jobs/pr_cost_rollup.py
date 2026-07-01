"""
pr_cost_rollup — rebuilds team_daily_metrics + team_weekly_metrics
from github_activity + pr_classifications.

Idempotent: every run truncates and rewrites 90d daily + 13w weekly
windows. This avoids the "late-arriving CUR row" trap that an
incremental approach would hit. Cost per PR is currently
team-aggregate (Cost-by-class deferred to v1.5) — we attribute
each merged PR an even slice of the team's spend in the bin.

Spend source (v1):
  We don't yet have CUR-derived per-team spend. Instead, we sum
  `quota_metrics.spend_usd` over the bin's days for users in the
  team (via `users.team_id`), then divide evenly across PRs.

Cycle time:
  Median + P90 of (PR.merged_at - earliest issue_ref.created_at).
  For PRs without a linked issue, we fall back to PR open->merge
  duration (data we don't currently store; v1 leaves these NULL).

Spec deferral: cycle time uses a placeholder of merged_at -
created_at (PR creation timestamp), which we don't track today, so
v1 returns NULL for cycle stats. The Speed view degrades to
"insufficient data" gracefully — Phase 4 wires the fallback.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from statistics import median

from db.session import get_db
from db.models import (
    AdminConfig, GithubActivity, JiraIssue, JiraWeeklyMetric,
    LinkedAccount, PrClassification, PrJiraRef,
    CurUserSpend, Team, TeamDailyMetric, TeamWeeklyMetric, User,
)

log = logging.getLogger("worker.pr_cost_rollup")


def _has_pat() -> bool:
    """Mirrors pr_classify._has_pat — single source of truth
    for "GitHub integration was set up at least once". (#278)"""
    with get_db() as db:
        row = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == "github_default_pat")
            .first()
        )
        raw = row.value if row else None
    if not raw:
        return False
    try:
        payload = json.loads(raw)
        tok = payload.get("token")
    except Exception:
        tok = raw
    return bool(tok) and tok != "SEED_PLACEHOLDER_NEEDS_ROTATE"


def _bin_day(ts: datetime) -> datetime:
    return ts.replace(
        hour=0, minute=0, second=0, microsecond=0,
    )


def _bin_week_start(ts: datetime) -> datetime:
    """Monday 00:00 UTC of the week containing ts."""
    d = _bin_day(ts)
    return d - timedelta(days=d.weekday())


def _team_for_login(login: str, link_index: dict, user_team: dict) -> str | None:
    email = link_index.get(login)
    if not email:
        return None
    return user_team.get(email)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def run() -> dict:
    # Skip cleanly when GitHub was never configured AND there's
    # no activity to roll up. Same rationale as pr_classify. (#278)
    with get_db() as db:
        n_activity = db.query(GithubActivity).count()
    if n_activity == 0 and not _has_pat():
        log.info(
            "pr_cost_rollup: no github_activity and no PAT, "
            "skipping",
        )
        return {
            "detail": (
                "skipped: github_activity empty and "
                "github_default_pat not configured"
            ),
            "skipped": True,
            "skip_reason": "no_github",
        }

    now = datetime.now(timezone.utc)
    daily_floor = _bin_day(now) - timedelta(days=89)
    weekly_floor = _bin_week_start(now) - timedelta(weeks=12)

    with get_db() as db:
        # Indexes.
        link_rows = (
            db.query(LinkedAccount)
            .filter(LinkedAccount.vendor == "github")
            .all()
        )
        link_index = {l.external_handle: l.email for l in link_rows}
        users = db.query(User).all()
        user_team = {u.email: u.team_id for u in users}

        cls_rows = db.query(PrClassification).all()
        cls_index = {
            (c.repo, c.pr_number): c.pr_class for c in cls_rows
        }

        # Pull merged PRs for the daily window (covers weekly too).
        prs = (
            db.query(GithubActivity)
            .filter(GithubActivity.merged_at >= daily_floor)
            .all()
        )
        # Detached-safe materialisation for the Jira sub-rollup
        # below — that runs in its own get_db() session, so we
        # snapshot the fields it needs while the current one
        # is still bound.
        pr_dicts = [
            {
                "repo": pr.repo,
                "pr_number": pr.pr_number,
                "author_login": pr.author_login,
                "merged_at": pr.merged_at,
            }
            for pr in prs
        ]

        # Aggregate counts per (team, day, class) and (team, week, class).
        daily: dict = defaultdict(int)
        weekly: dict = defaultdict(int)
        # Cycle-hours per (team, bin, class) — list of float hours
        # for percentile/median computation below.
        daily_cycle: dict = defaultdict(list)
        weekly_cycle: dict = defaultdict(list)
        for pr in prs:
            tid = _team_for_login(pr.author_login, link_index, user_team)
            if not tid:
                continue
            cls = cls_index.get((pr.repo, pr.pr_number), "task")
            day = _bin_day(pr.merged_at)
            wk  = _bin_week_start(pr.merged_at)
            daily[(tid, day, "all")] += 1
            daily[(tid, day, cls)] += 1
            if pr.merged_at >= weekly_floor:
                weekly[(tid, wk, "all")] += 1
                weekly[(tid, wk, cls)] += 1
            # Cycle hours (PR open → merge). Skip PRs without
            # created_at (pre-Phase-4 rows).
            if pr.created_at:
                hours = max(
                    0.0,
                    (pr.merged_at - pr.created_at).total_seconds() / 3600.0,
                )
                daily_cycle[(tid, day, "all")].append(hours)
                daily_cycle[(tid, day, cls)].append(hours)
                if pr.merged_at >= weekly_floor:
                    weekly_cycle[(tid, wk, "all")].append(hours)
                    weekly_cycle[(tid, wk, cls)].append(hours)

        # #643: spend per (team, day) now reads the REAL per-day
        # quota_metrics rows directly — the old "spread the month's
        # spend evenly across its days" hack is gone. Each
        # quota_metrics row already carries the exact usage_date, so
        # team daily/weekly spend reflects the day a member actually
        # spent, not a monthly smear. Only pull rows in the rollup
        # window (usage_date >= the daily floor's date).
        team_spend_day: dict = defaultdict(float)
        team_member_index: dict = defaultdict(list)
        for u in users:
            if u.team_id:
                team_member_index[u.team_id].append(u.email)
        floor_date = daily_floor.date()
        qms = (
            db.query(CurUserSpend)
            .filter(CurUserSpend.usage_hour >= floor_date)
            .all()
        )
        for qm in qms:
            tid = user_team.get(qm.email)
            if not tid:
                continue
            # usage_date (a DATE) → midnight-UTC datetime to match
            # the _bin_day key type used across this rollup.
            day_dt = datetime(
                qm.usage_hour.year, qm.usage_hour.month,
                qm.usage_hour.day, tzinfo=timezone.utc,
            )
            if daily_floor <= day_dt <= now:
                team_spend_day[(tid, day_dt)] += (qm.spend_usd or 0)

        # #657: a (team, day) with quota spend but ZERO merged PRs
        # that day would otherwise be dropped — the daily/weekly row
        # sets below are derived from `daily`/`weekly`, which only
        # carry merged-PR keys. Seed a count-0 "all" entry for every
        # spend-bearing (team, day) [and its week, if in the weekly
        # window] that has no merged-PR row, so that spend still lands
        # in team_daily/weekly_metrics (attributed to the team,
        # prs_merged=0, cycle stats null). The spend_by_* maps key off
        # these entries, so the dollars flow through. setdefault never
        # clobbers a real merged-PR count. Skip zero-spend days (a
        # quota_metrics row can exist with spend_usd=0 before pricing
        # is seeded) so we don't emit empty noise rows.
        for (tid, day), spend in team_spend_day.items():
            if spend <= 0:
                continue
            daily.setdefault((tid, day, "all"), 0)
            wk = _bin_week_start(day)
            if wk >= weekly_floor:
                weekly.setdefault((tid, wk, "all"), 0)

        # Truncate the rollup tables for the windows we cover.
        db.query(TeamDailyMetric).filter(
            TeamDailyMetric.day >= daily_floor,
        ).delete(synchronize_session=False)
        db.query(TeamWeeklyMetric).filter(
            TeamWeeklyMetric.week_start >= weekly_floor,
        ).delete(synchronize_session=False)
        db.flush()

        rolled_at = datetime.now(timezone.utc)

        # Daily rows.
        # Distribute the (team, day) spend across the PRs in that
        # bin's "all" class first, then sub-classes inherit a
        # proportional share.
        spend_by_team_day_class: dict = defaultdict(float)
        # Group counts per (team, day): {team_day: {class: count}}
        per_td: dict = defaultdict(lambda: defaultdict(int))
        for (tid, day, cls), n in daily.items():
            per_td[(tid, day)][cls] = n
        for (tid, day), cls_counts in per_td.items():
            total = cls_counts.get("all", 0)
            spend = team_spend_day.get((tid, day), 0.0)
            for cls, n in cls_counts.items():
                if cls == "all":
                    spend_by_team_day_class[(tid, day, cls)] = spend
                else:
                    share = (n / total) if total else 0
                    spend_by_team_day_class[(tid, day, cls)] = (
                        spend * share
                    )

        for (tid, day, cls), n in daily.items():
            cyc = daily_cycle.get((tid, day, cls)) or []
            med = median(cyc) if cyc else None
            p90 = _percentile(cyc, 0.9) if cyc else None
            db.add(TeamDailyMetric(
                team_id=tid,
                day=day,
                pr_class=cls,
                prs_merged=n,
                spend_usd=spend_by_team_day_class.get(
                    (tid, day, cls), 0.0),
                cycle_median_hours=med,
                cycle_p90_hours=p90,
                rolled_up_at=rolled_at,
            ))

        # Weekly rows: roll up daily into the week's start.
        per_tw: dict = defaultdict(lambda: defaultdict(int))
        for (tid, wk, cls), n in weekly.items():
            per_tw[(tid, wk)][cls] = n
        spend_by_tw_class: dict = defaultdict(float)
        # Sum daily spend into weekly buckets.
        for (tid, day, cls), s in spend_by_team_day_class.items():
            wk = _bin_week_start(day)
            if wk >= weekly_floor:
                spend_by_tw_class[(tid, wk, cls)] += s

        for (tid, wk, cls), n in weekly.items():
            cyc = weekly_cycle.get((tid, wk, cls)) or []
            med = median(cyc) if cyc else None
            p90 = _percentile(cyc, 0.9) if cyc else None
            db.add(TeamWeeklyMetric(
                team_id=tid,
                week_start=wk,
                pr_class=cls,
                prs_merged=n,
                spend_usd=spend_by_tw_class.get(
                    (tid, wk, cls), 0.0),
                cycle_median_hours=med,
                cycle_p90_hours=p90,
                rolled_up_at=rolled_at,
            ))

    # ── Jira-aware weekly rollup (#365) ─────────────────
    # Same input set; further-sliced by the linked Jira
    # issue's sprint + fix_version. PRs with no Jira link
    # land in the (sprint_id=0, fix_version="") aggregate
    # row so V&C can show "no-Jira" totals alongside
    # sliced ones. Independent of the main rollup so the
    # existing tests stay untouched.
    jira_rows = _rollup_jira(
        prs=pr_dicts,
        weekly_floor=weekly_floor,
        link_index=link_index,
        user_team=user_team,
        cls_index=cls_index,
        spend_by_tw_class=spend_by_tw_class,
        rolled_at=rolled_at,
    )

    detail = (
        f"daily_rows={len(daily)} weekly_rows={len(weekly)} "
        f"jira_rows={jira_rows} prs_seen={len(prs)}"
    )
    return {"detail": detail}


def _rollup_jira(
    prs, weekly_floor, link_index, user_team,
    cls_index, spend_by_tw_class, rolled_at,
) -> int:
    """Builds jira_weekly_metrics in its own session so
    the Jira-aware view can be queried even when the main
    rollup happens to skip a window. Returns row count."""
    with get_db() as db:
        # PR → list of (sprint_id, fix_version, story_points)
        # for each linked Jira issue. NULL fix_version becomes
        # "" so the composite PK works.
        ref_index: dict = defaultdict(list)
        ref_rows = db.query(PrJiraRef).all()
        issue_index = {
            i.issue_key: i for i in db.query(JiraIssue).all()
        }
        for r in ref_rows:
            issue = issue_index.get(r.issue_key)
            if not issue:
                continue
            try:
                fix_versions = json.loads(issue.fix_versions or "[]")
            except Exception:
                fix_versions = []
            ref_index[(r.repo, r.pr_number)].append({
                "sprint_id":    issue.sprint_id or 0,
                "fix_versions": fix_versions or [""],
                "story_points": issue.story_points,
            })

        # (team, week, class, sprint, fix) -> {prs, sp}
        bins: dict = defaultdict(lambda: {
            "prs": 0, "sp": 0.0, "has_sp": False,
        })

        for pr in prs:
            merged_at = pr["merged_at"]
            if merged_at < weekly_floor:
                continue
            tid = _team_for_login(
                pr["author_login"], link_index, user_team)
            if not tid:
                continue
            repo = pr["repo"]
            pr_number = pr["pr_number"]
            cls = cls_index.get((repo, pr_number), "task")
            wk = _bin_week_start(merged_at)
            refs = ref_index.get((repo, pr_number)) or [
                {"sprint_id": 0, "fix_versions": [""],
                 "story_points": None},
            ]
            # Each (sprint, fix_version) combination from a
            # linked Jira issue gets a row. SP attributed
            # once per PR (use the first ref's SP) so we
            # don't double-count when a PR links multiple
            # issues — the V&C narrative is "$/SP for the
            # work that shipped", and the PR shipped once.
            seen_pr_sp = False
            for ref in refs:
                fvs = ref["fix_versions"] or [""]
                for fv in fvs:
                    sp = ref["story_points"]
                    key_all = (tid, wk, "all", ref["sprint_id"], fv)
                    key_cls = (tid, wk, cls, ref["sprint_id"], fv)
                    bins[key_all]["prs"] += 1
                    bins[key_cls]["prs"] += 1
                    if sp is not None and not seen_pr_sp:
                        bins[key_all]["sp"] += sp
                        bins[key_cls]["sp"] += sp
                        bins[key_all]["has_sp"] = True
                        bins[key_cls]["has_sp"] = True
                        seen_pr_sp = True

        # Truncate the rollup window so re-runs are clean.
        db.query(JiraWeeklyMetric).filter(
            JiraWeeklyMetric.week_start >= weekly_floor,
        ).delete(synchronize_session=False)
        db.flush()

        for (tid, wk, cls, sid, fv), v in bins.items():
            spend = spend_by_tw_class.get((tid, wk, cls), 0.0)
            db.add(JiraWeeklyMetric(
                team_id=tid,
                week_start=wk,
                pr_class=cls,
                sprint_id=sid,
                fix_version=fv,
                prs_merged=v["prs"],
                spend_usd=spend,
                story_points=v["sp"] if v["has_sp"] else None,
                rolled_up_at=rolled_at,
            ))
        return len(bins)
