"""
pr_classify — classifies github_activity rows into pr_classifications.

Probe order (hardcoded in v1):
  1. issue_link  — first linked issue's labels (cross-repo or same-repo).
                   First-match-wins on multi-issue PRs; the rest get
                   recorded in probe_trace as "ignored".
  2. pr_label    — labels on the PR itself.
  3. fallback    — pr_class = 'task' (debt-by-default).

Label → class mapping comes from admin_config.github_label_map
(JSON), defaulting to the same structure as the API route.

Auto-link side-effect: when a PR's author_login matches a User
row's email local-part, write a `linked_accounts` row with
`linked_by='auto'`. This is one of the few ways linked_accounts
populates without an admin click.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import func

from db.session import get_db
from db.models import (
    AdminConfig, GithubActivity, JiraIssue, LinkedAccount,
    PrClassification, PrJiraRef, User,
)

log = logging.getLogger("worker.pr_classify")

DEFAULT_LABEL_MAP = {
    "story": [
        "feature", "enhancement", "epic",
        "type:feat", "user-story",
    ],
    "bug": [
        "bug", "defect", "regression",
        "hotfix", "type:bug",
    ],
    "task": [
        "chore", "docs", "refactor",
        "tech-debt", "deps",
    ],
}


# Jira issue-type → tg pr_class mapping. Customisable via
# admin_config.jira_type_mapping (JSON keyed by tg class
# with arrays of Jira issue types). Falls through to
# `task` for any unmapped type.
DEFAULT_JIRA_TYPE_MAPPING = {
    "story": ["Story", "Improvement", "Epic", "Spike"],
    "bug":   ["Bug", "Defect", "Hotfix"],
    "task":  ["Task", "Sub-task", "Chore"],
}


def _jira_type_mapping() -> dict:
    with get_db() as db:
        row = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == "jira_type_mapping")
            .first()
        )
        raw = row.value if row else None
    if not raw:
        return DEFAULT_JIRA_TYPE_MAPPING
    try:
        m = json.loads(raw)
        return {
            "story": list(m.get("story") or []),
            "bug":   list(m.get("bug") or []),
            "task":  list(m.get("task") or []),
        }
    except Exception:
        return DEFAULT_JIRA_TYPE_MAPPING


def _classify_jira_type(
    issue_type: str | None, mapping: dict,
) -> str | None:
    if not issue_type:
        return None
    norm = issue_type.strip().lower()
    for cls in ("bug", "story", "task"):
        wanted = {
            (t or "").strip().lower()
            for t in mapping.get(cls, [])
        }
        if norm in wanted:
            return cls
    return None


def _label_map() -> dict:
    with get_db() as db:
        row = (
            db.query(AdminConfig)
            .filter(AdminConfig.key == "github_label_map")
            .first()
        )
        raw = row.value if row else None
    if not raw:
        return DEFAULT_LABEL_MAP
    try:
        m = json.loads(raw)
        return {
            "story": list(m.get("story") or []),
            "bug":   list(m.get("bug") or []),
            "task":  list(m.get("task") or []),
        }
    except Exception:
        return DEFAULT_LABEL_MAP


def _normalize(lbl: str) -> str:
    return (lbl or "").strip().lower()


def _match_class(labels: list[str], lmap: dict) -> str | None:
    """Returns the first class whose label-set matches any label."""
    norm = {_normalize(l) for l in labels}
    # priority: bug > story > task — bug is the most actionable
    # signal, task is the fallback bucket.
    for cls in ("bug", "story", "task"):
        wanted = {_normalize(l) for l in lmap.get(cls, [])}
        if norm & wanted:
            return cls
    return None


def classify_one(
    pr: dict,
    issue_label_lookup,
    lmap: dict,
    jira_lookup=None,
    jira_mapping: dict | None = None,
) -> dict:
    """
    pr: {repo, pr_number, labels: [..], issue_refs: [{repo,number}, ...],
         jira_keys: [..]}
    issue_label_lookup: callable(repo, number) -> list[str] | None
    jira_lookup: callable(issue_key) -> issue_type str | None
    Returns {pr_class, classified_by, probe_trace}
    """
    trace = {"probes": []}
    refs = pr.get("issue_refs") or []
    pr_labels = pr.get("labels") or []
    jira_keys = pr.get("jira_keys") or []

    # Probe 0 (NEW): Jira issue-type. First-match-wins on
    # jira_keys; the mapping is admin-configurable and falls
    # through to `task` only when nothing matches a known
    # type. (#364)
    if jira_lookup is not None and jira_keys:
        mapping = jira_mapping or DEFAULT_JIRA_TYPE_MAPPING
        for i, key in enumerate(jira_keys):
            it = jira_lookup(key)
            if it is None:
                trace["probes"].append({
                    "probe": "jira_issue",
                    "ref": key,
                    "result": "not_found",
                })
                continue
            cls = _classify_jira_type(it, mapping)
            if cls:
                trace["probes"].append({
                    "probe": "jira_issue",
                    "ref": key,
                    "issue_type": it,
                    "result": "hit",
                    "class": cls,
                })
                for k in jira_keys[i + 1:]:
                    trace["probes"].append({
                        "probe": "jira_issue",
                        "ref": k,
                        "result": "ignored_first_wins",
                    })
                return {
                    "pr_class": cls,
                    "classified_by": "jira_issue",
                    "probe_trace": trace,
                }
            trace["probes"].append({
                "probe": "jira_issue",
                "ref": key,
                "issue_type": it,
                "result": "no_class_match",
            })

    # Probe 1: issue_link (first-match-wins on refs).
    for i, ref in enumerate(refs):
        labels = issue_label_lookup(ref["repo"], ref["number"])
        if labels is None:
            trace["probes"].append({
                "probe": "issue_link",
                "ref": ref,
                "result": "not_found",
            })
            continue
        cls = _match_class(labels, lmap)
        if cls:
            trace["probes"].append({
                "probe": "issue_link",
                "ref": ref,
                "labels": labels,
                "result": "hit",
                "class": cls,
            })
            # mark remaining refs as ignored
            for r in refs[i + 1:]:
                trace["probes"].append({
                    "probe": "issue_link",
                    "ref": r,
                    "result": "ignored_first_wins",
                })
            return {
                "pr_class": cls,
                "classified_by": "issue_link",
                "probe_trace": trace,
            }
        trace["probes"].append({
            "probe": "issue_link",
            "ref": ref,
            "labels": labels,
            "result": "no_class_label",
        })

    # Probe 2: pr_label.
    cls = _match_class(pr_labels, lmap)
    if cls:
        trace["probes"].append({
            "probe": "pr_label",
            "labels": pr_labels,
            "result": "hit",
            "class": cls,
        })
        return {
            "pr_class": cls,
            "classified_by": "pr_label",
            "probe_trace": trace,
        }
    trace["probes"].append({
        "probe": "pr_label",
        "labels": pr_labels,
        "result": "no_class_label",
    })

    # Probe 3: fallback.
    return {
        "pr_class": "task",
        "classified_by": "fallback",
        "probe_trace": trace,
    }


def _build_issue_label_index(db, prs: list[GithubActivity]) -> dict:
    """
    Cheap index — collect all issue refs across the batch and look
    them up via labels we already cached. v1 uses the **same**
    repo's PR labels as a stand-in (issues and PRs share label
    namespace on GitHub, and we don't fetch issues separately).
    Returns {(repo, number): [labels]}
    """
    needed: set[tuple[str, int]] = set()
    for pr in prs:
        try:
            refs = json.loads(pr.issue_refs or "[]")
        except Exception:
            refs = []
        for ref in refs:
            needed.add((ref["repo"], int(ref["number"])))
    if not needed:
        return {}
    out: dict = {}
    repos = {r for r, _ in needed}
    for repo in repos:
        rows = (
            db.query(GithubActivity)
            .filter(GithubActivity.repo == repo)
            .all()
        )
        for r in rows:
            try:
                lbls = json.loads(r.labels or "[]")
            except Exception:
                lbls = []
            out[(r.repo, r.pr_number)] = lbls
    return out


def _maybe_auto_link(
    db, login: str, recent_authors: dict[str, str],
):
    """If a User.email's local-part matches the GitHub login,
    write a linked_accounts row. Cached in recent_authors so we
    don't re-query for repeated logins."""
    if not login or login in recent_authors:
        return
    existing = (
        db.query(LinkedAccount)
        .filter(
            LinkedAccount.vendor == "github",
            LinkedAccount.external_handle == login,
        )
        .first()
    )
    if existing:
        recent_authors[login] = existing.email
        return
    user = (
        db.query(User)
        .filter(func.lower(User.email).like(f"{login.lower()}@%"))
        .first()
    )
    if user:
        db.add(LinkedAccount(
            email=user.email,
            vendor="github",
            external_handle=login,
            linked_by="auto",
        ))
        recent_authors[login] = user.email
    else:
        recent_authors[login] = ""


