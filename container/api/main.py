"""
tg-admin FastAPI — replaces Lambda + API Gateway.

Routes mirror the existing /api/* surface so the tg-admin
binary and React frontend need no changes.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

# #583: structured JSON logging — configure once, before the app
# (and uvicorn's loggers) emit anything. Env: TG_LOG_FORMAT
# (json|plain), TG_LOG_LEVEL.
from log_config import configure_logging
configure_logging()

# #587: attach the request-context filter so request_id + caller
# flow onto every log line (#583's JsonFormatter emits them).
# Must run after configure_logging() installed the handler.
import logging as _logging
from api.log_context import install_request_context_filter
install_request_context_filter()
_app_log = _logging.getLogger("api")

from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.auth import get_caller_email, get_caller_auth
from api.aws_errors import (
    EXPIRED_CRED_DETAIL,
    is_expired_cred_error,
)
from db.session import engine, get_db
from db.models import AdminRole, Base, Team

from api.routes import (
    users, teams, roles, models,
    quota, settings, jobs, analytics,
    internal, velocity, integrations_github,
    integrations_jira, velocity_jira,
    service_accounts, governance, cur,
)
from api.auth_routes import router as auth_router
from api.auth_gate import AuthGateMiddleware
from api.csrf import CSRFMiddleware
from api.middleware_access import RequestContextMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-create tables on startup (Alembic handles migrations in prod)
    Base.metadata.create_all(bind=engine)
    # Idempotent in-place column adds for existing DBs.
    # create_all() only creates whole tables — it won't add
    # columns to an already-existing one. Add them by hand
    # (Postgres-only: 'ADD COLUMN IF NOT EXISTS' is supported
    # since pg9.6). Each statement is its own transaction so
    # one missing column doesn't block the next.
    with engine.begin() as conn:
        from sqlalchemy import text
        conn.execute(text(
            "ALTER TABLE teams "
            "ADD COLUMN IF NOT EXISTS parent_team_id "
            "VARCHAR REFERENCES teams(team_id)"
        ))
        # #337: monthly USD budget reference cap (display only).
        conn.execute(text(
            "ALTER TABLE teams "
            "ADD COLUMN IF NOT EXISTS budget_usd "
            "DOUBLE PRECISION"
        ))
        # #345: principal-shape columns on users so the admin
        # UI can classify human/service/iam_user/root callers
        # and tell managed from unmanaged. Backfill
        # identity_key=email for existing rows so the new
        # field is populated for legacy data.
        for col_def in (
            "identity_key VARCHAR",
            "principal_arn VARCHAR",
            "principal_type VARCHAR",
        ):
            conn.execute(text(
                f"ALTER TABLE users "
                f"ADD COLUMN IF NOT EXISTS {col_def}"
            ))
        conn.execute(text(
            "UPDATE users SET identity_key = email "
            "WHERE identity_key IS NULL"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ix_users_identity_key "
            "ON users (identity_key)"
        ))
        # #625: deny-only governance foundation. role_type
        # (idc|iam) + governed flag + admin-set display_name on
        # users. principal_models (per-principal observed-model
        # edges) lands via Base.metadata.create_all above. All
        # back-compat: existing rows get governed=false and null
        # role_type/display_name until discovery / an admin
        # repopulates them.
        for col_def in (
            "role_type VARCHAR",
            "governed BOOLEAN NOT NULL DEFAULT FALSE",
            "display_name VARCHAR",
        ):
            conn.execute(text(
                f"ALTER TABLE users "
                f"ADD COLUMN IF NOT EXISTS {col_def}"
            ))
        # Notify-state for spend-cap alert de-dup. last_warn_sent_at
        # latches the warn email; last_status_notified records the
        # status we last emailed about so block/unblock fires once.
        for col_def in (
            "last_warn_sent_at TIMESTAMPTZ",
            "last_status_notified VARCHAR",
        ):
            conn.execute(text(
                f"ALTER TABLE users "
                f"ADD COLUMN IF NOT EXISTS {col_def}"
            ))
        # #354: auto-pricing pipeline. Extend model_pricing
        # with status/source/proposed_at/confirmed_at/
        # previous_rates_json. Pre-existing rows are treated
        # as confirmed manual entries so spend math keeps
        # working through the upgrade. discovered_models +
        # model_pricing_audit are created via Base.create_all
        # above (the new tables show up automatically).
        for col_def in (
            "status VARCHAR NOT NULL DEFAULT 'confirmed'",
            "source VARCHAR",
            "proposed_at TIMESTAMPTZ",
            "confirmed_at TIMESTAMPTZ",
            "previous_rates_json TEXT",
        ):
            conn.execute(text(
                f"ALTER TABLE model_pricing "
                f"ADD COLUMN IF NOT EXISTS {col_def}"
            ))
        conn.execute(text(
            "UPDATE model_pricing "
            "SET source = 'manual', "
            "    confirmed_at = COALESCE(confirmed_at, now()) "
            "WHERE source IS NULL"
        ))
        # #104: collapse parent_team_admin → team_admin. Scope
        # derives descent from parent_team_id, so the two roles
        # were redundant. Idempotent: zero rows on a clean DB.
        conn.execute(text(
            "UPDATE admin_roles SET role='team_admin' "
            "WHERE role='parent_team_admin'"
        ))
        # #111: structured fields on job_runs so the UI can
        # render the "Enforcement history" table with
        # blocked/unblocked deltas + triggered_by per row.
        for col_def in (
            "triggered_by VARCHAR",
            "blocked TEXT",
            "unblocked TEXT",
            "error TEXT",
        ):
            conn.execute(text(
                f"ALTER TABLE job_runs "
                f"ADD COLUMN IF NOT EXISTS {col_def}"
            ))
        # #364: jira_sites/jira_issues/pr_jira_refs land via
        # Base.metadata.create_all on first start. The indexes
        # below are not part of UniqueConstraint declarations
        # (multiple non-unique single-column lookups) so add
        # them idempotently here.
        for idx_def in (
            "ix_pr_jira_refs_issue_key "
            "ON pr_jira_refs(issue_key)",
            "ix_pr_jira_refs_repo_pr "
            "ON pr_jira_refs(repo, pr_number)",
        ):
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS {idx_def}"
            ))
        # #1042: host-first repo identity. Add host/path to
        # github_repos so the model can grow beyond github.com
        # (self-hosted GitLab, nested subgroups) without a second
        # migration. Backfill existing rows host='github.com',
        # path=repo; `repo` (the PK + github_activity join key) is left
        # unchanged so old keys + their activity rows stay intact.
        for col_def in (
            "host VARCHAR",
            "path VARCHAR",
        ):
            conn.execute(text(
                f"ALTER TABLE github_repos "
                f"ADD COLUMN IF NOT EXISTS {col_def}"
            ))
        conn.execute(text(
            "UPDATE github_repos SET host = 'github.com' "
            "WHERE host IS NULL"
        ))
        conn.execute(text(
            "UPDATE github_repos SET path = repo WHERE path IS NULL"
        ))
        # #1043: per-repo token tiers + public auto-detect. Add the
        # mode/override/probe columns. Backfill EVERY existing row to
        # token_kind='unprobed' + is_public=NULL so the first
        # post-deploy run probes each repo BEFORE handing it the
        # org-default token — the cross-tenant fail-safe (a private row
        # must never fall through to the org token unprobed). token_mode
        # defaults to 'auto'. The legacy 'default' token_kind value
        # (never written by code) is migrated to 'unprobed' too.
        for col_def in (
            "token_mode VARCHAR NOT NULL DEFAULT 'auto'",
            "pat_secret_arn VARCHAR",
            "pat_plain VARCHAR",
            "is_public BOOLEAN",
            "last_probed_at TIMESTAMPTZ",
        ):
            conn.execute(text(
                f"ALTER TABLE github_repos "
                f"ADD COLUMN IF NOT EXISTS {col_def}"
            ))
        conn.execute(text(
            "UPDATE github_repos SET token_kind = 'unprobed' "
            "WHERE token_kind IS NULL OR token_kind = 'default'"
        ))
        # #725 (#720 slice 3): drop the retired quota_metrics table.
        # CUR (cur_user_spend, #724) is the sole spend source and
        # metrics_aggregator — its only writer — is deleted in this
        # slice. The #643 re-grain migrations that lived here are
        # gone with the table. No backfill (owner decision: CUR
        # repopulates spend from billed data). Idempotent.
        conn.execute(text("DROP TABLE IF EXISTS quota_metrics"))
        # #750: Disable→Force block rename + Temporary-unblock removal.
        # Rename disabled_at→force_blocked_at (add new col, copy, the
        # old col is left to be dropped below); migrate the old
        # `disabled` status value → `force_blocked`; drop the
        # time-boxed unblock_expires_at reprieve column. All
        # idempotent (IF EXISTS / IF NOT EXISTS), so a clean DB where
        # create_all already made force_blocked_at is a no-op.
        conn.execute(text(
            "ALTER TABLE users "
            "ADD COLUMN IF NOT EXISTS force_blocked_at TIMESTAMPTZ"
        ))
        # Copy any legacy disabled_at into the new column when the old
        # column still exists, in a DO block so referencing a dropped
        # column doesn't fail to parse on a clean DB.
        conn.execute(text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='users' AND column_name='disabled_at') "
            "THEN UPDATE users SET force_blocked_at = disabled_at "
            "WHERE force_blocked_at IS NULL "
            "AND disabled_at IS NOT NULL; END IF; END $$;"
        ))
        conn.execute(text(
            "UPDATE users SET status = 'force_blocked' "
            "WHERE status = 'disabled'"
        ))
        conn.execute(text(
            "ALTER TABLE users DROP COLUMN IF EXISTS disabled_at"))
        conn.execute(text(
            "ALTER TABLE users "
            "DROP COLUMN IF EXISTS unblock_expires_at"))
    # Bootstrap: seed BOOTSTRAP_ADMIN_EMAIL as org_admin if
    # the admin_roles table is empty. Idempotent — only seeds
    # when no admin exists. Skipped if env var unset.
    bootstrap_email = os.environ.get(
        "BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    if bootstrap_email:
        from db.session import get_db
        from db.models import AdminRole
        with get_db() as db:
            existing = db.query(AdminRole).count()
            if existing == 0:
                db.add(AdminRole(
                    email=bootstrap_email,
                    role="org_admin",
                    granted_by="bootstrap",
                ))
    # #926: seed tg_owns_directory once (insert-if-absent — DB is the
    # source of truth thereafter). For an EXISTING install that ran on
    # TG_AUTH_PROVIDER, seed from it (okta → false, else → true) so a
    # live federated deployment doesn't silently flip to Cognito. A
    # fresh install has no env var → defaults to true (Cognito-only).
    from db.session import get_db as _gdb_dir
    from db.org_config import seed_tg_owns_directory
    with _gdb_dir() as db:
        seed_tg_owns_directory(
            db, os.environ.get("TG_AUTH_PROVIDER"))
    # Seed the runtime SAML config + SSO button label from the
    # build-time Okta env (insert-if-absent), so an existing env-
    # configured federated install sees its setup pre-populated and
    # editable in Settings — runtime config is authoritative thereafter.
    from db.auth_config import seed_saml_config_from_env
    with _gdb_dir() as db:
        seed_saml_config_from_env(db)
    # Idempotent seed: insert Opus 4.8 model_pricing if absent
    # so spend attribution works out-of-the-box for the
    # newest Bedrock release. Insert-if-absent only — never
    # overwrites an admin-edited price on restart.
    from db.session import get_db as _gdb
    from db.models import ModelPricing
    OPUS_48 = "us.anthropic.claude-opus-4-8"
    with _gdb() as db:
        exists = db.query(ModelPricing).filter(
            ModelPricing.model_id == OPUS_48).first()
        if not exists:
            db.add(ModelPricing(
                model_id=OPUS_48,
                input_per_1m=5.00,
                output_per_1m=25.00,
                cache_write_per_1m=6.25,
                cache_read_per_1m=0.50,
                updated_by="system",
            ))
    yield


app = FastAPI(title="tg-admin API", lifespan=lifespan)

# CORS: explicit allowlist when credentials=True. A wildcard
# origin with credentials lets any site issue authenticated
# cross-origin reads. Set TG_CORS_ORIGINS to a comma-separated
# list (e.g. "https://admin.example.com,http://localhost:5173")
# for the SPA dev/prod surfaces. Empty (default) disables CORS
# entirely — fine for the cloud surface where the SPA is served
# same-origin from container/static/.
_cors_env = os.environ.get("TG_CORS_ORIGINS", "").strip()
_cors_origins = [
    o.strip() for o in _cors_env.split(",") if o.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# CSRF must run after CORS (so OPTIONS preflights are
# answered) but before any auth dependency executes.
app.add_middleware(CSRFMiddleware)

# Auth gate runs outermost (Starlette processes middleware
# in reverse add-order): unauthenticated SPA / docs requests
# 302 to /auth/login before any other layer sees them. Gated
# on TG_AUTH_REQUIRE_LOGIN=1 so local dev / tests stay open.
app.add_middleware(AuthGateMiddleware)

# #587: request-context middleware is added LAST → it's the
# OUTERMOST layer, so it sets request_id before anything else
# logs and observes the FINAL status (after auth/csrf/cors). It
# emits the one http_access line per request and returns
# X-Request-Id.
app.add_middleware(RequestContextMiddleware)


# ── Centralized boto3 ClientError → HTTP translation ───
# Any route that doesn't catch ClientError itself ends up
# here. Expired-cred shapes return 503 + a structured body
# so the UI can show a clean "credentials" error instead of
# a generic 500. After #116 the container auto-refreshes
# via the native cred chain, so this almost always means
# the host's cred source itself is broken. All other
# ClientError codes return 502 with the AWS error code
# preserved — those almost always indicate an IAM-policy
# gap, not a host bug.
@app.exception_handler(ClientError)
async def _handle_boto_client_error(
    request: Request, exc: ClientError
):
    if is_expired_cred_error(exc):
        return JSONResponse(
            status_code=503,
            content={
                "detail": EXPIRED_CRED_DETAIL,
                "code":   "creds_expired",
            },
        )
    err = exc.response.get("Error", {}) \
        if hasattr(exc, "response") else {}
    return JSONResponse(
        status_code=502,
        content={
            "detail": err.get("Message", str(exc)),
            "code":   err.get("Code", "aws_error"),
        },
    )


# #587: catch-all for any UNHANDLED exception. Logs ONCE with the
# stacktrace + request context (request_id/method/path/caller via
# the filter's contextvars), and returns a clean JSON 500 — the
# request_id is the support-loop closer (a user quotes it, we grep
# one id). NEVER leak the stacktrace or exception text in the body.
# (HTTPException + ClientError have their own handlers and don't
# reach here; FastAPI's RequestValidationError also has its own.)
@app.exception_handler(Exception)
async def _handle_unhandled(request: Request, exc: Exception):
    from api.log_context import request_id_var
    # Prefer request.state (set by RequestContextMiddleware on this
    # exact request) — the contextvar may read its default here
    # since this handler runs on ServerErrorMiddleware, outside the
    # request-context middleware. Fall back to the contextvar.
    rid = getattr(request.state, "request_id", None) \
        or request_id_var.get()
    _app_log.exception(
        "unhandled",
        extra={
            "event": "unhandled_exception",
            "method": request.method,
            "path": request.url.path,
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "internal error",
            "code": "internal_error",
            "request_id": rid,
        },
        headers={"X-Request-Id": rid},
    )


app.include_router(users.router,        prefix="/api")
app.include_router(teams.router,        prefix="/api")
app.include_router(roles.router,        prefix="/api")
app.include_router(models.router,       prefix="/api")
app.include_router(quota.router,        prefix="/api")
app.include_router(settings.router,     prefix="/api")
app.include_router(jobs.router,         prefix="/api")
app.include_router(analytics.router,    prefix="/api")
app.include_router(velocity.router,     prefix="/api")
app.include_router(integrations_github.router, prefix="/api")
app.include_router(integrations_jira.router,   prefix="/api")
app.include_router(velocity_jira.router,       prefix="/api")
app.include_router(service_accounts.router,    prefix="/api")
app.include_router(governance.router,          prefix="/api")
app.include_router(cur.router,                 prefix="/api")
app.include_router(internal.router)
# /auth/* (login/callback/logout) + /api/csrf — registered
# unconditionally; routes 501 if OIDC env isn't configured.
app.include_router(auth_router)


# #791: warn ONCE if a non-dev environment serves the unstamped
# "dev" version. The version-keyed UAT dedup/trigger (#753) keys
# runs on this string; a literal "dev" on stage/prod collides
# across deploys and can't map back to a commit. The stamp itself
# is set at deploy time via the TG_VERSION task-def env (#791), so a
# "dev" here on stage/prod means that wiring regressed — surface it
# loudly rather than letting the UAT contract silently break.
_version_warned = False


@app.get("/api/version")
def version():
    global _version_warned
    v = os.environ.get("TG_VERSION", "dev")
    env = os.environ.get("TG_ENVIRONMENT", "prod")
    unstamped = v == "dev" and env not in ("dev", "test", "local")
    if unstamped and not _version_warned:
        _version_warned = True
        _app_log.warning(
            "api.version_unstamped",
            extra={"event": "version_unstamped",
                   "tg_environment": env},
        )
    out = {"version": v}
    # Flag the regression in the payload too, so the UAT harness can
    # discriminate "unstamped" from a real version without scraping
    # logs.
    if unstamped:
        out["unstamped"] = True
    return out


@app.get("/api/whoami")
def whoami(auth: tuple[str, str] = Depends(get_caller_auth)):
    email, auth_method = auth
    # Resolve persona from admin_roles. team_ids is the
    # full transitive scope (team_admin → all descendants
    # via parent_team_id), matching what the API actually
    # enforces — so the UI can pre-filter accurately and
    # tests can assert the contract.
    from api.auth import Scope
    persona = "member"
    org_admin = False
    team_ids: list[str] = []
    available: list[dict] = []
    with get_db() as db:
        rows = (
            db.query(AdminRole)
            .filter(AdminRole.email == email)
            .all()
        )
        if any(r.role == "org_admin" for r in rows):
            persona = "org_admin"
            org_admin = True
        elif any(r.role == "team_admin" for r in rows):
            persona = "team_admin"
        if org_admin:
            teams = (
                db.query(Team)
                .order_by(Team.team_id)
                .all()
            )
        else:
            team_ids = list(Scope(email, db).admin_team_ids)
            if team_ids:
                teams = (
                    db.query(Team)
                    .filter(Team.team_id.in_(team_ids))
                    .order_by(Team.team_id)
                    .all()
                )
            else:
                teams = []
        available = [
            {"team_id": t.team_id, "name": t.name or t.team_id}
            for t in teams
        ]
        # #1056: the V&C experimental flag is a UI VISIBILITY gate, so
        # every role needs to read it (the admin-only /admin/config
        # 403s for members). Surface it on whoami, which every role
        # loads at login, so the nav can hide the item for all roles.
        from db.vc_feature import is_vc_enabled
        vc_enabled = is_vc_enabled(db)
    return {
        "email":          email,
        "persona":        persona,
        "org_admin":      org_admin,
        "team_ids":       team_ids,
        "available_teams": available,
        "auth_method":    auth_method,
        "vc_enabled":     vc_enabled,
    }


@app.get("/api/dev/personas")
def dev_personas():
    """Dev-only: list admin emails so the UI can offer a
    'View as' dropdown that flips X-Tg-Test-Email. Hidden
    unless TG_AUTH_TEST_TRUST=1 — production deployments
    must keep this off."""
    if os.environ.get("TG_AUTH_TEST_TRUST") != "1":
        return JSONResponse(
            {"detail": "dev personas disabled"}, status_code=404)
    with get_db() as db:
        rows = (
            db.query(AdminRole.email, AdminRole.role, AdminRole.team_id)
            .order_by(AdminRole.role, AdminRole.email)
            .all()
        )
    return {
        "personas": [
            {
                "email":   r.email,
                "role":    r.role,
                "team_id": r.team_id,
            }
            for r in rows
        ]
    }


@app.get("/api/config/features")
def config_features():
    # Feature flags surfaced to the React UI on load. Kept
    # as a stable shape so the SPA can probe what's enabled
    # in this build without a separate endpoint per flag.
    # enable_velocity_cost was retired in #276 — V&C is
    # always-on.
    return {
        "cur_athena": False,
    }


# ── React UI static serving ──────────────────────────
# The admin-ui/web vite build is copied into /app/static
# by tg-local-install.sh (and into the image during ECS
# build). When present, mount it at the root so /api/*
# routes resolve first and any other path falls back to
# index.html (SPA-style).
_STATIC_DIR = Path(__file__).resolve().parent.parent \
    / "static"
if _STATIC_DIR.is_dir() and (
    _STATIC_DIR / "index.html").is_file():
    # /assets, /vite.svg, /favicon, etc.
    app.mount(
        "/assets",
        StaticFiles(directory=_STATIC_DIR / "assets"),
        name="assets",
    )

    @app.get("/")
    def _index():
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/{full_path:path}")
    def _spa_fallback(full_path: str):
        # Don't intercept /api or /docs; FastAPI's router
        # handles /api/* before this matcher anyway, but
        # be explicit so /docs and /openapi.json work.
        if full_path.startswith(("api/", "auth/", "docs",
                                  "openapi", "internal/",
                                  "redoc")):
            from fastapi import HTTPException
            raise HTTPException(404)
        # Prefer a real file (e.g. /vite.svg)
        target = _STATIC_DIR / full_path
        if target.is_file():
            return FileResponse(target)
        # SPA fallback
        return FileResponse(_STATIC_DIR / "index.html")
