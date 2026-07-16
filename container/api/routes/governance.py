from __future__ import annotations
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import GovernanceDrift
from db.drift_sweep import get_last_drift_sweep_at
from api.auth import get_caller_email, Scope

router = APIRouter()


def _db():
    with get_db() as db:
        yield db


def _scope(
    request: Request,
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


def _last_checked_iso(db: Session) -> str | None:
    """The banner's "last checked" time = the latest COMPLETED sweep,
    from the last_drift_sweep_at marker. Under the clean-slate model
    the drift table holds ONLY the current sweep's rows, so a clean
    sweep leaves it empty and can't carry a timestamp — the marker
    does. Returns None only if no sweep has ever run."""
    when = get_last_drift_sweep_at(db)
    return when.isoformat() if when else None


def _row(d: GovernanceDrift) -> dict:
    # role_type lets the UI branch the governed_no_deny remedy: an IDC
    # (AWSReservedSSO_*) principal can't have the deny attached by tg
    # (identity-side), so the banner shows pending-guidance instead of
    # a "Re-apply now" that can't help. Derived from the role_arn (the
    # same AWSReservedSSO_ marker _is_idc uses) — no join needed.
    role_type = "idc" if (
        d.role_arn and "AWSReservedSSO_" in d.role_arn) else "iam"
    return {
        "identity_key": d.identity_key,
        "email":        d.email,
        "role_arn":     d.role_arn,
        "role_type":    role_type,
        "direction":    d.direction,
        "expected":     d.expected,
        "actual":       d.actual,
        "detail":       d.detail,
        "sweep_at":     d.sweep_at.isoformat() if d.sweep_at else None,
    }


@router.get("/governance/drift-count")
def drift_count(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Number of principals drifted in the latest sweep — drives the
    org-admin nav badge. Under the clean-slate model the table holds
    exactly the current sweep's findings, so this is a plain COUNT(*):
    0 when the latest sweep was clean or no sweep has run. org-admin
    scoped."""
    scope.require_org_admin()
    count = db.query(GovernanceDrift).count()
    return {"count": count, "sweep_at": _last_checked_iso(db)}


@router.get("/governance/drift")
def drift_list(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """The current drift set (the latest sweep's findings) so the admin
    can see WHAT drifted and correct it. Under the clean-slate model the
    table IS the current set, so this returns all rows. org-admin
    scoped."""
    scope.require_org_admin()
    rows = (
        db.query(GovernanceDrift)
        .order_by(GovernanceDrift.identity_key)
        .all()
    )
    return {
        "drift": [_row(r) for r in rows],
        "sweep_at": _last_checked_iso(db),
    }


@router.post("/governance/reapply/{identity_key}")
def reapply_governance(
    identity_key: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Re-run the deny enforcement for ONE drifted principal, WITHIN
    the request, so an admin can fix a governed_no_deny row from the
    banner without navigating away or waiting for the ~5-min tick.

    Calls the SHARED deny_reconciler.reconcile_principal (the single
    IAM writer — no new attach logic) and returns its honest post-apply
    state ({state, enforced, ...}). Never auto-sets `governed` (the
    intent is untouched; only the mechanism is re-run). org-admin
    scoped. 404 if the principal isn't known."""
    from fastapi import HTTPException
    from db.models import User
    from worker.jobs import deny_reconciler as dr
    scope.require_org_admin()
    u = (
        db.query(User)
        .filter(User.identity_key == identity_key)
        .first()
    )
    if u is None:
        u = db.query(User).filter(User.email == identity_key).first()
    if u is None:
        raise HTTPException(404, f"principal {identity_key} not found")
    try:
        apply = dr.reconcile_principal(db, u)
    except Exception as e:  # noqa: BLE001 — report, never 500 the banner
        apply = {"state": dr.APPLY_FAILED, "enforced": False,
                 "detail": f"re-apply not confirmed ({e})"}
    return {"identity_key": identity_key, "apply": apply}
