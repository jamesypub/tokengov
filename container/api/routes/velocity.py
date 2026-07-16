"""
Velocity & Cost endpoints — issue #213.

Phase 3 wires the leaderboard to `team_weekly_metrics` and
`team_daily_metrics` written by `pr_cost_rollup`. Speed view
remains a scaffold until Phase 4 (cycle-time data).
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import (
    GithubActivity, GithubRepo, LinkedAccount,
    PrClassification, CurUserSpend, Team, TeamDailyMetric,
    TeamWeeklyMetric, User,
)
from api.auth import Scope, get_caller_email

router = APIRouter()


def _db():
    with get_db() as db:
        yield db


def _scope(
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


def _flag_on(db: Session) -> bool:
    """Always-on after #276 (V&C is no longer a feature flag).
    Kept as a function so existing call sites stay valid and
    can be removed in a follow-up cleanup pass."""
    return True


def _require_flag(db: Session):
    """No-op after #276."""
    return None


def _visible_team_ids(scope: Scope, db: Session) -> list[str] | None:
    """Returns the team_ids the caller may see; None == all teams."""
    if scope.is_org_admin:
        return None
    if scope.is_team_admin:
        return scope.admin_team_ids
    # Member: their own team only.
    from db.models import User
    u = db.query(User).filter(User.email == scope.email).first()
    return [u.team_id] if u and u.team_id else []


def _scoped_team_filter(
    scope: Scope, db: Session, team: str | None,
) -> list[str] | None:
    """Apply the active-team-switcher selection ON TOP OF the
    role-visibility filter. Returns the team_ids to query.

    - None  → all visible teams (no extra filter).
    - []    → empty result (caller asked for a team they can't see).
    - [...] → just those team_ids.

    `team=*` or no team arg means "all visible teams" (the
    sidebar's Org-all selection). Anything else is intersected
    with the role-visibility set so a member can't escape their
    team via URL hack. (#227)
    """
    visible = _visible_team_ids(scope, db)
    if not team or team == "*":
        return visible
    if visible is None:
        return [team]
    return [team] if team in visible else []


def _bin_day(ts: datetime) -> datetime:
    return ts.replace(
        hour=0, minute=0, second=0, microsecond=0,
    )


def _bin_week_start(ts: datetime) -> datetime:
    d = _bin_day(ts)
    return d - timedelta(days=d.weekday())


def _bins_for(window: str):
    """Returns (table, bin_count, bin_delta, bin_floor_fn).
    bin_floor_fn snaps an arbitrary timestamp to its bin start."""
    now = datetime.now(timezone.utc)
    if window == "7d":
        return (
            TeamDailyMetric, 7, timedelta(days=1), _bin_day,
            _bin_day(now) - timedelta(days=6),
        )
    if window == "30d":
        return (
            TeamWeeklyMetric, 4, timedelta(days=7), _bin_week_start,
            _bin_week_start(now) - timedelta(weeks=3),
        )
    if window == "90d":
        return (
            TeamWeeklyMetric, 13, timedelta(days=7), _bin_week_start,
            _bin_week_start(now) - timedelta(weeks=12),
        )
    if window == "ytd":
        # First Monday of the current year, capped at 52 weeks back.
        year_start = datetime(
            now.year, 1, 1, tzinfo=timezone.utc,
        )
        floor = _bin_week_start(year_start)
        weeks = max(
            1,
            int((_bin_week_start(now) - floor).days / 7) + 1,
        )
        return (
            TeamWeeklyMetric, weeks, timedelta(days=7),
            _bin_week_start, floor,
        )
    raise HTTPException(400, f"unknown window: {window}")


def _trend_label(curr: float, prev: float) -> str:
    """flat if within ±5%, otherwise up/down."""
    if prev <= 0 and curr <= 0:
        return "flat"
    if prev <= 0:
        return "up"
    delta = (curr - prev) / prev
    if abs(delta) < 0.05:
        return "flat"
    return "up" if delta > 0 else "down"


