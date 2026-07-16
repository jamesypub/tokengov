from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


def _now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    email           = Column(String, primary_key=True)
    # #750: active | blocked (auto over-cap) | force_blocked
    # (manual admin override — denies regardless of spend). The
    # old `disabled` value + the time-boxed `unblock_expires_at`
    # reprieve were removed: Unblock now just clears force_blocked
    # and lets the reconciler re-decide against the cap.
    status          = Column(String, nullable=False, default="active")  # active|blocked|force_blocked
    cap_usd         = Column(Float, nullable=True)
    team_id         = Column(String, ForeignKey("teams.team_id"), nullable=True)
    first_seen_at   = Column(DateTime(timezone=True), nullable=True)
    last_seen_at    = Column(DateTime(timezone=True), nullable=True)
    force_blocked_at = Column(DateTime(timezone=True), nullable=True)
    # Notify-state for spend-cap alert de-dup: send only on a
    # transition. last_warn_sent_at latches the warn email (cleared
    # when the user drops back below the warn threshold and on month
    # rollover); last_status_notified is the status we last emailed
    # about, so a block/unblock email fires once per change.
    last_warn_sent_at = Column(DateTime(timezone=True), nullable=True)
    last_status_notified = Column(String, nullable=True)
    version         = Column(Integer, nullable=False, default=1)
    created_at      = Column(DateTime(timezone=True), default=_now)
    updated_at      = Column(DateTime(timezone=True), default=_now, onupdate=_now)
    # #345: principal-shape capture so admins can see machine
    # callers and unmanaged human callers. identity_key is the
    # synthetic primary identity (email for humans, role:<R>
    # for service rows). principal_arn + principal_type let the
    # UI classify each row. v1.0 keeps `email` as PK; v1.1
    # promotes identity_key.
    identity_key    = Column(String, nullable=True, unique=True)
    principal_arn   = Column(String, nullable=True)
    principal_type  = Column(String, nullable=True)
    # #625 (deny-only governance foundation):
    #   role_type    — IDC permission-set role
    #                  (name AWSReservedSSO_*, path
    #                  /aws-reserved/sso.amazonaws.com/) → "idc";
    #                  every other principal → "iam". Lets the UI
    #                  disable Manage on IDC rows (a direct deny
    #                  is wiped on IDC re-provision; #618).
    #   governed     — persisted deny-only governance flag: True
    #                  once tg-BedrockQuotaDeny has been
    #                  explicitly attached to this principal's
    #                  role (the attach itself lands in child C).
    #                  Discovery never sets it; default False.
    #                  NAMED `governed`, not `managed`, on purpose:
    #                  the API already ships a v1.0 `managed`
    #                  heuristic key (reaches Bedrock via the
    #                  tg-consumer chokepoint, #345) that means
    #                  something different. Keeping the names
    #                  distinct avoids breaking that contract.
    #   display_name — admin-set friendly label, distinct from
    #                  the ARN-derived caller. Never a key; the
    #                  caller stays read-only. Edit UI is child E.
    role_type       = Column(String, nullable=True)
    governed        = Column(Boolean, nullable=False, default=False)
    display_name    = Column(String, nullable=True)
    # Admin-maintained email↔Bedrock-key mapping for CUR attribution
    # of bedrock-mantle / Codex (bearer-key) traffic. A long-term
    # Bedrock API key is backed by an IAM user (e.g.
    # `MantleApiKey-uhbhn79a`); CUR records THAT IAM-user name in
    # line_item_iam_principal, not a person, so key spend can't
    # otherwise correlate to a developer. An org-admin records the
    # key's IAM-user NAME here (the non-secret public identifier —
    # tg never sees/stores/mints the key secret), and both spend
    # resolution layers re-attribute a matching `user/<name>`
    # principal to this user. unique: a key belongs to one person.
    # nullable: most (SSO-role) users never need it.
    bedrock_key_user = Column(String, nullable=True, unique=True)

    team            = relationship("Team", back_populates="users")
    memberships     = relationship("TeamMembership", back_populates="user")


