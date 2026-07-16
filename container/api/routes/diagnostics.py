"""GET /api/diagnostics — the read-only diagnostics check-engine surface.

org_admin-scoped (matches /api/cur/health): it exposes account id, role
names, stack/job status. Builds a DiagContext from the running
container's env + boto3 clients + DB, runs the phase-1 checks, and
returns the wire object. `?only=`/`?skip=` filter by category or check
id.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from db.session import get_db
from api.auth import get_caller_email, Scope
from diagnostics.model import run_all
from diagnostics.context import DiagContext

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


@router.get("/diagnostics")
def diagnostics(
    only: Optional[str] = None,
    skip: Optional[str] = None,
    scope: Scope = Depends(_scope),
):
    """Run the phase-1 diagnostics checks and return the wire object.
    org_admin-only (a member/non-admin gets 403)."""
    scope.require_org_admin()
    ctx = DiagContext()
    return run_all(ctx, only=only, skip=skip)
