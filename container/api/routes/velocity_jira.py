"""
Jira-aware Velocity & Cost endpoints — issue #365.

Sits alongside the v1 /velocity routes, hitting the new
jira_weekly_metrics table that pr_cost_rollup populates
(when Jira refs exist). All endpoints degrade gracefully
to "no Jira data yet" responses, which the SPA uses to
hide the Jira-only UI bits.

Surfaces:
  GET /velocity/jira/sprints   — active + recent sprints
  GET /velocity/jira/epics     — epics with linked PRs
  GET /velocity/jira/series    — weekly series filtered by
                                 sprint/epic/fix_version,
                                 returns $/SP when SP data
                                 exists in the window.
  GET /velocity/jira/sprint/{sprint_id}
                               — sprint-detail aggregate
                                 (stories shipped, SP burn,
                                 by-epic, carry-over).
"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import distinct
from sqlalchemy.orm import Session

from api.auth import Scope, get_caller_email
from db.session import get_db
from db.models import (
    GithubActivity, JiraIssue, JiraSite, JiraWeeklyMetric,
    PrClassification, PrJiraRef, Team,
)

router = APIRouter()


def _db():
    with get_db() as db:
        yield db


def _scope(
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


def _visible_team_ids(
    scope: Scope, db: Session,
) -> list[str] | None:
    if scope.is_org_admin:
        return None
    if not scope.is_team_admin:
        return []
    return scope.admin_team_ids


def _jira_linked(db: Session) -> bool:
    """True iff at least one jira_sites row exists with
    sync_status='ok'. The SPA gates Jira UI on this — no
    point in showing Sprint dropdown when extraction can
    never have happened."""
    row = (
        db.query(JiraSite)
        .filter(JiraSite.sync_status == "ok")
        .first()
    )
    return row is not None


def _window_floor(window: str) -> datetime:
    now = datetime.now(timezone.utc)
    if window == "7d":
        return now - timedelta(days=7)
    if window == "30d":
        return now - timedelta(days=30)
    if window == "90d":
        return now - timedelta(days=90)
    if window == "ytd":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc)
    return now - timedelta(days=30)


@router.get("/velocity/jira/sprints")
def list_sprints(
    window: str = Query("90d",
                        pattern="^(30d|90d|ytd)$"),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Active + recent sprints derived from jira_issues.

    `active` is the heuristic "issues whose status_category
    isn't 'done' AND sprint isn't null"; the rest are the
    most-recently-active distinct sprints in the window.
    """
    if not _jira_linked(db):
        return {
            "linked":  False,
            "sprints": [],
        }

    floor = _window_floor(window)
    rows = (
        db.query(
            JiraIssue.sprint_id, JiraIssue.sprint_name,
            JiraIssue.status_category,
        )
        .filter(
            JiraIssue.sprint_id.isnot(None),
            JiraIssue.jira_updated_at >= floor,
        )
        .all()
    )
    by_sprint: dict[int, dict] = {}
    for sid, sname, cat in rows:
        if sid is None:
            continue
        s = by_sprint.setdefault(sid, {
            "id": sid,
            "name": sname or f"Sprint {sid}",
            "active": False,
            "issue_count": 0,
        })
        s["issue_count"] += 1
        if cat != "done":
            s["active"] = True
    sprints = sorted(
        by_sprint.values(),
        key=lambda s: (not s["active"], -s["id"]),
    )[:10]
    return {
        "linked":  True,
        "sprints": sprints,
    }