def _trend_pct(curr: float, prev: float) -> int:
    if prev <= 0:
        return 0
    return int(round(100 * (curr - prev) / prev))


@router.get("/velocity/leaderboard")
def velocity_leaderboard(
    window: str = Query("30d", pattern="^(7d|30d|90d|ytd)$"),
    team: str | None = Query(None),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    _require_flag(db)
    visible = _scoped_team_filter(scope, db, team)
    teams_q = db.query(Team)
    if visible is not None:
        teams_q = teams_q.filter(Team.team_id.in_(visible))
    teams = teams_q.order_by(Team.team_id).all()

    table, bin_count, bin_delta, bin_floor_fn, window_floor = (
        _bins_for(window)
    )
    bin_col = (
        TeamDailyMetric.day if table is TeamDailyMetric
        else TeamWeeklyMetric.week_start
    )
    prev_floor = window_floor - bin_count * bin_delta

    # Build a map of bin_starts in chronological order for the window.
    bin_starts: list[datetime] = []
    cur = window_floor
    for _ in range(bin_count):
        bin_starts.append(cur)
        cur = cur + bin_delta

    visible_ids = {t.team_id for t in teams}
    if not visible_ids:
        return _empty_leaderboard(window)

    # Pull rolled-up rows for the current AND previous windows
    # (single query — we'll partition in Python).
    rows = (
        db.query(table)
        .filter(
            table.team_id.in_(visible_ids),
            bin_col >= prev_floor,
            bin_col < window_floor + bin_count * bin_delta,
        )
        .all()
    )

    # by_team[tid][bin_start] = {class: {prs, spend}}
    by_team: dict = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        bs = r.day if table is TeamDailyMetric else r.week_start
        by_team[r.team_id][bs][r.pr_class] = {
            "prs": r.prs_merged or 0,
            "spend": r.spend_usd or 0.0,
        }

    # Devs per team — distinct linked GitHub author count from the
    # window. Cheap because we already pull the small linked_accounts
    # table.
    link_rows = (
        db.query(LinkedAccount)
        .filter(LinkedAccount.vendor == "github")
        .all()
    )
    user_team = dict(
        db.query(User.email, User.team_id)
        .filter(User.email.in_({l.email for l in link_rows}))
        .all()
    )
    devs_by_team: dict = defaultdict(set)
    for l in link_rows:
        tid = user_team.get(l.email)
        if tid:
            devs_by_team[tid].add(l.email)

    repos_by_team = defaultdict(list)
    for gr in db.query(GithubRepo).all():
        if gr.team_id in visible_ids:
            repos_by_team[gr.team_id].append(gr.repo)

    # Org row.
    org_curr_prs = 0
    org_curr_spend = 0.0
    org_prev_prs = 0
    org_prev_spend = 0.0
    org_spark = [0.0] * bin_count

    out_teams = []
    for t in teams:
        bins = by_team.get(t.team_id, {})
        prs_total = 0
        spend_total = 0.0
        spark = []
        for bs in bin_starts:
            cell = bins.get(bs, {}).get("all", {"prs": 0, "spend": 0.0})
            prs_total += cell["prs"]
            spend_total += cell["spend"]
            spark.append(cell["spend"])

        # Previous window
        prev_prs = 0
        prev_spend = 0.0
        for bs, cls_map in bins.items():
            if prev_floor <= bs < window_floor:
                a = cls_map.get("all", {"prs": 0, "spend": 0.0})
                prev_prs += a["prs"]
                prev_spend += a["spend"]

        mix_counts = {"story": 0, "bug": 0, "task": 0}
        for bs in bin_starts:
            for cls in ("story", "bug", "task"):
                cell = bins.get(bs, {}).get(cls, {"prs": 0})
                mix_counts[cls] += cell["prs"]
        mix_total = sum(mix_counts.values()) or 1
        mix_pct = {
            cls: int(round(100 * n / mix_total))
            for cls, n in mix_counts.items()
        }

        dpp = (spend_total / prs_total) if prs_total else 0
        prev_dpp = (prev_spend / prev_prs) if prev_prs else 0
        trend = _trend_label(dpp, prev_dpp) if prev_prs else "flat"

        # Org totals roll up across ALL visible teams (incl.
        # zero-activity), but the leaderboard table only shows
        # teams with merged PRs in the window — zero rows aren't
        # informative and dilute sort behavior.
        org_curr_prs += prs_total
        org_curr_spend += spend_total
        org_prev_prs += prev_prs
        org_prev_spend += prev_spend
        for i, v in enumerate(spark):
            org_spark[i] += v

        if prs_total <= 0:
            continue

        out_teams.append({
            "team_id": t.team_id,
            "name": t.name or t.team_id,
            "devs": len(devs_by_team.get(t.team_id, set())),
            "repos": sorted(repos_by_team.get(t.team_id, [])),
            "spend_usd": round(spend_total, 2),
            "prs_merged": prs_total,
            "dollar_per_pr": round(dpp, 2) if dpp < 1 else int(round(dpp)),
            "sparkline": [round(v, 2) for v in spark],
            "trend": trend,
            "mix_pct": mix_pct,
            "budget_usd": t.budget_usd,
        })

    org_dpp = (org_curr_spend / org_curr_prs) if org_curr_prs else 0
    org_prev_dpp = (
        org_prev_spend / org_prev_prs if org_prev_prs else 0
    )
    org = {
        "spend_usd": round(org_curr_spend, 2),
        "prs_merged": org_curr_prs,
        "dollar_per_pr": int(round(org_dpp)),
        "trend_pct_vs_prev": _trend_pct(org_dpp, org_prev_dpp),
        "sparkline": [round(v, 2) for v in org_spark],
    }
    # #810: report ALL Bedrock spend, not just GitHub-linked devs.
    # `org.spend_usd` above is the PR-attributed rollup ($/PR base);
    # `total_spend_usd` is the bill-reconciling SUM(cur_user_spend)
    # across EVERY principal, and `unlinked` lists the principals
    # (role/machine/federated sessions) that have spend but no GitHub
    # link so nothing is dropped. Org-wide (unlinked principals have
    # no team) → only attached when the caller sees all teams.
    unlinked: list[dict] = []
    if visible is None:
        total_spend, unlinked = _all_spend_unlinked(db, window_floor)
        org["total_spend_usd"] = total_spend
    return {
        "window": window,
        "org": org,
        "teams": out_teams,
        "unlinked": unlinked,
    }


def _empty_leaderboard(window: str) -> dict:
    return {
        "window": window,
        "org": {
            "spend_usd": 0.0, "prs_merged": 0,
            "dollar_per_pr": 0,
            "trend_pct_vs_prev": 0,
            "sparkline": [],
        },
        "teams": [],
    }


def _weighted_median(samples: list[tuple[float | None, int]]):
    """`samples` is a list of (median_hours, n_prs) per bin.
    Returns weight-aggregated median (using the bin medians as
    representative samples)."""
    pts = [(v, n) for v, n in samples if v is not None and n > 0]
    if not pts:
        return None
    # Expand by weight, then take 50th percentile.
    expanded: list[float] = []
    for v, n in pts:
        expanded.extend([v] * n)
    if not expanded:
        return None
    s = sorted(expanded)
    mid = len(s) // 2
    if len(s) % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]