def _has_pat() -> bool:
    """Returns True iff a real (non-placeholder) GitHub PAT is
    stored in admin_config. Used to distinguish 'never set up
    GitHub' from 'temporary empty state' when github_activity
    has no rows. (#278)"""
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


def run(full_rebuild: bool = False) -> dict:
    lmap = _label_map()
    jira_mapping = _jira_type_mapping()
    classified = 0
    skipped = 0

    with get_db() as db:
        # Skip cleanly when there's nothing to classify and
        # GitHub was never configured — the PR table is empty
        # because the customer doesn't use the integration,
        # not because of a transient outage. (#278)
        n_activity = db.query(GithubActivity).count()
    if n_activity == 0 and not _has_pat():
        log.info(
            "pr_classify: no github_activity and no PAT, "
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

    with get_db() as db:
        # Always classify recent (last 60d) PRs; full_rebuild
        # forces a full re-pass.
        cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        q = db.query(GithubActivity)
        if not full_rebuild:
            q = q.filter(GithubActivity.merged_at >= cutoff)
        prs = q.all()

        index = _build_issue_label_index(db, prs)

        def lookup(repo, number):
            return index.get((repo, int(number)))

        # Jira lookups: a single batch query keyed by issue_key
        # avoids N+1 round trips through the classify loop.
        jira_keys_by_pr: dict[tuple[str, int], list[str]] = {}
        ref_rows = db.query(PrJiraRef).all()
        for ref in ref_rows:
            jira_keys_by_pr.setdefault(
                (ref.repo, ref.pr_number), []
            ).append(ref.issue_key)
        jira_type_index: dict[str, str] = {
            r.issue_key: r.issue_type
            for r in db.query(JiraIssue).all()
        }

        def jira_lookup(issue_key):
            return jira_type_index.get(issue_key)

        recent_authors: dict[str, str] = {}
        for pr in prs:
            try:
                pr_dict = {
                    "repo": pr.repo,
                    "pr_number": pr.pr_number,
                    "labels": json.loads(pr.labels or "[]"),
                    "issue_refs": json.loads(pr.issue_refs or "[]"),
                    "jira_keys": jira_keys_by_pr.get(
                        (pr.repo, pr.pr_number), []),
                }
            except Exception:
                pr_dict = {
                    "repo": pr.repo,
                    "pr_number": pr.pr_number,
                    "labels": [],
                    "issue_refs": [],
                    "jira_keys": jira_keys_by_pr.get(
                        (pr.repo, pr.pr_number), []),
                }
            verdict = classify_one(
                pr_dict, lookup, lmap,
                jira_lookup=jira_lookup,
                jira_mapping=jira_mapping,
            )

            existing = (
                db.query(PrClassification)
                .filter(
                    PrClassification.repo == pr.repo,
                    PrClassification.pr_number == pr.pr_number,
                )
                .first()
            )
            if existing and not full_rebuild:
                skipped += 1
            else:
                if existing:
                    existing.pr_class = verdict["pr_class"]
                    existing.classified_by = verdict["classified_by"]
                    existing.probe_trace = json.dumps(
                        verdict["probe_trace"])
                    existing.classified_at = datetime.now(
                        timezone.utc)
                else:
                    db.add(PrClassification(
                        repo=pr.repo,
                        pr_number=pr.pr_number,
                        pr_class=verdict["pr_class"],
                        classified_by=verdict["classified_by"],
                        probe_trace=json.dumps(
                            verdict["probe_trace"]),
                    ))
                classified += 1

            # auto-link author email <- login
            _maybe_auto_link(db, pr.author_login, recent_authors)

            # also write author_email back when we know it
            if recent_authors.get(pr.author_login) and \
                    not pr.author_email:
                pr.author_email = recent_authors[pr.author_login]

    detail = (
        f"classified={classified} skipped={skipped} "
        f"total={len(prs)} mode="
        f"{'full' if full_rebuild else 'incremental'}"
    )
    return {"detail": detail}