@router.get("/velocity/jira/epics")
def list_epics(
    window: str = Query("90d",
                        pattern="^(30d|90d|ytd)$"),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Epics with at least one linked PR in the window."""
    if not _jira_linked(db):
        return {"linked": False, "epics": []}

    floor = _window_floor(window)
    # Distinct parent_epic_key of issues referenced by PRs
    # merged in the window.
    rows = (
        db.query(
            JiraIssue.parent_epic_key,
            JiraIssue.summary,
        )
        .join(
            PrJiraRef,
            PrJiraRef.issue_key == JiraIssue.issue_key,
        )
        .join(
            GithubActivity,
            (GithubActivity.repo == PrJiraRef.repo) &
            (GithubActivity.pr_number == PrJiraRef.pr_number),
        )
        .filter(
            JiraIssue.parent_epic_key.isnot(None),
            GithubActivity.merged_at >= floor,
        )
        .distinct()
        .all()
    )
    # Hydrate epic summaries from a separate JiraIssue lookup.
    epic_keys = {r[0] for r in rows if r[0]}
    if not epic_keys:
        return {"linked": True, "epics": []}
    epic_rows = (
        db.query(JiraIssue)
        .filter(JiraIssue.issue_key.in_(epic_keys))
        .all()
    )
    epics = [
        {
            "key":     e.issue_key,
            "summary": e.summary or "",
            "status":  e.status,
        }
        for e in sorted(epic_rows, key=lambda x: x.issue_key)
    ]
    return {"linked": True, "epics": epics}


@router.get("/velocity/jira/series")
def jira_series(
    window: str = Query("30d",
                        pattern="^(7d|30d|90d|ytd)$"),
    team: str | None = Query(None),
    sprint_id: int | None = Query(None),
    epic_key: str | None = Query(None),
    fix_version: str | None = Query(None),
    pr_class: str = Query("all",
                          pattern="^(all|story|bug|task)$"),
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Weekly time-series of prs_merged, spend, story_points,
    and computed cost_per_story_point, filtered by
    sprint/epic/fix_version when set.

    Epic filtering pivots through pr_jira_refs because
    jira_weekly_metrics doesn't carry epic_key directly
    (epics are an N-to-many parent-of relationship).
    """
    if not _jira_linked(db):
        return {
            "linked":     False,
            "weeks":      [],
            "totals":     None,
        }

    floor = _window_floor(window)

    visible = _visible_team_ids(scope, db)
    q = db.query(JiraWeeklyMetric).filter(
        JiraWeeklyMetric.week_start >= floor,
        JiraWeeklyMetric.pr_class == pr_class,
    )
    if visible is not None:
        if not visible:
            return {
                "linked":     True,
                "weeks":      [],
                "totals":     None,
            }
        q = q.filter(JiraWeeklyMetric.team_id.in_(visible))
    if team:
        q = q.filter(JiraWeeklyMetric.team_id == team)
    if sprint_id is not None:
        q = q.filter(JiraWeeklyMetric.sprint_id == sprint_id)
    if fix_version is not None:
        q = q.filter(JiraWeeklyMetric.fix_version == fix_version)

    rows = q.all()

    # Epic filtering: keep only weeks where some PR linked to
    # an issue under this epic merged. Implemented as a
    # post-filter to avoid a wide join in the rollup path.
    if epic_key:
        epic_issue_keys = {
            r.issue_key
            for r in db.query(JiraIssue)
            .filter(JiraIssue.parent_epic_key == epic_key)
            .all()
        }
        if not epic_issue_keys:
            return {
                "linked": True, "weeks": [], "totals": None,
            }
        weeks_with_epic_prs = set(
            r[0]
            for r in db.query(
                JiraWeeklyMetric.week_start,
            )
            .join(PrJiraRef,
                  PrJiraRef.issue_key.in_(epic_issue_keys))
            .filter(JiraWeeklyMetric.week_start >= floor)
            .distinct()
            .all()
        )
        rows = [
            r for r in rows
            if r.week_start in weeks_with_epic_prs
        ]

    # Aggregate per week.
    per_week: dict[datetime, dict] = defaultdict(lambda: {
        "prs_merged": 0,
        "spend_usd": 0.0,
        "story_points": 0.0,
        "has_sp": False,
    })
    for r in rows:
        w = per_week[r.week_start]
        w["prs_merged"] += r.prs_merged
        w["spend_usd"] += r.spend_usd
        if r.story_points is not None:
            w["story_points"] += r.story_points
            w["has_sp"] = True

    weeks = []
    total_prs = 0
    total_spend = 0.0
    total_sp = 0.0
    any_sp = False
    for ws in sorted(per_week):
        w = per_week[ws]
        sp = w["story_points"] if w["has_sp"] else None
        cps = (
            w["spend_usd"] / w["story_points"]
            if w["has_sp"] and w["story_points"] > 0 else None
        )
        weeks.append({
            "week_start":            ws.isoformat(),
            "prs_merged":            w["prs_merged"],
            "spend_usd":             round(w["spend_usd"], 2),
            "story_points":          sp,
            "cost_per_story_point":  (
                round(cps, 2) if cps is not None else None
            ),
        })
        total_prs += w["prs_merged"]
        total_spend += w["spend_usd"]
        if w["has_sp"]:
            total_sp += w["story_points"]
            any_sp = True

    totals_cps = (
        round(total_spend / total_sp, 2)
        if any_sp and total_sp > 0 else None
    )
    totals = {
        "prs_merged":           total_prs,
        "spend_usd":            round(total_spend, 2),
        "story_points":         total_sp if any_sp else None,
        "cost_per_story_point": totals_cps,
    }
    return {
        "linked": True,
        "weeks":  weeks,
        "totals": totals,
    }


@router.get("/velocity/jira/sprint/{sprint_id}")
def sprint_detail(
    sprint_id: int,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Sprint-shaped aggregate: stories shipped vs committed,
    SP burn, $/SP, by-epic table, carry-over to next sprint.
    """
    if not _jira_linked(db):
        raise HTTPException(404, "no Jira site linked")

    issues = (
        db.query(JiraIssue)
        .filter(JiraIssue.sprint_id == sprint_id)
        .all()
    )
    if not issues:
        raise HTTPException(
            404, f"no issues found for sprint {sprint_id}")
    sprint_name = issues[0].sprint_name or f"Sprint {sprint_id}"

    # Stories vs everything else
    stories = [i for i in issues if i.issue_type in (
        "Story", "Improvement", "Epic", "Spike",
    )]
    shipped_keys = {
        i.issue_key for i in issues if i.status_category == "done"
    }
    committed = len(stories)
    shipped = sum(
        1 for s in stories
        if s.issue_key in shipped_keys
    )
    carry_over = committed - shipped

    sp_committed = sum(
        i.story_points or 0 for i in stories
        if i.story_points is not None
    )
    sp_shipped = sum(
        i.story_points or 0 for i in stories
        if i.story_points is not None
        and i.issue_key in shipped_keys
    )

    # Spend for this sprint = sum of jira_weekly_metrics rows
    # filtered to sprint_id (across visible teams).
    visible = _visible_team_ids(scope, db)
    q = db.query(JiraWeeklyMetric).filter(
        JiraWeeklyMetric.sprint_id == sprint_id,
        JiraWeeklyMetric.pr_class == "all",
    )
    if visible is not None:
        if not visible:
            sprint_spend = 0.0
        else:
            q = q.filter(
                JiraWeeklyMetric.team_id.in_(visible))
            sprint_spend = sum(r.spend_usd or 0 for r in q.all())
    else:
        sprint_spend = sum(r.spend_usd or 0 for r in q.all())

    cps = (
        round(sprint_spend / sp_shipped, 2)
        if sp_shipped > 0 else None
    )

    # By-epic breakdown — count per parent_epic_key.
    by_epic: dict[str, dict] = defaultdict(lambda: {
        "shipped": 0,
        "committed": 0,
        "story_points_shipped": 0.0,
    })
    for i in issues:
        ek = i.parent_epic_key or "(no epic)"
        e = by_epic[ek]
        if i.issue_type in ("Story", "Improvement",
                             "Epic", "Spike"):
            e["committed"] += 1
            if i.issue_key in shipped_keys:
                e["shipped"] += 1
                if i.story_points is not None:
                    e["story_points_shipped"] += i.story_points
    epics = [
        {
            "epic_key": k,
            "shipped":  v["shipped"],
            "committed": v["committed"],
            "story_points_shipped": v["story_points_shipped"],
        }
        for k, v in sorted(by_epic.items())
    ]

    return {
        "sprint_id":            sprint_id,
        "sprint_name":          sprint_name,
        "committed_stories":    committed,
        "shipped_stories":      shipped,
        "carry_over_stories":   carry_over,
        "story_points_committed": sp_committed,
        "story_points_shipped":   sp_shipped,
        "spend_usd":             round(sprint_spend, 2),
        "cost_per_story_point":  cps,
        "by_epic":               epics,
    }


@router.get("/velocity/jira/pr-refs")
def pr_jira_refs(
    repo: str = Query(...),
    pr_numbers: str = Query("",
                            description="comma-separated"),
    db: Session = Depends(_db),
    scope: Scope = Depends(_scope),
):
    """Returns a {repo, pr_number} → [{issue_key, issue_type,
    summary, status}] map for the requested PRs. The drill-
    down table uses this to render Jira badges.

    Bounded to keep payloads small — the SPA pages PRs anyway.
    """
    if not _jira_linked(db):
        return {"linked": False, "refs": {}}
    nums = [
        int(n) for n in pr_numbers.split(",")
        if n.strip().isdigit()
    ]
    if not nums:
        return {"linked": True, "refs": {}}

    refs = (
        db.query(PrJiraRef)
        .filter(
            PrJiraRef.repo == repo,
            PrJiraRef.pr_number.in_(nums),
        )
        .all()
    )
    keys = {r.issue_key for r in refs}
    issues = {
        i.issue_key: i for i in db.query(JiraIssue)
        .filter(JiraIssue.issue_key.in_(keys))
        .all()
    } if keys else {}

    out: dict[int, list] = defaultdict(list)
    for r in refs:
        i = issues.get(r.issue_key)
        out[r.pr_number].append({
            "issue_key": r.issue_key,
            "source":    r.source,
            "issue_type": (i.issue_type if i else None),
            "summary":   (i.summary if i else None),
            "status":    (i.status if i else None),
        })
    # Also fetch classification probe trace for drilldown
    # tooltip ("Class verdict came from: jira_issue/.../").
    cls_rows = (
        db.query(PrClassification)
        .filter(
            PrClassification.repo == repo,
            PrClassification.pr_number.in_(nums),
        )
        .all()
    )
    classifications = {
        c.pr_number: {
            "pr_class": c.pr_class,
            "classified_by": c.classified_by,
            "probe_trace": (
                json.loads(c.probe_trace)
                if c.probe_trace else None
            ),
        }
        for c in cls_rows
    }

    return {
        "linked": True,
        "refs": {str(k): v for k, v in out.items()},
        "classifications": {
            str(k): v for k, v in classifications.items()
        },
    }