def _weighted_p90(samples: list[tuple[float | None, int]]):
    pts = [(v, n) for v, n in samples if v is not None and n > 0]
    if not pts:
        return None
    expanded: list[float] = []
    for v, n in pts:
        expanded.extend([v] * n)
    if not expanded:
        return None
    s = sorted(expanded)
    k = (len(s) - 1) * 0.9
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _pct(sorted_vals: list, p: int) -> float | None:
    """Simple percentile on a pre-sorted list."""
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _all_spend_unlinked(
    db: Session, window_floor: datetime,
) -> tuple[float, list[dict]]:
    """#810: report ALL Bedrock spend, not just GitHub-linked devs.

    The team leaderboard's per-team `spend_usd` comes from the
    PR-attributed `team_*_metrics` rollup (it drives $/PR), so any
    principal with no GitHub link on a visible team — role sessions,
    machine roles, federated sessions — never appears and its spend
    is silently dropped. This helper returns:

      - total_spend_usd: SUM(cur_user_spend.spend_usd) over the
        window across EVERY principal (the bill-reconciling figure —
        equals the Users page + raw Athena), and
      - an "unlinked / service" list: one row per principal in the
        window whose key is NOT a linked GitHub dev, so the unlinked
        spend is auditable rather than missing.

    Spend is keyed on `cur_user_spend.email` (== identity_key ==
    the role-session-name, #810) — `tg-org-admin+ops@…` is its own row,
    never collapsed to a base email. PR metrics are NA for unlinked
    principals (they have no GitHub-attributed PRs)."""
    from db.models import User
    floor_date = window_floor.date()
    rows = (
        db.query(
            CurUserSpend.email,
            func.sum(CurUserSpend.spend_usd).label("total"),
        )
        .filter(CurUserSpend.usage_hour >= floor_date)
        .group_by(CurUserSpend.email)
        .all()
    )
    total = float(sum((r.total or 0) for r in rows))

    linked = {
        l.email for l in
        db.query(LinkedAccount)
        .filter(LinkedAccount.vendor == "github")
        .all()
    }
    keys = [r.email for r in rows if r.email not in linked]
    users = {
        u.identity_key or u.email: u
        for u in db.query(User).filter(
            (User.identity_key.in_(keys)) | (User.email.in_(keys))
        ).all()
    } if keys else {}

    unlinked = []
    for r in rows:
        if r.email in linked:
            continue
        u = users.get(r.email)
        is_service = bool(
            u and u.principal_type in ("service", "service_linked")
        )
        unlinked.append({
            "identity_key": r.email,
            "email": r.email,
            "spend_usd": round(float(r.total or 0), 2),
            "is_service": is_service,
            "principal_type": (u.principal_type if u else None),
            "prs_merged": None,
            "dollar_per_pr": None,
        })
    unlinked.sort(key=lambda x: x["spend_usd"], reverse=True)
    return round(total, 2), unlinked