class Team(Base):
    __tablename__ = "teams"

    team_id     = Column(String, primary_key=True)
    name        = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    parent_team_id = Column(
        String,
        ForeignKey("teams.team_id"),
        nullable=True,
    )
    created_by  = Column(String, nullable=True)
    created_at  = Column(DateTime(timezone=True), default=_now)
    updated_at  = Column(DateTime(timezone=True), default=_now, onupdate=_now)
    # Reference cap (#337). Null = unlimited. Display only —
    # never enforces; per-user cap_usd is the only enforcement.
    budget_usd  = Column(Float, nullable=True)

    users       = relationship("User", back_populates="team")
    memberships = relationship("TeamMembership", back_populates="team")
    parent      = relationship(
        "Team",
        remote_side="Team.team_id",
        backref="children",
    )


class TeamMembership(Base):
    __tablename__ = "team_memberships"
    __table_args__ = (UniqueConstraint("email", "team_id"),)

    id          = Column(Integer, primary_key=True, autoincrement=True)
    email       = Column(String, ForeignKey("users.email"), nullable=False)
    team_id     = Column(String, ForeignKey("teams.team_id"), nullable=False)
    added_by    = Column(String, nullable=True)
    added_at    = Column(DateTime(timezone=True), default=_now)

    user        = relationship("User", back_populates="memberships")
    team        = relationship("Team", back_populates="memberships")


class AdminRole(Base):
    __tablename__ = "admin_roles"
    __table_args__ = (UniqueConstraint("email", "role", "team_id"),)

    id          = Column(Integer, primary_key=True, autoincrement=True)
    email       = Column(String, nullable=False, index=True)
    role        = Column(String, nullable=False)  # org_admin|team_admin
    team_id     = Column(String, nullable=True)
    granted_by  = Column(String, nullable=True)
    granted_at  = Column(DateTime(timezone=True), default=_now)


class AdminConfig(Base):
    """Key-value store for feature flags and org settings."""
    __tablename__ = "admin_config"

    key         = Column(String, primary_key=True)
    value       = Column(Text, nullable=True)
    updated_at  = Column(DateTime(timezone=True), default=_now, onupdate=_now)


class ModelPricing(Base):
    __tablename__ = "model_pricing"

    model_id            = Column(String, primary_key=True)
    input_per_1m        = Column(Float, nullable=False)
    output_per_1m       = Column(Float, nullable=False)
    cache_write_per_1m  = Column(Float, nullable=False)
    cache_read_per_1m   = Column(Float, nullable=False)
    updated_by          = Column(String, nullable=True)
    updated_at          = Column(DateTime(timezone=True), default=_now, onupdate=_now)
    # #354: auto-pricing pipeline. status=confirmed rates
    # are trusted by spend math; status=proposed rows wait
    # for admin review and contribute 0 to spend until
    # confirmed. source records where the rate came from
    # (manual | aws_pricing_api | heuristic | imported).
    status              = Column(
        String, nullable=False, default="confirmed"
    )
    source              = Column(String, nullable=True)
    proposed_at         = Column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_at        = Column(
        DateTime(timezone=True), nullable=True
    )
    previous_rates_json = Column(Text, nullable=True)


class DiscoveredModel(Base):
    """#354: every model_id observed in CW Logs gets a row
    here so the proposer job has a worklist. processed_at
    is the watermark — null means proposer hasn't fetched
    yet; older than 7d is the re-fetch cadence."""
    __tablename__ = "discovered_models"

    model_id          = Column(String, primary_key=True)
    first_seen_at     = Column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    last_seen_at      = Column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    invocations_count = Column(Integer, nullable=False, default=0)
    processed_at      = Column(
        DateTime(timezone=True), nullable=True
    )


class PrincipalModel(Base):
    """#625: per-principal observed-model edges. One row per
    (identity_key, model_id) seen in the Bedrock invocation log.
    Adjacent to `discovered_models` (which is org-wide) but
    scoped to a single calling principal so the Users screen can
    list "models this principal has invoked". invocations_count
    is a running tally bumped on each discovery tick; the deny
    model-allowlist (child B) reads neither this nor sets it —
    this is read/observe only."""
    __tablename__ = "principal_models"
    __table_args__ = (
        UniqueConstraint("identity_key", "model_id"),
    )

    id                = Column(
        Integer, primary_key=True, autoincrement=True
    )
    identity_key      = Column(String, nullable=False, index=True)
    model_id          = Column(String, nullable=False)
    first_seen_at     = Column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    last_seen_at      = Column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    invocations_count = Column(Integer, nullable=False, default=0)


