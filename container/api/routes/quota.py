from __future__ import annotations
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import func

from db.session import get_db
from db.models import CurUserSpend, User, GithubActivity, LinkedAccount
from db.usage_windows import window_start_utc
from db.org_config import (
    get_spend_estimate_strategy, get_spend_estimate_enforcement,
    get_org_default_quota_usd,
)
from db.spend_estimate import project_for_principal
from api.auth import get_caller_email, Scope
# #436: reuse the Users-page principal classification so
# "managed" means exactly the same thing on both surfaces.
from api.routes.users import _is_managed

router = APIRouter()


def _row_is_managed(r) -> bool:
    """`_is_managed` only reads .principal_type/.principal_arn,
    both of which the usage query labels onto each row — so the
    same predicate classifies a query row by duck typing. A
    CurUserSpend with no matching User row (outer join → null
    principal fields) is treated as unmanaged."""
    return _is_managed(r)


def _db():
    with get_db() as db:
        yield db


def _scope(
    request: Request,
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


@router.get("/activity")
def get_activity(
    team: str = "*",
    window: str = "mtd",
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    # #643: per-day grain → sum over a usage_date window. window ∈
    # 7d|30d|mtd (default mtd preserves prior month-to-date).
    start = window_start_utc(window)
    q = db.query(
        CurUserSpend.email,
        CurUserSpend.model_id,
        func.sum(CurUserSpend.input_tokens).label("input_tokens"),
        func.sum(CurUserSpend.output_tokens).label("output_tokens"),
        func.sum(CurUserSpend.cache_write_tokens).label("cache_write_tokens"),
        func.sum(CurUserSpend.cache_read_tokens).label("cache_read_tokens"),
        func.sum(CurUserSpend.total_tokens).label("total_tokens"),
        func.sum(CurUserSpend.spend_usd).label("spend_usd"),
    )
    if start is not None:
        q = q.filter(CurUserSpend.usage_hour >= start)

    if not scope.is_org_admin:
        if scope.admin_team_ids:
            emails = [
                u.email for u in db.query(User.email)
                .filter(User.team_id.in_(scope.admin_team_ids)).all()
            ]
            q = q.filter(CurUserSpend.email.in_(emails))

    if team and team != "*":
        emails = [
            u.email for u in db.query(User.email)
            .filter(User.team_id == team).all()
        ]
        q = q.filter(CurUserSpend.email.in_(emails))

    rows = q.group_by(CurUserSpend.email, CurUserSpend.model_id).all()

    # Group by user
    by_user: dict = {}
    for r in rows:
        e = r.email
        if e not in by_user:
            by_user[e] = {"email": e, "models": [], "total_tokens": 0, "spend_usd": 0.0}
        by_user[e]["models"].append({
            "model_id":     r.model_id,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cache_write_tokens": r.cache_write_tokens or 0,
            "cache_read_tokens":  r.cache_read_tokens or 0,
            "total_tokens": r.total_tokens,
            "spend_usd":    round(float(r.spend_usd or 0), 4),
        })
        by_user[e]["total_tokens"] += r.total_tokens or 0
        by_user[e]["spend_usd"]    += float(r.spend_usd or 0)

    result = sorted(by_user.values(), key=lambda x: -x["spend_usd"])
    # Spend projection: each row carries billed (= spend_usd) plus an
    # estimated-unbilled badge so the UI can show billed-vs-estimated
    # without blending them. billed stays the authoritative primary
    # figure; the estimate fills the CUR lag window from the user's own
    # recent billed rate (strategy = admin-configurable). MTD is the
    # cap-relevant window; the projection rides on every window so the
    # badge is always available.
    strategy = get_spend_estimate_strategy(db)
    enforcement = get_spend_estimate_enforcement(db)
    # Per-user effective cap (user override → org default) so the warn
    # comparison can fire. The estimate-warn flag is meaningful only on
    # the cap-relevant MTD window; for other windows the cap is exposed
    # but the over-projection flag stays false (billed there isn't the
    # month-to-date the cap measures).
    org_default_cap = get_org_default_quota_usd(db)
    cap_emails = [r["email"] for r in result]
    cap_by_email = {}
    if cap_emails:
        for u in (
            db.query(User.email, User.cap_usd)
            .filter(User.email.in_(cap_emails)).all()
        ):
            cap_by_email[u.email] = (
                float(u.cap_usd) if u.cap_usd is not None
                else org_default_cap)
    for r in result:
        r["spend_usd"] = round(r["spend_usd"], 4)
        proj = project_for_principal(
            db, r["email"],
            billed_mtd=r["spend_usd"],
            strategy=strategy)
        r["billed"]         = proj["billed"]
        r["estimated"]      = proj["estimated"]
        r["projected"]      = proj["projected"]
        r["unbilled_hours"] = proj["unbilled_hours"]
        r["estimate_low_sample"] = proj["low_sample"]
        # warn-mode signal: the ESTIMATE is what crosses the cap —
        # billed alone is still under, but billed + estimated >= cap.
        # (An already-billed-over user is a normal billed-over case, not
        # an estimate warning.) Only on MTD, where billed == the
        # cap-measured month-to-date. The UI shows the warning badge
        # only when enforcement == 'warn'; the flag is computed
        # regardless so off/enforce can ignore it.
        cap = cap_by_email.get(r["email"])
        r["cap_usd"] = cap
        r["projected_over_cap"] = bool(
            window == "mtd" and cap is not None and cap > 0
            and r["billed"] < cap and r["projected"] >= cap)
    return {
        "window": window,
        "rows": result,
        "estimate_strategy": strategy,
        "estimate_enforcement": enforcement,
    }


def _team_filtered_rows(
    db: Session, scope: Scope, team: str, window: str = "mtd"
):
    """Shared filter for /api/usage + /api/summary. #643: rows are
    now per (email, usage_date, model); we sum over the requested
    window's usage_date range. Returns (window, rows) — callers
    aggregate per (email, model) across the day rows."""
    start = window_start_utc(window)
    q = db.query(
        CurUserSpend.email,
        CurUserSpend.model_id.label("model"),
        CurUserSpend.input_tokens,
        CurUserSpend.output_tokens,
        CurUserSpend.cache_write_tokens,
        CurUserSpend.cache_read_tokens,
        CurUserSpend.total_tokens,
        CurUserSpend.spend_usd,
        User.team_id.label("team_id"),
        # #436: principal shape so the Activity page can filter
        # managed vs unmanaged principals consuming tokens.
        User.principal_type.label("principal_type"),
        User.principal_arn.label("principal_arn"),
    ).outerjoin(
        User, User.email == CurUserSpend.email
    )
    if start is not None:
        q = q.filter(CurUserSpend.usage_hour >= start)

    if not scope.is_org_admin:
        if scope.admin_team_ids:
            scoped_emails = [
                u.email for u in db.query(User.email)
                .filter(
                    User.team_id.in_(scope.admin_team_ids)
                ).all()
            ]
            q = q.filter(
                CurUserSpend.email.in_(scoped_emails))
        else:
            # Plain member sees only their own usage
            q = q.filter(CurUserSpend.email == scope.email)

    if team and team != "*":
        emails = [
            u.email for u in db.query(User.email)
            .filter(User.team_id == team).all()
        ]
        q = q.filter(CurUserSpend.email.in_(emails))

    return window, q.all()


def _prs_merged_30d(db: Session, emails: set) -> dict:
    """Returns {email: pr_count} for the last 30 days."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    links = (
        db.query(LinkedAccount)
        .filter(
            LinkedAccount.vendor == "github",
            LinkedAccount.email.in_(emails),
        )
        .all()
    )
    handle_to_email = {l.external_handle: l.email for l in links}
    if not handle_to_email:
        return {}
    rows = (
        db.query(
            GithubActivity.author_login,
            func.count(GithubActivity.id).label("cnt"),
        )
        .filter(
            GithubActivity.author_login.in_(handle_to_email),
            GithubActivity.merged_at >= cutoff,
        )
        .group_by(GithubActivity.author_login)
        .all()
    )
    result: dict = {}
    for r in rows:
        email = handle_to_email.get(r.author_login)
        if email:
            result[email] = result.get(email, 0) + r.cnt
    return result


@router.get("/usage")
def get_usage(
    team: str = "*",
    window: str = "mtd",
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """
    Flat per-(user, model) usage rows over the requested window
    (7d|30d|mtd, default mtd). Consumed by the Activity page.

    #643: query rows are now per-day, so the same (email, model)
    spans multiple usage_date rows — roll them up per (email,
    model) here so the Activity page still sees one row per
    user-model with the window's totals.
    """
    window, rows = _team_filtered_rows(db, scope, team, window)
    emails = {r.email for r in rows}
    prs_by_email = _prs_merged_30d(db, emails)
    agg: dict = {}
    for r in rows:
        key = (r.email, r.model)
        o = agg.get(key)
        if o is None:
            o = {
                "email":             r.email,
                "team_id":           r.team_id,
                "model":             r.model,
                "input_tokens":      0,
                "output_tokens":     0,
                "cache_write_tokens": 0,
                "cache_read_tokens":  0,
                "total_tokens":      0,
                "spend_usd":         0.0,
                "prs_merged_30d":    prs_by_email.get(r.email, 0),
                # #436: managed flag (same classification the Users
                # page uses) so Activity can filter unmanaged
                # principals consuming tokens.
                "managed":           _row_is_managed(r),
            }
            agg[key] = o
        o["input_tokens"]       += r.input_tokens or 0
        o["output_tokens"]      += r.output_tokens or 0
        o["cache_write_tokens"] += r.cache_write_tokens or 0
        o["cache_read_tokens"]  += r.cache_read_tokens or 0
        o["total_tokens"]       += r.total_tokens or 0
        o["spend_usd"]          += float(r.spend_usd or 0)
    # warn-mode signal (per user, applied to that user's rows): billed
    # alone under cap but billed + estimated unbilled >= cap. Computed
    # once per distinct email over MTD (the cap-measured window); the
    # Activity UI shows the warning only when enforcement == 'warn'.
    enforcement = get_spend_estimate_enforcement(db)
    over_by_email: dict = {}
    if window == "mtd" and emails:
        strategy = get_spend_estimate_strategy(db)
        org_default_cap = get_org_default_quota_usd(db)
        cap_by_email = {}
        for u in (
            db.query(User.email, User.cap_usd)
            .filter(User.email.in_(list(emails))).all()
        ):
            cap_by_email[u.email] = (
                float(u.cap_usd) if u.cap_usd is not None
                else org_default_cap)
        # billed MTD per email (sum of this user's rows already summed
        # per model in agg — re-sum to the user level).
        billed_by_email: dict = {}
        for o in agg.values():
            billed_by_email[o["email"]] = (
                billed_by_email.get(o["email"], 0.0) + o["spend_usd"])
        for em in emails:
            cap = cap_by_email.get(em)
            if not (cap and cap > 0):
                continue
            billed = round(billed_by_email.get(em, 0.0), 4)
            if billed >= cap:
                continue  # already billed-over — not an estimate warning
            try:
                proj = project_for_principal(
                    db, em, billed_mtd=billed, strategy=strategy)
                if proj["projected"] >= cap:
                    over_by_email[em] = True
            except Exception:  # noqa: BLE001 — non-fatal decoration
                pass
    out = []
    for o in agg.values():
        o["spend_usd"] = round(o["spend_usd"], 4)
        o["projected_over_cap"] = bool(over_by_email.get(o["email"]))
        out.append(o)
    return {
        "window": window,
        "rows": out,
        "estimate_enforcement": enforcement,
    }


@router.get("/summary")
def get_summary(
    team: str = "*",
    window: str = "mtd",
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """
    Summary-card numbers, scoped to the caller's role/team and the
    requested window. Surfaced on the Users page (the Activity header
    was removed). Returns: window, active_users (in-window emails),
    approaching_cap_count (>=90% of cap, non-blocked), blocked_count
    (persisted User.status, NOT spend), total_spend_usd (sum MTD spend).
    """
    window, rows = _team_filtered_rows(db, scope, team, window)
    emails = {r.email for r in rows}
    active_users = len(emails)

    # Per-user spend totals + the in-scope MTD spend sum.
    spend_by_email: dict = {}
    for r in rows:
        spend_by_email[r.email] = (
            spend_by_email.get(r.email, 0.0)
            + float(r.spend_usd or 0)
        )
    total_spend_usd = round(sum(spend_by_email.values()), 4)

    # Blocked count is sourced from PERSISTED User.status over the
    # role-scoped MEMBERSHIP, NOT the windowed-spend `emails` set: a
    # blocked user who stopped invoking (or was force-blocked under cap)
    # has no rows this window and would be missed. Apply the SAME role
    # predicate _team_filtered_rows uses internally so Blocked == the
    # Users-table blocked set by construction.
    member_q = db.query(User.email, User.status, User.cap_usd)
    if not scope.is_org_admin:
        if scope.admin_team_ids:
            member_q = member_q.filter(
                User.team_id.in_(scope.admin_team_ids))
        else:
            member_q = member_q.filter(User.email == scope.email)
    if team and team != "*":
        member_q = member_q.filter(User.team_id == team)
    members = member_q.all()

    _BLOCKED = ("blocked", "force_blocked")
    blocked_emails = {m.email for m in members if m.status in _BLOCKED}
    blocked_count = len(blocked_emails)

    # Caps from the same scoped membership (so an in-window user that
    # joined a different scope can't leak a cap in).
    user_caps = {m.email: (m.cap_usd or 0) for m in members}

    # Approaching cap: in-window emails with cap>0 and spend/cap >= 0.90,
    # EXCLUDING anyone counted as blocked (no double-count). Ungoverned
    # users (cap null/0) naturally fall out — cap > 0 is false.
    approaching_cap_count = 0
    for e, spend in spend_by_email.items():
        if e in blocked_emails:
            continue
        cap = user_caps.get(e, 0)
        if cap > 0 and spend / cap >= 0.90:
            approaching_cap_count += 1

    return {
        "window":                window,
        "active_users":          active_users,
        "approaching_cap_count": approaching_cap_count,
        "blocked_count":         blocked_count,
        "total_spend_usd":       total_spend_usd,
    }