def _user_window_spend(
    db: Session, emails: set[str], window_floor: datetime,
) -> dict[str, float]:
    """Per-user spend over [window_floor, now]. #643: reads the
    REAL per-day quota_metrics rows whose usage_date falls in the
    window and sums them — no more month-spread approximation
    (the prior even-spread was an acknowledged simplification;
    per-day rows give the exact figure)."""
    if not emails:
        return {}
    floor_date = window_floor.date()
    rows = (
        db.query(
            CurUserSpend.email,
            func.sum(CurUserSpend.spend_usd).label("total"),
        )
        .filter(
            CurUserSpend.email.in_(emails),
            CurUserSpend.usage_hour >= floor_date,
        )
        .group_by(CurUserSpend.email)
        .all()
    )
    spend: dict[str, float] = {e: 0.0 for e in emails}
    for r in rows:
        spend[r.email] = float(r.total or 0)
    return spend


def _cost_user_breakdown(
    window: str, type: str, team: str,
    scope: Scope, db: Session,
) -> dict:
    """Per-user PR + spend breakdown for one team — the user
    drilldown reached from a team row in V&C → Cost.

    Same shape as the team leaderboard, one level down:
      prs_merged, spend_usd, dollar_per_pr, mix_pct
    Filtered by window + optional pr_class type.
    """
    visible = _scoped_team_filter(scope, db, team)
    if visible is not None and team not in (visible or []):
        return {
            "window": window, "type": type,
            "team": team, "users": [],
        }

    _, _, _, _, window_floor = _bins_for(window)

    links = (
        db.query(LinkedAccount)
        .filter(LinkedAccount.vendor == "github")
        .all()
    )
    handle_to_email = {l.external_handle: l.email for l in links}
    email_to_handle = {l.email: l.external_handle for l in links}

    users = (
        db.query(User)
        .filter(User.team_id == team)
        .all()
    )
    team_emails = {u.email for u in users}
    team_handles = {
        email_to_handle[e] for e in team_emails
        if e in email_to_handle
    }

    spend_by_email = _user_window_spend(
        db, team_emails, window_floor,
    )

    if not team_handles:
        # No linked GitHub authors — but we can still surface
        # spend for team members so the table isn't empty when
        # PRs haven't been linked yet.
        rows_out = []
        for u in users:
            s = spend_by_email.get(u.email, 0.0)
            if s <= 0:
                continue
            rows_out.append({
                "user_id": u.email,
                "email": u.email,
                "prs_merged": 0,
                "spend_usd": round(s, 2),
                "dollar_per_pr": None,
                "mix_pct": {"story": 0, "bug": 0, "task": 0},
            })
        rows_out.sort(
            key=lambda r: r["spend_usd"], reverse=True,
        )
        return {
            "window": window, "type": type,
            "team": team, "users": rows_out,
        }

    # All PRs in window for team's authors.
    all_prs = (
        db.query(GithubActivity)
        .filter(
            GithubActivity.merged_at >= window_floor,
            GithubActivity.author_login.in_(team_handles),
        )
        .all()
    )

    # Build a (repo, pr_number) -> pr_class map. PRs without a
    # classification fall into "task" (matches pr_classify's
    # default verdict).
    cls_rows = db.query(PrClassification).all()
    pr_class_map = {
        (r.repo, r.pr_number): r.pr_class for r in cls_rows
    }

    # type filter + by-author bucketing.
    by_author_in_filter: dict = defaultdict(list)
    by_author_all: dict = defaultdict(list)
    for pr in all_prs:
        cls = pr_class_map.get((pr.repo, pr.pr_number), "task")
        by_author_all[pr.author_login].append((pr, cls))
        if type == "all" or cls == type:
            by_author_in_filter[pr.author_login].append((pr, cls))

    rows_out = []
    for handle, pr_list in by_author_in_filter.items():
        email = handle_to_email.get(handle, handle)
        n = len(pr_list)
        spend = spend_by_email.get(email, 0.0)
        dpp = (spend / n) if n else None

        # Mix is computed over ALL classes the user has in
        # the window (not narrowed by `type`), so the bar
        # always reflects the full breakdown.
        mix_counts = {"story": 0, "bug": 0, "task": 0}
        for _pr, cls in by_author_all.get(handle, []):
            if cls in mix_counts:
                mix_counts[cls] += 1
        total = sum(mix_counts.values()) or 1
        mix_pct = {
            cls: int(round(100 * c / total))
            for cls, c in mix_counts.items()
        }

        rows_out.append({
            "user_id": email,
            "email": email,
            "prs_merged": n,
            "spend_usd": round(spend, 2),
            "dollar_per_pr": (
                round(dpp, 2) if dpp is not None and dpp < 1
                else (int(round(dpp)) if dpp is not None else None)
            ),
            "mix_pct": mix_pct,
        })

    # Highest spend first — matches "top spend" expectations
    # from the team Cost table; the UI can re-sort.
    rows_out.sort(
        key=lambda r: r["spend_usd"], reverse=True,
    )
    return {
        "window": window, "type": type, "team": team,
        "users": rows_out,
    }