class ModelPricingAudit(Base):
    """#354: append-only audit feed for every propose /
    confirm / repropose action against model_pricing.
    Surfaced via GET /api/pricing/audit; consumed by
    #350b's drawer."""
    __tablename__ = "model_pricing_audit"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    model_id    = Column(String, nullable=False, index=True)
    action      = Column(String, nullable=False)
    prev_rates  = Column(Text, nullable=True)
    new_rates   = Column(Text, nullable=True)
    actor       = Column(String, nullable=False)
    at          = Column(
        DateTime(timezone=True), nullable=False, default=_now
    )


# #725 (#720 slice 3): the QuotaMetric model + quota_metrics table
# are retired. CUR (CurUserSpend, #724) is the sole spend source;
# metrics_aggregator — the only writer — is deleted in this slice,
# so the table is dropped in the lifespan migration (api/main.py).


class CurUserSpend(Base):
    """#724 (#720 slice 2): per-(principal, usage_hour, region,
    model) spend + token counters sourced from AWS-billed CUR 2.0
    (the `line_item_iam_principal` column, #714) — the new spend
    source that supersedes quota_metrics. CUR is authoritative for
    billed spend; caps become billed-spend, ≤24h lagged.

    `email` is the principal key (real email | role:<R> |
    iam_user | idc | …) via classify_principal() — same semantics
    as quota_metrics.email, so every existing SUM-by-email reader
    repoints with no shape change. `identity_arn` keeps the raw
    line_item_iam_principal for audit.

    Grain is per-HOUR (line_item_usage_start_date bucket), one
    finer than quota_metrics' per-day — every reader already SUMs
    over a time range, so day→hour is transparent. `region`
    (product_region_code) is a display/aggregation column ONLY —
    NEVER a deny key (owner rule: the cap is per-principal-global,
    summed across regions).

    Re-sync REPLACES the current billing month (CUR overwrites the
    month partition; a month can revise DOWN) — writers
    delete-and-reinsert or upsert on the unique key, never `+=`.

    Option C (#720b, tg-lead 2026-06-07): this table is ADDED and
    readers repoint here; quota_metrics + its writer stay in place
    (dead-but-harmless) until #725 (720c) drops them atomically
    with the metrics_aggregator deletion."""
    __tablename__ = "cur_user_spend"
    __table_args__ = (
        UniqueConstraint(
            "email", "usage_hour", "region", "model_id"),
    )

    id              = Column(
        Integer, primary_key=True, autoincrement=True)
    email           = Column(String, nullable=False, index=True)
    identity_arn    = Column(String, nullable=True)
    usage_hour      = Column(
        DateTime(timezone=True), nullable=False, index=True)
    region          = Column(String, nullable=True)
    model_id        = Column(String, nullable=False)
    input_tokens    = Column(Integer, nullable=False, default=0)
    output_tokens   = Column(Integer, nullable=False, default=0)
    cache_write_tokens = Column(Integer, nullable=False, default=0)
    cache_read_tokens  = Column(Integer, nullable=False, default=0)
    total_tokens    = Column(Integer, nullable=False, default=0)
    spend_usd       = Column(Float, nullable=False, default=0.0)
    billing_period  = Column(String, nullable=True)   # YYYY-MM
    data_source     = Column(String, nullable=False, default="cur")
    updated_at      = Column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class SyncState(Base):
    """#643: high-water-mark for timestamp-based CDC ingestion.
    metrics_aggregator queries the invocation log for events
    strictly after `last_synced_through` and, in the SAME
    transaction as the per-day upserts, advances the watermark to
    the query's end time — so a crash mid-write re-runs the window
    (the upsert is a set/replace of that window's per-day rows, not
    a blind +=) rather than skipping it. One row per logical
    stream, keyed by `name`."""
    __tablename__ = "sync_state"

    name               = Column(String, primary_key=True)
    last_synced_through = Column(
        DateTime(timezone=True), nullable=True
    )
    updated_at         = Column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class QuotaPolicy(Base):
    """Default and per-user monthly cap overrides."""
    __tablename__ = "quota_policies"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    scope           = Column(String, nullable=False)   # DEFAULT or USER#<email>
    monthly_cap_usd = Column(Float, nullable=False)
    updated_by      = Column(String, nullable=True)
    updated_at      = Column(DateTime(timezone=True), default=_now, onupdate=_now)


