"""
jira_synth_seed — synthesises Jira-linked test data inside
the worker container. Mock-mode counterpart to the
host-side scripts/python/jira_synth.py (which keeps the
real-site path because that needs JIRA_TOKEN + outbound
HTTPS the worker container shouldn't carry by default).

Same plan as the host-side generator: deterministic,
50/30/15/5 ratios, 3 sprints round-robin, idempotent on
re-run. Re-runs upsert.

Inputs:
  TG_JIRA_SYNTH_PROJECT    (default: "PROJ")
  TG_JIRA_SYNTH_COUNT      (default: 50)
"""
from __future__ import annotations
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from db.session import get_db
from db.models import GithubActivity, JiraIssue, JiraSite, PrJiraRef
from db.jira_feature import is_jira_enabled

log = logging.getLogger("worker.jira_synth_seed")

# Reserved markers for the synthetic jira_sites row. jira_sync
# detects synthetic sites by `added_by` and skips them entirely
# (no token read, no probe) so sync_status stays 'ok' and the
# V&C Jira surface keeps rendering. The .invalid TLD is RFC 2606
# reserved — it can never resolve, so nothing ever hits the wire.
SYNTH_SITE_URL  = "https://synthetic.invalid"
SYNTH_ADDED_BY  = "jira_synth_seed"
SYNTH_AUTH_EMAIL = "synthetic@synthetic.invalid"


ISSUE_TYPE_RATIOS = [
    ("Story", 0.50),
    ("Bug",   0.30),
    ("Task",  0.15),
    ("Epic",  0.05),
]
SPRINT_NAMES = [
    (10001, "Sprint 24"),
    (10002, "Sprint 25"),
    (10003, "Sprint 26"),
]
SP_BY_TYPE = {
    "Story": [3, 5, 8, 13],
    "Bug":   [1, 2, 3, 5],
    "Task":  [1, 2, 3],
    "Epic":  [21, 34],
}
STATUSES_BY_TYPE = {
    "Story": [
        ("Done", "done"),
        ("In Progress", "indeterminate"),
        ("To Do", "new"),
    ],
    "Bug":   [
        ("Done", "done"),
        ("In Progress", "indeterminate"),
    ],
    "Task":  [("Done", "done"), ("To Do", "new")],
    "Epic":  [
        ("In Progress", "indeterminate"),
        ("Done", "done"),
    ],
}


@dataclass
class IssuePlan:
    issue_key: str
    issue_type: str
    summary: str
    status: str
    status_category: str
    parent_epic_key: str | None
    sprint_id: int | None
    sprint_name: str | None
    story_points: float | None
    fix_versions: list[str]


@dataclass
class PrEditPlan:
    repo: str
    pr_number: int
    issue_key: str


def _ratios_to_counts(total: int) -> list[tuple[str, int]]:
    out: list[list] = []
    running = 0
    for t, r in ISSUE_TYPE_RATIOS:
        n = round(total * r)
        out.append([t, n])
        running += n
    drift = total - running
    out[-1][1] = max(0, out[-1][1] + drift)
    return [(t, n) for t, n in out]


def generate_plan(
    project: str,
    count: int,
    repos_and_prs: list[tuple[str, int]],
    seed_offset: int = 0,
) -> tuple[list[IssuePlan], list[PrEditPlan]]:
    counts = _ratios_to_counts(count)
    issues: list[IssuePlan] = []
    epic_keys: list[str] = []
    next_id = 1 + seed_offset

    for issue_type, n in counts:
        if issue_type != "Epic":
            continue
        for _ in range(n):
            key = f"{project}-{next_id}"
            next_id += 1
            sp_options = SP_BY_TYPE[issue_type]
            sp = sp_options[next_id % len(sp_options)]
            statuses = STATUSES_BY_TYPE[issue_type]
            status, cat = statuses[next_id % len(statuses)]
            issues.append(IssuePlan(
                issue_key=key, issue_type=issue_type,
                summary=f"Epic: initiative {key}",
                status=status, status_category=cat,
                parent_epic_key=None,
                sprint_id=None, sprint_name=None,
                story_points=float(sp),
                fix_versions=["v1.2.0"],
            ))
            epic_keys.append(key)

    for issue_type, n in counts:
        if issue_type == "Epic":
            continue
        for _ in range(n):
            key = f"{project}-{next_id}"
            sprint_id, sprint_name = SPRINT_NAMES[
                next_id % len(SPRINT_NAMES)]
            sp_options = SP_BY_TYPE[issue_type]
            sp = sp_options[next_id % len(sp_options)]
            statuses = STATUSES_BY_TYPE[issue_type]
            status, cat = statuses[next_id % len(statuses)]
            parent = (
                epic_keys[next_id % len(epic_keys)]
                if epic_keys else None
            )
            fv_idx = next_id % 3
            fix_versions = (
                ["v1.2.0"] if fv_idx == 0
                else ["v1.3.0"] if fv_idx == 1
                else []
            )
            issues.append(IssuePlan(
                issue_key=key, issue_type=issue_type,
                summary=f"{issue_type}: {key} synthetic",
                status=status, status_category=cat,
                parent_epic_key=parent,
                sprint_id=sprint_id, sprint_name=sprint_name,
                story_points=float(sp),
                fix_versions=fix_versions,
            ))
            next_id += 1

    non_epic = [i for i in issues if i.issue_type != "Epic"]
    pr_edits: list[PrEditPlan] = []
    if non_epic:
        for i, (repo, pr_num) in enumerate(repos_and_prs):
            issue = non_epic[i % len(non_epic)]
            pr_edits.append(PrEditPlan(
                repo=repo, pr_number=pr_num,
                issue_key=issue.issue_key,
            ))

    return issues, pr_edits


