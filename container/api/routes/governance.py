from __future__ import annotations
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import GovernanceDrift
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


def _latest_sweep_at(db: Session):
    """The most recent sweep's timestamp, or None if no sweep has
    run yet. The latest sweep IS the current drift set (#649)."""
    return db.query(func.max(GovernanceDrift.sweep_at)).scalar()


def _row(d: GovernanceDrift) -> dict:
    return {
        "identity_key": d.identity_key,
        "email":        d.email,
        "role_arn":     d.role_arn,
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
    """#649: number of principals drifted in the latest sweep —
    drives the org-admin nav badge (same pattern as #624's
    pricing pending-count). 0 when the latest sweep was clean or
    no sweep has run. org-admin scoped."""
    scope.require_org_admin()
    latest = _latest_sweep_at(db)
    if latest is None:
        return {"count": 0, "sweep_at": None}
    count = (
        db.query(GovernanceDrift)
        .filter(GovernanceDrift.sweep_at == latest)
        .count()
    )
    return {"count": count, "sweep_at": latest.isoformat()}


@router.get("/governance/drift")
def drift_list(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """#649: the current drift set (latest sweep's findings) so the
    admin can see WHAT drifted and correct it. org-admin scoped."""
    scope.require_org_admin()
    latest = _latest_sweep_at(db)
    if latest is None:
        return {"drift": [], "sweep_at": None}
    rows = (
        db.query(GovernanceDrift)
        .filter(GovernanceDrift.sweep_at == latest)
        .order_by(GovernanceDrift.identity_key)
        .all()
    )
    return {
        "drift": [_row(r) for r in rows],
        "sweep_at": latest.isoformat(),
    }