class JobRun(Base):
    __tablename__ = "job_runs"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    job_name     = Column(String, nullable=False, index=True)
    status       = Column(String, nullable=False)  # running|succeeded|failed
    started_at   = Column(DateTime(timezone=True), default=_now)
    finished_at  = Column(DateTime(timezone=True), nullable=True)
    detail       = Column(Text, nullable=True)
    triggered_by = Column(String, nullable=True)
    blocked      = Column(Text, nullable=True)   # JSON array of emails
    unblocked    = Column(Text, nullable=True)   # JSON array of emails
    error        = Column(Text, nullable=True)


class AnalyticsCache(Base):
    """Cache for Athena query results."""
    __tablename__ = "analytics_cache"

    query_id        = Column(String, primary_key=True)
    columns         = Column(Text, nullable=True)   # JSON
    rows            = Column(Text, nullable=True)   # JSON
    row_count       = Column(Integer, nullable=True)
    execution_id    = Column(String, nullable=True)
    cached_at       = Column(DateTime(timezone=True), default=_now)


class ServiceAccountCap(Base):
    """#346: per-role budget for machine principals.
    Keyed by `identity_key=role:<R>` matching the User row
    #345 created. mode='alert_only' tracks spend without
    enforcement; 'alert_and_block' writes an inline deny
    on the role at budget exhaustion; 'disabled' records
    intent-not-to-enforce (e.g. service-linked roles)."""
    __tablename__ = "service_account_caps"

    identity_key        = Column(String, primary_key=True)
    budget_usd          = Column(Float, nullable=False)
    period              = Column(String, nullable=False)
    # day | week | month
    mode                = Column(String, nullable=False)
    # alert_only | alert_and_block | disabled
    alert_threshold_pct = Column(
        Integer, nullable=False, default=80
    )
    owner_emails        = Column(Text, nullable=False, default="")
    grace_pct           = Column(Integer, nullable=False, default=0)
    auto_unblock        = Column(
        Boolean, nullable=False, default=True
    )
    blocked_at          = Column(
        DateTime(timezone=True), nullable=True
    )
    created_by          = Column(String, nullable=False)
    created_at          = Column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at          = Column(
        DateTime(timezone=True), nullable=False,
        default=_now, onupdate=_now,
    )


class ServiceAccountAlert(Base):
    """#346: alert ledger. Dedup key per period is
    (identity_key, kind) — the monitor checks this so a
    hot pipeline doesn't fire one email per 5-min tick."""
    __tablename__ = "service_account_alerts"

    id              = Column(
        Integer, primary_key=True, autoincrement=True
    )
    identity_key    = Column(String, nullable=False, index=True)
    fired_at        = Column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    kind            = Column(String, nullable=False)
    # threshold | budget_exhausted | unblocked
    pct_of_budget   = Column(Float, nullable=False)
    period_key      = Column(String, nullable=False, index=True)
    # YYYY-MM-DD for day, YYYY-Wnn for week, YYYY-MM for month
    delivered       = Column(
        Boolean, nullable=False, default=False
    )