def apply_mock(
    issues: list[IssuePlan],
    pr_edits: list[PrEditPlan],
) -> dict:
    now = datetime.now(timezone.utc)
    issues_inserted = 0
    issues_updated = 0
    refs_inserted = 0
    refs_existing = 0
    prs_rewritten = 0

    with get_db() as db:
        for ip in issues:
            existing = (
                db.query(JiraIssue)
                .filter(JiraIssue.issue_key == ip.issue_key)
                .first()
            )
            payload = dict(
                issue_type=ip.issue_type,
                summary=ip.summary,
                status=ip.status,
                status_category=ip.status_category,
                priority="Medium",
                assignee_email=None,
                reporter_email=None,
                parent_epic_key=ip.parent_epic_key,
                sprint_id=ip.sprint_id,
                sprint_name=ip.sprint_name,
                story_points=ip.story_points,
                fix_versions=json.dumps(ip.fix_versions),
                labels=json.dumps([]),
                resolved_at=now if ip.status_category == "done"
                                else None,
                jira_created_at=now - timedelta(days=14),
                jira_updated_at=now,
                last_synced_at=now,
            )
            if existing:
                for k, v in payload.items():
                    setattr(existing, k, v)
                issues_updated += 1
            else:
                db.add(JiraIssue(
                    issue_key=ip.issue_key, **payload))
                issues_inserted += 1

        for ed in pr_edits:
            existing_ref = (
                db.query(PrJiraRef)
                .filter(
                    PrJiraRef.repo == ed.repo,
                    PrJiraRef.pr_number == ed.pr_number,
                    PrJiraRef.issue_key == ed.issue_key,
                )
                .first()
            )
            if existing_ref:
                refs_existing += 1
            else:
                db.add(PrJiraRef(
                    repo=ed.repo,
                    pr_number=ed.pr_number,
                    issue_key=ed.issue_key,
                    source="title",
                ))
                refs_inserted += 1
            pr = (
                db.query(GithubActivity)
                .filter(
                    GithubActivity.repo == ed.repo,
                    GithubActivity.pr_number == ed.pr_number,
                )
                .first()
            )
            if pr and pr.title and ed.issue_key not in pr.title:
                pr.title = f"{ed.issue_key}: {pr.title}"
                prs_rewritten += 1

    return {
        "issues_inserted": issues_inserted,
        "issues_updated":  issues_updated,
        "refs_inserted":   refs_inserted,
        "refs_existing":   refs_existing,
        "prs_rewritten":   prs_rewritten,
    }


def upsert_synthetic_site(project: str) -> str:
    """Idempotently ensure ONE synthetic jira_sites row exists so
    the V&C Jira surface (_jira_linked) renders the seeded data.

    Keyed on the reserved SYNTH_SITE_URL (unique column), so
    re-runs update in place rather than inserting a duplicate.
    Always (re)asserts sync_status='ok' — if a prior jira_sync
    tick or manual poke flipped it, this restores it.

    Returns "inserted" or "updated".
    """
    with get_db() as db:
        existing = (
            db.query(JiraSite)
            .filter(JiraSite.site_url == SYNTH_SITE_URL)
            .first()
        )
        projects_json = json.dumps([project])
        if existing:
            existing.projects = projects_json
            existing.sync_status = "ok"
            existing.added_by = SYNTH_ADDED_BY
            existing.auth_email = SYNTH_AUTH_EMAIL
            return "updated"
        db.add(JiraSite(
            site_url=SYNTH_SITE_URL,
            auth_email=SYNTH_AUTH_EMAIL,
            api_token_secret_arn=None,
            api_token_plain=None,
            projects=projects_json,
            sync_status="ok",
            added_by=SYNTH_ADDED_BY,
        ))
        return "inserted"


def run() -> dict:
    """Entry point used by the worker / /api/internal/run-job."""
    # #447: gated behind the runtime admin_config flag
    # jira_enabled (default OFF). Skip seeding when the Jira
    # feature is disabled; the job_runner still logs the row.
    with get_db() as db:
        if not is_jira_enabled(db):
            detail = ("skipped: jira feature disabled "
                      "(admin_config.jira_enabled)")
            log.info("jira_synth_seed: %s", detail)
            return {
                "detail": detail,
                "skipped": True,
                "skip_reason": "jira_disabled",
            }

    project = os.environ.get(
        "TG_JIRA_SYNTH_PROJECT", "PROJ")
    count = int(
        os.environ.get("TG_JIRA_SYNTH_COUNT", "50"))

    site_action = upsert_synthetic_site(project)

    with get_db() as db:
        rows = (
            db.query(GithubActivity)
            .order_by(
                GithubActivity.repo,
                GithubActivity.pr_number,
            )
            .limit(count)
            .all()
        )
        repos_and_prs = [(r.repo, r.pr_number) for r in rows]

    issues, pr_edits = generate_plan(
        project, count, repos_and_prs,
    )
    counts = apply_mock(issues, pr_edits)
    detail = (
        f"project={project} count={count} "
        f"site={site_action} "
        f"issues_inserted={counts['issues_inserted']} "
        f"issues_updated={counts['issues_updated']} "
        f"refs_inserted={counts['refs_inserted']} "
        f"refs_existing={counts['refs_existing']} "
        f"prs_rewritten={counts['prs_rewritten']}"
    )
    log.info("jira_synth_seed: %s", detail)
    return {"detail": detail, "site_action": site_action, **counts}
