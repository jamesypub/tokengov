"""
#346: per-role budget API for machine principals. CRUD on
service_account_caps + alert ledger + manual unblock. All
routes admin-scoped.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import (
    ServiceAccountAlert, ServiceAccountCap,
)
from api.auth import get_caller_email, Scope

router = APIRouter()

VALID_PERIODS = {"day", "week", "month"}
VALID_MODES = {"alert_only", "alert_and_block", "disabled"}


def _db():
    with get_db() as db:
        yield db


def _scope(
    request: Request,
    email: str = Depends(get_caller_email),
    db: Session = Depends(_db),
) -> Scope:
    return Scope(email, db)


def _row(cap: ServiceAccountCap) -> dict:
    return {
        "identity_key":        cap.identity_key,
        "budget_usd":          float(cap.budget_usd),
        "period":              cap.period,
        "mode":                cap.mode,
        "alert_threshold_pct": cap.alert_threshold_pct,
        "owner_emails":        cap.owner_emails or "",
        "grace_pct":           cap.grace_pct,
        "auto_unblock":        cap.auto_unblock,
        "blocked":             cap.blocked_at is not None,
        "blocked_at":          (
            cap.blocked_at.isoformat() if cap.blocked_at else None
        ),
        "created_by":          cap.created_by,
        "created_at":          (
            cap.created_at.isoformat() if cap.created_at else None
        ),
        "updated_at":          (
            cap.updated_at.isoformat() if cap.updated_at else None
        ),
    }


def _validate(body: dict) -> None:
    if body.get("period") not in VALID_PERIODS:
        raise HTTPException(
            400, f"period must be one of {sorted(VALID_PERIODS)}",
        )
    if body.get("mode") not in VALID_MODES:
        raise HTTPException(
            400, f"mode must be one of {sorted(VALID_MODES)}",
        )
    bu = body.get("budget_usd")
    try:
        bu_f = float(bu)
    except (TypeError, ValueError):
        raise HTTPException(400, "budget_usd must be numeric")
    if bu_f < 0:
        raise HTTPException(400, "budget_usd must be >= 0")
    threshold = body.get("alert_threshold_pct", 80)
    if not (0 <= int(threshold) <= 100):
        raise HTTPException(
            400, "alert_threshold_pct must be 0..100",
        )
    grace = body.get("grace_pct", 0)
    if not (0 <= int(grace) <= 100):
        raise HTTPException(
            400, "grace_pct must be 0..100",
        )
    if body["mode"] != "disabled" and not (
        body.get("owner_emails") or ""
    ).strip():
        raise HTTPException(
            400,
            "owner_emails required when mode != disabled",
        )


# NOTE: identity_key values contain ':' (e.g. `role:Foo`)
# which FastAPI tolerates fine in a path param, but we
# can't combine `{identity_key:path}` with a literal
# trailing segment like `/alerts` because the `path`
# converter is greedy. Use a `?identity_key=` query for
# the sub-resources (alerts, unblock).


@router.get("/service-account-caps")
def list_caps(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    caps = (
        db.query(ServiceAccountCap)
        .order_by(ServiceAccountCap.identity_key)
        .all()
    )
    return {"caps": [_row(c) for c in caps]}


@router.get("/service-account-caps/alerts")
def list_alerts(
    identity_key: str,
    limit: int = 50,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """List alert history for an identity_key. Path-style
    sub-resource on the cap doesn't fly because identity_key
    contains a ':' which combined with `:path` confuses
    FastAPI's router. Query param keeps the route shape
    flat."""
    scope.require_org_admin()
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be 1..500")
    rows = (
        db.query(ServiceAccountAlert)
        .filter(ServiceAccountAlert.identity_key == identity_key)
        .order_by(ServiceAccountAlert.id.desc())
        .limit(limit)
        .all()
    )
    return {"alerts": [
        {
            "id":            a.id,
            "kind":          a.kind,
            "fired_at":      (
                a.fired_at.isoformat() if a.fired_at else None
            ),
            "pct_of_budget": float(a.pct_of_budget),
            "period_key":    a.period_key,
            "delivered":     a.delivered,
        }
        for a in rows
    ]}


@router.post("/service-account-caps/unblock")
def manual_unblock(
    identity_key: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Manually clear the blocked_at flag so the next
    monitor tick will remove the inline deny. The actual
    IAM call happens in the worker so the API stays out of
    the IAM-write blast radius."""
    scope.require_org_admin()
    cap = (
        db.query(ServiceAccountCap)
        .filter(ServiceAccountCap.identity_key == identity_key)
        .first()
    )
    if cap is None:
        raise HTTPException(
            404, f"no cap for {identity_key}",
        )
    if cap.blocked_at is None:
        return _row(cap)
    cap.blocked_at = None
    db.flush()
    return _row(cap)


@router.get("/service-account-caps/{identity_key:path}")
def get_cap(
    identity_key: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    cap = (
        db.query(ServiceAccountCap)
        .filter(ServiceAccountCap.identity_key == identity_key)
        .first()
    )
    if cap is None:
        raise HTTPException(
            404, f"no cap for {identity_key}",
        )
    return _row(cap)


@router.put("/service-account-caps/{identity_key:path}")
def upsert_cap(
    identity_key: str,
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    _validate(body)
    cap = (
        db.query(ServiceAccountCap)
        .filter(ServiceAccountCap.identity_key == identity_key)
        .first()
    )
    if cap is None:
        cap = ServiceAccountCap(
            identity_key=identity_key,
            budget_usd=float(body["budget_usd"]),
            period=body["period"],
            mode=body["mode"],
            alert_threshold_pct=int(
                body.get("alert_threshold_pct", 80)
            ),
            owner_emails=body.get("owner_emails", ""),
            grace_pct=int(body.get("grace_pct", 0)),
            auto_unblock=bool(body.get("auto_unblock", True)),
            created_by=scope.email,
        )
        db.add(cap)
    else:
        cap.budget_usd = float(body["budget_usd"])
        cap.period = body["period"]
        cap.mode = body["mode"]
        cap.alert_threshold_pct = int(
            body.get("alert_threshold_pct", 80)
        )
        cap.owner_emails = body.get("owner_emails", "")
        cap.grace_pct = int(body.get("grace_pct", 0))
        cap.auto_unblock = bool(
            body.get("auto_unblock", True)
        )
    db.flush()
    return _row(cap)


@router.delete("/service-account-caps/{identity_key:path}")
def delete_cap(
    identity_key: str,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    cap = (
        db.query(ServiceAccountCap)
        .filter(ServiceAccountCap.identity_key == identity_key)
        .first()
    )
    if cap is None:
        raise HTTPException(
            404, f"no cap for {identity_key}",
        )
    db.delete(cap)
    db.flush()
    return {"deleted": identity_key}