class GovernanceDrift(Base):
    """#649: one row per drifted principal found in a sweep of
    `governance_drift_check`. Drift = tg's intent flag
    (`users.governed`) disagrees with IAM truth
    (`iam:ListAttachedRolePolicies` on the principal's role).

    Two directions:
      - `governed_no_deny`  — governed=True but the deny policy is
        NOT attached to the role (self-detach / IDC re-provision /
        manual edit / failed attach) → enforcing nothing.
      - `deny_no_governed`  — deny attached but governed=False, AND
        no OTHER governed principal shares that role (else it's the
        shared-role norm, not drift — see the job's guard).

    Rows are grouped by `sweep_at`; the latest sweep IS the current
    drift set. History is retained so an admin can see when a drift
    first appeared / cleared. Detect+alert only — the job writes
    NO IAM and flips NO flag (owner decision; #649)."""
    __tablename__ = "governance_drift"

    id            = Column(
        Integer, primary_key=True, autoincrement=True
    )
    sweep_at      = Column(
        DateTime(timezone=True), nullable=False,
        default=_now, index=True,
    )
    identity_key  = Column(String, nullable=False, index=True)
    email         = Column(String, nullable=True)
    role_arn      = Column(String, nullable=True)
    direction     = Column(String, nullable=False)
    # governed_no_deny | deny_no_governed
    expected      = Column(String, nullable=False)  # managed|unmanaged
    actual        = Column(String, nullable=False)  # the IAM truth
    detail        = Column(Text, nullable=True)


class GithubActivity(Base):
    """One row per merged GitHub PR. Source for V&C views."""
    __tablename__ = "github_activity"
    __table_args__ = (UniqueConstraint("repo", "pr_number"),)

    id              = Column(Integer, primary_key=True, autoincrement=True)
    repo            = Column(String, nullable=False, index=True)
    pr_number       = Column(Integer, nullable=False)
    title           = Column(Text, nullable=True)
    author_login    = Column(String, nullable=False, index=True)
    author_email    = Column(String, nullable=True, index=True)
    body            = Column(Text, nullable=True)
    labels          = Column(Text, nullable=True)   # JSON array
    issue_refs      = Column(Text, nullable=True)   # JSON array of {number, repo}
    additions       = Column(Integer, nullable=False, default=0)
    deletions       = Column(Integer, nullable=False, default=0)
    merged_at       = Column(DateTime(timezone=True), nullable=False, index=True)
    # PR open time on GitHub (NOT row insertion time). Used by
    # pr_cost_rollup to compute cycle stats: merged_at - created_at.
    created_at      = Column(DateTime(timezone=True), nullable=True)


class PrClassification(Base):
    """Per-PR class verdict from `pr_classify`."""
    __tablename__ = "pr_classifications"
    __table_args__ = (UniqueConstraint("repo", "pr_number"),)

    id              = Column(Integer, primary_key=True, autoincrement=True)
    repo            = Column(String, nullable=False, index=True)
    pr_number       = Column(Integer, nullable=False)
    pr_class        = Column(String, nullable=False)   # story|bug|task
    classified_by   = Column(String, nullable=False)   # issue_link|pr_label|fallback
    probe_trace     = Column(Text, nullable=True)      # JSON
    classified_at   = Column(DateTime(timezone=True), default=_now)


class LinkedAccount(Base):
    """Maps a tg user (email) to an external vendor handle."""
    __tablename__ = "linked_accounts"
    __table_args__ = (UniqueConstraint("vendor", "external_handle"),)

    id              = Column(Integer, primary_key=True, autoincrement=True)
    email           = Column(String, nullable=False, index=True)
    vendor          = Column(String, nullable=False)   # github
    external_handle = Column(String, nullable=False)
    linked_by       = Column(String, nullable=False)   # 'auto' or admin email
    linked_at       = Column(DateTime(timezone=True), default=_now)


class TeamWeeklyMetric(Base):
    """Pre-rolled per-team weekly bins for 30d/90d/YTD V&C views."""
    __tablename__ = "team_weekly_metrics"
    __table_args__ = (
        UniqueConstraint("team_id", "week_start", "pr_class"),
    )

    id                      = Column(Integer, primary_key=True, autoincrement=True)
    team_id                 = Column(String, nullable=False, index=True)
    week_start              = Column(DateTime(timezone=True), nullable=False, index=True)
    pr_class                = Column(String, nullable=False)   # all|story|bug|task
    prs_merged              = Column(Integer, nullable=False, default=0)
    spend_usd               = Column(Float, nullable=False, default=0.0)
    cycle_median_hours      = Column(Float, nullable=True)
    cycle_p90_hours         = Column(Float, nullable=True)
    rolled_up_at            = Column(DateTime(timezone=True), default=_now)