@router.get("/velocity/leaderboard/users")
def velocity_leaderboard_users(
    team_id: str = Query(..., min_length=1),
    window: str = Query("30d", pattern="^(7d|30d|90d|ytd)$"),
    type: str = Query("all", pattern="^(all|story|bug|task)$"),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Per-user Cost drilldown for one team. Mirrors the team
    leaderboard one level down (#270)."""
    _require_flag(db)
    return _cost_user_breakdown(window, type, team_id, scope, db)


def _speed_user_breakdown(
    window: str, type: str, team: str,
    scope: Scope, db: Session,
) -> dict:
    """Per-user median/p90 cycle time for a single team."""
    _, _, _, _, window_floor = _bins_for(window)

    links = (
        db.query(LinkedAccount)
        .filter(LinkedAccount.vendor == "github")
        .all()
    )
    handle_to_email = {l.external_handle: l.email for l in links}
    email_to_handle = {l.email: l.external_handle for l in links}

    users = (
        db.query(User.email)
        .filter(User.team_id == team)
        .all()
    )
    team_emails = {u.email for u in users}
    team_handles = {
        email_to_handle[e] for e in team_emails
        if e in email_to_handle
    }

    if not team_handles:
        return {
            "window": window, "type": type,
            "team": team, "users": [],
        }

    q = (
        db.query(GithubActivity)
        .filter(
            GithubActivity.merged_at >= window_floor,
            GithubActivity.author_login.in_(team_handles),
        )
    )
    if type != "all":
        classified = (
            db.query(
                PrClassification.repo,
                PrClassification.pr_number,
            )
            .filter(PrClassification.pr_class == type)
            .all()
        )
        cls_set = {(r.repo, r.pr_number) for r in classified}
        prs = [
            r for r in q.all()
            if (r.repo, r.pr_number) in cls_set
        ]
    else:
        prs = q.all()

    by_author: dict = defaultdict(list)
    for pr in prs:
        by_author[pr.author_login].append(pr)

    rows_out = []
    for handle, pr_list in sorted(by_author.items()):
        email = handle_to_email.get(handle, handle)
        hours = sorted(
            (pr.merged_at - pr.created_at).total_seconds() / 3600
            for pr in pr_list
            if pr.created_at is not None and pr.merged_at is not None
        )
        n = len(pr_list)
        med = _pct(hours, 50)
        p90 = _pct(hours, 90)
        rows_out.append({
            "email": email,
            "prs_merged": n,
            "median_hours": round(med, 2) if med is not None else None,
            "p90_hours": round(p90, 2) if p90 is not None else None,
            "dollar_per_pr": None,
            "trend": None,
        })

    rows_out.sort(key=lambda x: x["median_hours"] or 9e9)
    return {
        "window": window, "type": type, "team": team,
        "users": rows_out,
    }


@router.get("/velocity/speed")
def velocity_speed(
    window: str = Query("30d", pattern="^(7d|30d|90d|ytd)$"),
    type: str = Query("all", pattern="^(all|story|bug|task)$"),
    team: str | None = Query(None),
    breakdown: str = Query("team", pattern="^(team|user)$"),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    _require_flag(db)
    if breakdown == "user":
        if not team:
            raise HTTPException(
                400, "team is required when breakdown=user",
            )
        return _speed_user_breakdown(
            window, type, team, scope, db,
        )
    visible = _scoped_team_filter(scope, db, team)
    teams_q = db.query(Team)
    if visible is not None:
        teams_q = teams_q.filter(Team.team_id.in_(visible))
    teams = teams_q.order_by(Team.team_id).all()

    table, bin_count, bin_delta, _bf, window_floor = _bins_for(window)
    bin_col = (
        TeamDailyMetric.day if table is TeamDailyMetric
        else TeamWeeklyMetric.week_start
    )
    bin_starts: list[datetime] = []
    cur = window_floor
    for _ in range(bin_count):
        bin_starts.append(cur)
        cur = cur + bin_delta
    prev_floor = window_floor - bin_count * bin_delta

    visible_ids = {t.team_id for t in teams}
    if not visible_ids:
        return _empty_speed(window, type)

    rows = (
        db.query(table)
        .filter(
            table.team_id.in_(visible_ids),
            bin_col >= prev_floor,
            bin_col < window_floor + bin_count * bin_delta,
        )
        .all()
    )
    # by_team[tid][bin_start][cls] = row
    by_team: dict = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        bs = r.day if table is TeamDailyMetric else r.week_start
        by_team[r.team_id][bs][r.pr_class] = r

    # Devs + repos (same as leaderboard).
    link_rows = (
        db.query(LinkedAccount)
        .filter(LinkedAccount.vendor == "github")
        .all()
    )
    user_team = dict(
        db.query(User.email, User.team_id)
        .filter(User.email.in_({l.email for l in link_rows}))
        .all()
    )
    devs_by_team: dict = defaultdict(set)
    for l in link_rows:
        tid = user_team.get(l.email)
        if tid:
            devs_by_team[tid].add(l.email)
    repos_by_team = defaultdict(list)
    for gr in db.query(GithubRepo).all():
        if gr.team_id in visible_ids:
            repos_by_team[gr.team_id].append(gr.repo)

    # PR-with-linked-issue counts (drives `with_pr_pct`).
    # In v1, "issues with PR" ≈ count of PRs that have non-empty
    # issue_refs among PRs in the current window (all teams visible).
    issue_count_q = (
        db.query(GithubActivity)
        .filter(GithubActivity.merged_at >= window_floor)
    )
    total_prs_window = issue_count_q.count()
    prs_with_link = 0
    if total_prs_window:
        prs_with_link = sum(
            1 for r in issue_count_q.all()
            if r.issue_refs and r.issue_refs not in ("[]", "null")
        )

    out_teams = []
    org_samples_curr: dict = defaultdict(list)  # cls -> [(med, n)]
    org_samples_prev: dict = defaultdict(list)
    for t in teams:
        bins = by_team.get(t.team_id, {})
        per_class_samples_curr: dict = defaultdict(list)
        per_class_samples_prev: dict = defaultdict(list)
        per_class_dpp: dict = defaultdict(float)
        per_class_prs: dict = defaultdict(int)
        per_class_spend: dict = defaultdict(float)
        per_class_spark: dict = {
            cls: [0.0] * bin_count for cls in
            ("all", "story", "bug", "task")
        }

        for bs, cls_map in bins.items():
            in_curr = bs in bin_starts
            in_prev = prev_floor <= bs < window_floor
            for cls, row in cls_map.items():
                if cls not in ("all", "story", "bug", "task"):
                    continue
                if in_curr:
                    per_class_samples_curr[cls].append(
                        (row.cycle_median_hours, row.prs_merged or 0)
                    )
                    per_class_prs[cls] += row.prs_merged or 0
                    per_class_spend[cls] += row.spend_usd or 0.0
                    idx = bin_starts.index(bs)
                    per_class_spark[cls][idx] = row.cycle_median_hours or 0.0
                    org_samples_curr[cls].append(
                        (row.cycle_median_hours, row.prs_merged or 0)
                    )
                elif in_prev:
                    per_class_samples_prev[cls].append(
                        (row.cycle_median_hours, row.prs_merged or 0)
                    )
                    org_samples_prev[cls].append(
                        (row.cycle_median_hours, row.prs_merged or 0)
                    )

        by_type = {}
        for cls in ("all", "story", "bug", "task"):
            med = _weighted_median(per_class_samples_curr[cls])
            p90 = _weighted_p90(per_class_samples_curr[cls])
            n = per_class_prs[cls]
            spend = per_class_spend[cls]
            dpp = (spend / n) if n else 0
            by_type[cls] = {
                "median_hours": (
                    round(med, 1) if med is not None else None
                ),
                "p90_hours": (
                    round(p90, 1) if p90 is not None else None
                ),
                "dollar_per_pr": (
                    round(dpp, 2) if dpp < 1 else int(round(dpp))
                ),
                "sparkline": [
                    round(v, 1) for v in per_class_spark[cls]
                ],
            }
        # Trend on 'all' median: down=speeding=good, up=slowing=bad.
        med_curr_all = _weighted_median(per_class_samples_curr["all"])
        med_prev_all = _weighted_median(per_class_samples_prev["all"])
        trend = "flat"
        if med_curr_all is not None and med_prev_all:
            delta = (med_curr_all - med_prev_all) / med_prev_all
            if abs(delta) >= 0.05:
                trend = "up" if delta > 0 else "down"

        # Skip teams with no data in the type's filter for current window.
        if by_type[type]["median_hours"] is None:
            continue

        out_teams.append({
            "team_id": t.team_id,
            "name": t.name or t.team_id,
            "devs": len(devs_by_team.get(t.team_id, set())),
            "repos": sorted(repos_by_team.get(t.team_id, [])),
            "by_type": by_type,
            "trend": trend,
        })

    org_med = _weighted_median(org_samples_curr["all"])
    org_p90 = _weighted_p90(org_samples_curr["all"])
    org_med_prev = _weighted_median(org_samples_prev["all"])
    org_trend_pct = 0
    if org_med is not None and org_med_prev:
        org_trend_pct = int(round(
            100 * (org_med - org_med_prev) / org_med_prev
        ))

    return {
        "window": window,
        "type": type,
        "org": {
            "median_hours": (
                round(org_med, 1) if org_med is not None else None
            ),
            "p90_hours": (
                round(org_p90, 1) if org_p90 is not None else None
            ),
            "total_issues": total_prs_window,
            "with_pr_pct": (
                int(round(100 * prs_with_link / total_prs_window))
                if total_prs_window else 0
            ),
            "trend_pct_vs_prev": org_trend_pct,
        },
        "teams": out_teams,
    }


def _empty_speed(window: str, type: str) -> dict:
    return {
        "window": window, "type": type,
        "org": {
            "median_hours": None, "p90_hours": None,
            "total_issues": 0, "with_pr_pct": 0,
            "trend_pct_vs_prev": 0,
        },
        "teams": [],
    }


@router.get("/users/{email}/linked-accounts")
def get_linked_accounts(
    email: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """
    A caller may read linked-accounts for:
      - any user (org_admin)
      - users in their team subtree (team_admin)
      - their own row (member)
    #650: self-or-team-admin tier, same helper as the write routes.
    """
    email = email.lower()
    from db.models import User
    u = db.query(User).filter(User.email == email).first()
    scope.require_self_or_team_admin_for(u or email)

    if not _flag_on(db):
        return []

    rows = (
        db.query(LinkedAccount)
        .filter(LinkedAccount.email == email)
        .all()
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    out = []
    for r in rows:
        pr_count = (
            db.query(GithubActivity)
            .filter(
                GithubActivity.author_login == r.external_handle,
                GithubActivity.merged_at >= cutoff,
            )
            .count()
        )
        out.append({
            "vendor": r.vendor,
            "external_handle": r.external_handle,
            "pr_count_30d": pr_count,
            "linked_at": (
                r.linked_at.isoformat() if r.linked_at else None
            ),
            "linked_by": r.linked_by,
        })
    return out


@router.put("/users/{email}/linked-accounts/{vendor}")
def put_linked_account(
    email: str,
    vendor: str,
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    if vendor != "github":
        raise HTTPException(400, "only github is supported in v1")
    email = email.lower()
    # #650: self-service (own row) OR an admin per the 3-tier rule.
    from db.models import User
    u = db.query(User).filter(User.email == email).first()
    scope.require_self_or_team_admin_for(u or email)

    handle = (body or {}).get("external_handle", "").strip()
    if not handle:
        raise HTTPException(400, "external_handle required")

    row = (
        db.query(LinkedAccount)
        .filter(
            LinkedAccount.email == email,
            LinkedAccount.vendor == vendor,
        )
        .first()
    )
    if row:
        row.external_handle = handle
        row.linked_by = scope.email
    else:
        db.add(LinkedAccount(
            email=email, vendor=vendor,
            external_handle=handle, linked_by=scope.email,
        ))
    db.flush()
    return {}


@router.delete(
    "/users/{email}/linked-accounts/{vendor}",
    status_code=204,
)
def delete_linked_account(
    email: str,
    vendor: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    email = email.lower()
    # #650: was `scope.is_team_admin` (ANY team_admin could unlink
    # ANY user) — now subtree-scoped via the shared helper, matching
    # the PUT route. Self-service or admin-of-this-user only.
    from db.models import User
    u = db.query(User).filter(User.email == email).first()
    scope.require_self_or_team_admin_for(u or email)
    db.query(LinkedAccount).filter(
        LinkedAccount.email == email,
        LinkedAccount.vendor == vendor,
    ).delete()
    db.flush()
    return None