class TeamDailyMetric(Base):
    """Pre-rolled per-team daily bins for the 7d V&C window."""
    __tablename__ = "team_daily_metrics"
    __table_args__ = (
        UniqueConstraint("team_id", "day", "pr_class"),
    )

    id                      = Column(Integer, primary_key=True, autoincrement=True)
    team_id                 = Column(String, nullable=False, index=True)
    day                     = Column(DateTime(timezone=True), nullable=False, index=True)
    pr_class                = Column(String, nullable=False)
    prs_merged              = Column(Integer, nullable=False, default=0)
    spend_usd               = Column(Float, nullable=False, default=0.0)
    cycle_median_hours      = Column(Float, nullable=True)
    cycle_p90_hours         = Column(Float, nullable=True)
    rolled_up_at            = Column(DateTime(timezone=True), default=_now)


class GithubRepo(Base):
    """Repos the org has configured for GitHub sync."""
    __tablename__ = "github_repos"

    repo            = Column(String, primary_key=True)
    # #1042: host-first identity so the model can grow beyond github.com
    # (self-hosted GitLab, nested subgroups) without re-modelling. `repo`
    # stays the PK/display key (canonical host/path for new rows; legacy
    # owner/name preserved on backfill so github_activity joins survive).
    # `path` is the full variable-depth project path under `host`.
    host            = Column(String, nullable=True, default="github.com")
    path            = Column(String, nullable=True)
    team_id         = Column(String, ForeignKey("teams.team_id"), nullable=True)
    # #1043: token_kind is now a DERIVED status the resolver writes each
    # run, not an admin input. Values:
    #   public   — synced anonymously (probe returned public)
    #   override — a per-repo PAT (pat_secret_arn / pat_plain) was used
    #   org      — resolved to the org-default PAT (the common case)
    #   missing  — private, no token resolves → sync_status=paused
    #   unprobed — added but not yet classified (every backfilled row)
    # The admin input is the per-repo MODE (auto|override|public), held
    # in token_mode; token_kind is what the resolver actually did.
    token_kind      = Column(String, nullable=False, default="unprobed")
    # Admin-chosen mode: auto (probe→org/anon), override (use repo PAT),
    # public (force anonymous). Default auto.
    token_mode      = Column(String, nullable=False, default="auto")
    # Per-repo override PAT (the exception to the org default). Secret
    # in SM (pat_secret_arn); pat_plain is the dev/local-compose-only
    # fallback, mirroring JiraSite.api_token_plain. Never serialized.
    pat_secret_arn  = Column(String, nullable=True)
    pat_plain       = Column(String, nullable=True)
    # Cached visibility probe: None = unprobed (forces a probe before any
    # org-default sync — the cross-tenant fail-safe), True = public,
    # False = private. last_probed_at is the probe watermark.
    is_public       = Column(Boolean, nullable=True)
    last_probed_at  = Column(DateTime(timezone=True), nullable=True)
    sync_status     = Column(String, nullable=False, default="ok")       # ok|paused|auth_failed|rate_limited
    last_sync_at    = Column(DateTime(timezone=True), nullable=True)
    added_by        = Column(String, nullable=True)
    added_at        = Column(DateTime(timezone=True), default=_now)


class JiraSite(Base):
    """An Atlassian Jira Cloud site the org has wired up.

    `api_token_secret_arn` points to a Secrets Manager secret
    holding the Basic-auth token; `api_token_plain` is the
    dev-only fallback for local docker-compose without SM.
    `projects` is a JSON array of project keys
    (e.g. ["PROJ", "DATA"]) used to filter issue-key
    extraction so unrelated `K8S-2024`-style strings don't
    pollute pr_jira_refs.
    """
    __tablename__ = "jira_sites"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    site_url             = Column(String, nullable=False, unique=True)
    auth_email           = Column(String, nullable=False)
    api_token_secret_arn = Column(String, nullable=True)
    api_token_plain      = Column(Text, nullable=True)
    projects             = Column(Text, nullable=False, default="[]")
    sync_status          = Column(String, nullable=False, default="ok")
    last_sync_at         = Column(DateTime(timezone=True), nullable=True)
    added_by             = Column(String, nullable=False)
    added_at             = Column(DateTime(timezone=True), default=_now)


class JiraIssue(Base):
    """Mirror of a Jira issue's metadata for V&C joins."""
    __tablename__ = "jira_issues"

    issue_key        = Column(String, primary_key=True)
    issue_type       = Column(String, nullable=False)
    summary          = Column(Text, nullable=True)
    status           = Column(String, nullable=False)
    status_category  = Column(String, nullable=False)
    priority         = Column(String, nullable=True)
    assignee_email   = Column(String, nullable=True, index=True)
    reporter_email   = Column(String, nullable=True)
    parent_epic_key  = Column(String, nullable=True, index=True)
    sprint_id        = Column(Integer, nullable=True, index=True)
    sprint_name      = Column(String, nullable=True)
    story_points     = Column(Float, nullable=True)
    fix_versions     = Column(Text, nullable=True)
    labels           = Column(Text, nullable=True)
    resolved_at      = Column(DateTime(timezone=True), nullable=True)
    jira_created_at  = Column(DateTime(timezone=True), nullable=False)
    jira_updated_at  = Column(DateTime(timezone=True), nullable=False)
    last_synced_at   = Column(DateTime(timezone=True), default=_now)


class PrJiraRef(Base):
    """Edge table linking a github_activity PR to a Jira issue."""
    __tablename__ = "pr_jira_refs"
    __table_args__ = (
        UniqueConstraint("repo", "pr_number", "issue_key"),
    )

    id          = Column(Integer, primary_key=True, autoincrement=True)
    repo        = Column(String, nullable=False)
    pr_number   = Column(Integer, nullable=False)
    issue_key   = Column(String, nullable=False, index=True)
    source      = Column(String, nullable=False)   # title|commit|body|branch
    created_at  = Column(DateTime(timezone=True), default=_now)


class JiraWeeklyMetric(Base):
    """Pre-rolled per-team weekly Jira-aware bins for V&C.

    Composite PK (team_id, week_start, pr_class, sprint_id,
    fix_version) lets the rollup cleanly upsert per slice
    without touching neighboring slices. NULL sprint_id /
    fix_version means "no Jira slicing" — that row is the
    aggregate for PRs without a Jira ref or without that
    field.

    `story_points` sums the SP from linked Jira issues for
    the PRs that fed this bin; `$ / SP` is computed at API
    time as `spend_usd / story_points`.
    """
    __tablename__ = "jira_weekly_metrics"

    team_id            = Column(String, primary_key=True)
    week_start         = Column(DateTime(timezone=True), primary_key=True)
    pr_class           = Column(String, primary_key=True)   # all|story|bug|task
    sprint_id          = Column(Integer, primary_key=True, default=0)
    fix_version        = Column(String, primary_key=True, default="")
    prs_merged         = Column(Integer, nullable=False, default=0)
    spend_usd          = Column(Float, nullable=False, default=0.0)
    story_points       = Column(Float, nullable=True)
    cycle_median_hours = Column(Float, nullable=True)
    rolled_up_at       = Column(DateTime(timezone=True), default=_now)


class WebSession(Base):
    """Browser session backing the Okta OIDC cookie auth (#130).

    `id` is a CSPRNG token sent to the browser as the
    `tg_session` cookie value. We store it in plain text only
    because the cookie is HTTP-only + Secure and the row is
    deleted on logout. Refresh token (Okta) is stored verbatim
    today; #133 will encrypt at rest.
    """
    __tablename__ = "web_sessions"

    id              = Column(String, primary_key=True)
    email           = Column(String, nullable=False, index=True)
    created_at      = Column(DateTime(timezone=True), default=_now)
    expires_at      = Column(DateTime(timezone=True), nullable=False)
    last_seen_at    = Column(DateTime(timezone=True), default=_now)
    refresh_token   = Column(Text, nullable=True)
    user_agent      = Column(Text, nullable=True)
    ip              = Column(String, nullable=True)
