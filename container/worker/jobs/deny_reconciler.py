"""
deny_reconciler — builds + applies tg-BedrockQuotaDeny IAM policy.
Ported from lambda/deny_reconciler/index.py — same logic, reads from Postgres.
"""
from __future__ import annotations
import json
import logging
import os
import re
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import func

from db.session import get_db
from db.models import User, CurUserSpend, AdminRole, Team
from db.usage_windows import month_start_utc
from db.org_config import (
    get_org_default_quota_usd, get_blocked_models,
    get_spend_estimate_strategy, get_spend_estimate_enforcement,
    get_spend_alert_warn_pct, get_spend_alert_exceeded,
)
from db.spend_estimate import (
    project_for_principal, ENFORCE_ENFORCE,
)
# #1011: shared IDC classifier — keep one definition of "is this an
# AWSReservedSSO_* permission-set principal" across the reconciler,
# governance.verify(), and the drift checker.
from governance import _is_idc
from worker import notify

log = logging.getLogger("worker.deny_reconciler")

REGION      = os.environ.get("AWS_REGION", "us-east-1")
POLICY_NAME = os.environ.get("DENY_POLICY_NAME", "tg-BedrockQuotaDeny")
ACCOUNT_ID  = os.environ.get("AWS_ACCOUNT_ID", "")
# #349: role rename. Prefer TG_TOKEN_CONSUMER_ROLE_NAME; fall
# back to BEDROCK_ROLE_NAME (deprecated, drop in v1.1) so a
# rolling upgrade keeps the worker pointed at the right role.
ROLE_NAME = (
    os.environ.get("TG_TOKEN_CONSUMER_ROLE_NAME")
    or os.environ.get("BEDROCK_ROLE_NAME")
    or "tg-consumer"
)
if (
    os.environ.get("BEDROCK_ROLE_NAME")
    and not os.environ.get("TG_TOKEN_CONSUMER_ROLE_NAME")
):
    log.warning(
        "BEDROCK_ROLE_NAME is deprecated; rename to "
        "TG_TOKEN_CONSUMER_ROLE_NAME (drop in v1.1)."
    )


def _resolve_account_id() -> str:
    """Resolve account ID from env, or fall back to STS. Surface
    the source so error paths can show "creds account != configured
    account" mismatches cleanly. (#217)"""
    if ACCOUNT_ID:
        return ACCOUNT_ID
    try:
        sts = boto3.client("sts", region_name=REGION)
        ident = sts.get_caller_identity()
        return ident["Account"]
    except Exception as e:
        raise RuntimeError(
            "AWS_ACCOUNT_ID env unset and STS GetCallerIdentity "
            f"failed: {e}"
        )


# #809 (reverses #746 defect-1 / #804 admin refusal): under the
# #746 DENYLIST (fail-open), DenyBlockedModels denies ONLY the
# org-blocked models — not everything-but-allowed — so attaching
# tg-BedrockQuotaDeny role-wide to ANY role (including an
# AdministratorAccess role) does the intended thing: it blocks the
# listed model(s) for everyone on the role and denies the named
# identities. That is NOT the allow-list-era freeze. The
# AdministratorAccess refusal was a vestige of the allow-list era
# and is removed — there is no longer any role-name / admin logic.
_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::\d+:role/(?P<role>.+)$")


def _role_name_from_arn(arn: str | None) -> str | None:
    """IAM role NAME from a role ARN, or from an assumed-role ARN
    (arn:aws:sts::<acct>:assumed-role/<role>/<session>). None if
    neither shape matches."""
    if not arn:
        return None
    m = _ROLE_ARN_RE.match(arn)
    if m:
        return m.group("role")
    m2 = re.match(
        r"^arn:aws:sts::\d+:assumed-role/(?P<role>[^/]+)/", arn)
    return m2.group("role") if m2 else None


def _attach_policy_to_role(iam, role_name: str, policy_arn: str,
                           fatal: bool) -> None:
    """Idempotent AttachRolePolicy. `fatal=True` (the configured
    consumer role) raises on an unexpected failure — governance is
    OFF if THAT attach fails (#746 defect 3). `fatal=False` (an
    extra role a managed user assumes) logs and continues — one
    bad role must not abort enforcement for the others (#809)."""
    try:
        iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code")
        if code == "EntityAlreadyExists":
            return
        if code == "LimitExceeded":
            log.warning(
                "deny_reconciler: attach hit the managed-policy "
                "limit on role %s: %s", role_name, e)
            return
        if not fatal:
            log.warning(
                "deny_reconciler: could not attach %s to %s: %s",
                policy_arn, role_name, e)
            return
        raise RuntimeError(
            f"deny_reconciler: attach_role_policy FAILED ({code}) for "
            f"role {role_name!r} — governance is OFF until this "
            f"resolves. Check TG_TOKEN_CONSUMER_ROLE_NAME points at a "
            f"real governed role: {e}"
        ) from e


def _ensure_policy(iam, account_id: str, desired: str) -> None:
    """Create tg-BedrockQuotaDeny on first run (and attach to the
    configured consumer role) so the reconciler is self-healing.
    (#217) #809: no admin-role refusal — denylist semantics make a
    role-wide attach safe on any role."""
    try:
        iam.create_policy(
            PolicyName=POLICY_NAME,
            Path="/",
            PolicyDocument=desired,
            Description=(
                "Token Governance — per-user Bedrock deny "
                "auto-managed by deny_reconciler."
            ),
        )
        log.warning(
            "deny_reconciler: created missing policy %s in account %s",
            POLICY_NAME, account_id,
        )
    except ClientError as e:
        # If a concurrent run created it, treat as success.
        code = (e.response or {}).get("Error", {}).get("Code")
        if code != "EntityAlreadyExists":
            raise
    # Attach to the configured consumer role (idempotent, fatal —
    # if THIS attach fails governance is silently OFF, #746 defect 3).
    _attach_policy_to_role(
        iam, ROLE_NAME,
        f"arn:aws:iam::{account_id}:policy/{POLICY_NAME}",
        fatal=True)


def _reconcile_policy_attachments(
    iam, policy_arn: str, desired_roles: set[str]) -> None:
    """#809: attach tg-BedrockQuotaDeny to every role a governed/
    blocked user assumes, and detach it from roles no such user
    assumes anymore. The configured consumer role (ROLE_NAME) is
    ALWAYS kept attached (it's the baseline governed role + the
    self-heal target), never detached. No admin-role guard — the
    denylist makes a role-wide attach correct, not a freeze.
    Best-effort per role: a single failure is logged, not fatal."""
    keep = set(desired_roles) | {ROLE_NAME}
    attached: set[str] = set()
    try:
        paginator = iam.get_paginator("list_entities_for_policy")
        for page in paginator.paginate(
                PolicyArn=policy_arn, EntityFilter="Role"):
            for r in page.get("PolicyRoles", []):
                if r.get("RoleName"):
                    attached.add(r["RoleName"])
    except ClientError as e:
        log.warning(
            "deny_reconciler: list_entities_for_policy failed for "
            "%s (%s) — attaching desired roles without detach pass",
            policy_arn, e)
        attached = set()
    for role in keep - attached:
        _attach_policy_to_role(iam, role, policy_arn, fatal=False)
        log.warning(
            "deny_reconciler: attached %s to role %s (#809)",
            policy_arn, role)
    for role in attached - keep:
        # #1011: never DetachRolePolicy on an AWSReservedSSO_* (IDC)
        # role. The counterintuitive trap: dropping IDC roles from
        # `keep` (above) means any AWSReservedSSO_* role that happens
        # to be attached now falls into `attached - keep` — so the
        # detach pass would actively race IDC's re-provision, the
        # mirror image of the attach problem. IDC owns those
        # attachments (via the #1010 permission-set reference); tg
        # must never attach OR detach them directly.
        if role.startswith("AWSReservedSSO_"):
            log.info(
                "deny_reconciler: skipping detach of IDC role %s "
                "(IDC owns this attachment; #1011)", role)
            continue
        try:
            iam.detach_role_policy(RoleName=role, PolicyArn=policy_arn)
            log.warning(
                "deny_reconciler: detached %s from role %s "
                "(no governed user assumes it) (#809)",
                policy_arn, role)
        except ClientError as e:
            log.warning(
                "deny_reconciler: could not detach %s from %s: %s",
                policy_arn, role, e)


# #626/#627: the four Bedrock invoke actions both deny classes
# (model-denylist + per-person/per-role quota) MUST list.
# Converse/ConverseStream are distinct IAM actions — NOT blocked
# by an InvokeModel deny — so a blocked model, or a blocked
# principal, stays reachable via Converse unless listed
# explicitly. (Epic #618; asserted in tests.)
_INVOKE_ACTIONS = [
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
]

# #746: CRIS geo prefixes the catalog model_ids carry. A blocked
# model must match in EVERY region/profile (the stage freeze was a
# us-east-1/us.* allow-list missing the real us-west-2/global.*
# traffic), so we strip the geo prefix to a region/account/profile-
# agnostic token and wildcard it into the resource ARNs. Order
# matters only in that we strip at most one leading prefix.
_CRIS_PREFIXES = ("us.", "global.", "apac.", "eu.")


def _agnostic_token(model_id: str) -> str:
    """Strip a leading CRIS geo prefix (us./global./apac./eu.) from
    a model_id so the denylist matches the model under ANY inference
    profile. e.g. 'global.anthropic.claude-opus-4-8' and
    'us.anthropic.claude-opus-4-8' both reduce to
    'anthropic.claude-opus-4-8'. A model_id without a known prefix
    (a bare foundation-model id) is returned unchanged."""
    s = (model_id or "").strip()
    for p in _CRIS_PREFIXES:
        if s.startswith(p):
            return s[len(p):]
    return s


def _model_denylist_statement(blocked: list[str]) -> dict | None:
    """#746: build the `DenyBlockedModels` DENYLIST statement —
    `Deny` the four invoke actions on `Resource` = the blocked
    models (wildcarded). An explicit Deny overrides any broad
    `bedrock:*` Allow upstream, so this blocks every principal on
    the role regardless of how they got access (role-wide; no
    identity condition).

    FAIL-OPEN by owner decision (2026-06-07, reverses the #618/#626
    allow-list): a model NOT on the block-list is ALLOWED, including
    models AWS ships in the future. Spend caps (QuotaDeny) enforce
    separately. Returns None when the block-list is empty — we emit
    NO model statement, so the default posture is allow-every-model
    (a `Resource: []` is invalid IAM, and 'block nothing' = no
    statement).

    Each blocked model_id is reduced to a region/account/profile-
    agnostic token and matched via BOTH resource types an
    InvokeModel/Converse call can present as:
      arn:aws:bedrock:*:*:inference-profile/*<token>*
      arn:aws:bedrock:*::foundation-model/*<token>*   (empty acct)
    The `*` wildcard spans '/', so us.* / global.* / apac.* and
    every region match the one token."""
    if not blocked:
        return None
    resources: list[str] = []
    seen: set[str] = set()
    for model_id in blocked:
        tok = _agnostic_token(model_id)
        if not tok:
            continue
        for res in (
            f"arn:aws:bedrock:*:*:inference-profile/*{tok}*",
            f"arn:aws:bedrock:*::foundation-model/*{tok}*",
        ):
            if res not in seen:
                seen.add(res)
                resources.append(res)
    if not resources:
        return None
    return {
        "Sid":      "DenyBlockedModels",
        "Effect":   "Deny",
        "Action":   list(_INVOKE_ACTIONS),
        "Resource": resources,
    }


def _should_deny(
    user: User,
    spend: float,
    cap: float,
) -> bool:
    # #750: clean 2-way. force_blocked is the manual admin override
    # — deny regardless of spend. Everything else is governed purely
    # by the cap (the auto over-cap `blocked` status is a label the
    # reconciler sets when spend >= cap; it is NOT a separate deny
    # rule). The old time-boxed `unblock_expires_at` reprieve is
    # gone: Unblock clears force_blocked and lets THIS cap check
    # re-decide, so an over-cap user re-denies on the next tick (no
    # cap-free window). To let an over-cap user through, raise the
    # cap — there is no separate temporary-unblock path.
    if user.status == "force_blocked":
        return True
    return spend >= cap


def _emails_from_policy_doc(doc: dict) -> set[str]:
    """Extract emails currently denied by the policy doc.
    aws:userid entries look like "*:<email>". Returns the
    set of <email>s. Tolerates the no-op "none" sentinel by
    returning an empty set in that case.
    """
    out: set[str] = set()
    for stmt in doc.get("Statement") or []:
        cond = stmt.get("Condition") or {}
        like = cond.get("StringLike") or {}
        ids = like.get("aws:userid") or []
        if isinstance(ids, str):
            ids = [ids]
        for v in ids:
            if ":" in v:
                tail = v.split(":", 1)[1]
                if tail and tail != "none":
                    out.add(tail)
    return out


# ── spend-cap alert notifications ───────────────────────────────────
# Email the user (and their team/org admins) on a spend-cap
# transition: a WARN when projected/billed spend first crosses the
# admin-set warn threshold, a BLOCKED when an active principal goes
# over cap, and an UNBLOCKED when an over-cap principal returns under
# cap. De-dup is per-transition so a hot account doesn't email every
# tick: the warn latch (last_warn_sent_at) and the last-notified
# status (last_status_notified) live on the user row and are written
# in the same session that mutates status, so they persist on commit.


def _spend_alert_events(
    old_status: str,
    new_status: str,
    effective_spend: float,
    cap: float,
    warn_pct: int,
    exceeded_on: bool,
    warn_latched: bool,
) -> list[str]:
    """Decide which spend-cap alert events fire this tick. Pure (no DB
    / no IAM) so the transition logic is unit-testable in isolation;
    the caller turns the event list into emails + latch writes.

    Returns a subset of {"warn", "blocked", "unblocked"}:
      - "warn"      — effective_spend/cap >= warn_pct/100 AND the warn
                      latch isn't already set (one warn per upward
                      cross; the caller clears the latch when the ratio
                      drops back under, so a later re-cross re-warns).
                      Requires cap > 0 and an ACTIVE status — a user
                      who's already blocked/over-cap is past the
                      "approaching" stage and gets the block notice,
                      not a warn.
      - "blocked"   — active → blocked/force_blocked this tick (only
                      when exceeded-email is on).
      - "unblocked" — blocked/force_blocked → active this tick.
    A block supersedes a warn for the same tick (no point warning about
    a cap the user just blew past)."""
    events: list[str] = []
    became_blocked = (
        old_status == "active"
        and new_status in ("blocked", "force_blocked"))
    became_unblocked = (
        old_status in ("blocked", "force_blocked")
        and new_status == "active")
    if became_blocked:
        if exceeded_on:
            events.append("blocked")
    elif became_unblocked:
        events.append("unblocked")
    elif (new_status == "active" and cap > 0 and not warn_latched
            and (effective_spend / cap) >= (warn_pct / 100.0)):
        events.append("warn")
    return events


def _alert_recipients(db, user: User) -> list[str]:
    """The emails to notify for `user`'s spend-cap event: the user
    plus the admins for their team (walking up parent_team_id so a
    parent-team admin is included), falling back to the org admins
    when the user has no team or no team_admin. De-duped, the user
    first; an admin who is also the user isn't emailed twice."""
    recipients: list[str] = []
    seen: set[str] = set()

    def _add(email: str | None) -> None:
        if email and email not in seen:
            seen.add(email)
            recipients.append(email)

    _add(user.email)

    # Walk the team chain (the user's team + every ancestor) and
    # collect team_admins on any of them.
    team_admins: list[str] = []
    team_id = user.team_id
    visited: set[str] = set()
    while team_id and team_id not in visited:
        visited.add(team_id)
        rows = (
            db.query(AdminRole.email)
            .filter(AdminRole.role == "team_admin",
                    AdminRole.team_id == team_id)
            .all()
        )
        team_admins.extend(r.email for r in rows)
        parent = (
            db.query(Team.parent_team_id)
            .filter(Team.team_id == team_id)
            .first()
        )
        team_id = parent.parent_team_id if parent else None

    if team_admins:
        for email in team_admins:
            _add(email)
    else:
        # No team / no team_admin → fall back to the org admins.
        org_admins = (
            db.query(AdminRole.email)
            .filter(AdminRole.role == "org_admin")
            .all()
        )
        for r in org_admins:
            _add(r.email)

    return recipients


def _spend_basis_phrase(enforce_on_estimate: bool) -> str:
    """How the number was computed, for the email body."""
    if enforce_on_estimate:
        return (
            "based on your projected spend including the unbilled "
            "window (AWS bills Bedrock with up to ~24h lag)")
    return "based on billed spend (AWS bills with up to ~24h lag)"


def _alert_email(
    event: str,
    audience: str,
    user_email: str,
    effective_spend: float,
    cap: float,
    enforce_on_estimate: bool,
) -> tuple[str, str]:
    """Build the (subject, plain-text body) for one spend-cap email.
    `audience` is "user" (the person over/approaching their own cap)
    or "admin" (a team/org admin notified about that person). The body
    states why the recipient is getting it, the current status + spend
    vs cap, the basis (billed vs projected), and — for the user — what
    they can do."""
    pct = int(round((effective_spend / cap) * 100)) if cap > 0 else 0
    money = f"${effective_spend:,.2f} of ${cap:,.2f} (~{pct}%)"
    basis = _spend_basis_phrase(enforce_on_estimate)
    url = notify.app_url()
    link_line = f"\nSign in: {url}\n" if url else ""

    if audience == "user":
        if event == "warn":
            subject = "Heads-up: approaching your Bedrock spend cap"
            why = (
                "You're approaching your monthly Bedrock spend cap.")
        elif event == "blocked":
            subject = "Your Bedrock access is paused (spend cap reached)"
            why = (
                "You're over your monthly Bedrock spend cap, so your "
                "Bedrock access has been paused.")
        else:  # unblocked
            subject = "Your Bedrock access has been restored"
            why = (
                "You're back under your monthly Bedrock spend cap, so "
                "your Bedrock access has been restored.")
        body = (
            f"{why}\n\n"
            f"Spend: {money} this month, {basis}.\n"
            f"{link_line}"
        )
        if event in ("warn", "blocked"):
            body += (
                "\nWhat you can do: wait — your cap resets "
                "automatically on the 1st of the month (UTC) — or ask "
                "your admin to raise your cap.\n")
        return subject, body

    # audience == "admin"
    if event == "warn":
        subject = f"{user_email} is approaching their Bedrock spend cap"
        why = (
            f"You're the team admin for {user_email}. They're at "
            f"{pct}% of their ${cap:,.2f} monthly Bedrock cap.")
    elif event == "blocked":
        subject = f"{user_email} hit their Bedrock spend cap"
        why = (
            f"You're the team admin for {user_email}. They're over "
            f"their ${cap:,.2f} monthly Bedrock cap and their Bedrock "
            f"access has been paused.")
    else:  # unblocked
        subject = f"{user_email} is back under their Bedrock spend cap"
        why = (
            f"You're the team admin for {user_email}. They're back "
            f"under their ${cap:,.2f} monthly Bedrock cap and their "
            f"access has been restored.")
    body = (
        f"{why}\n\n"
        f"Spend: {money} this month, {basis}.\n"
        f"{link_line}"
    )
    if event in ("warn", "blocked"):
        body += (
            f"\nTo give them more headroom, raise their cap from the "
            f"Users page.\n")
    return subject, body


def _alert_webhook_line(
    event: str,
    user_email: str,
    effective_spend: float,
    cap: float,
) -> str:
    """One-line announcement for the Slack/webhook channel (fired once
    per event, not per recipient — a channel post is shared)."""
    money = f"(${effective_spend:,.2f} of ${cap:,.2f})"
    if event == "blocked":
        return (
            f"{user_email} was blocked — over Bedrock spend cap {money}")
    if event == "warn":
        return (
            f"{user_email} is approaching their Bedrock spend cap "
            f"{money}")
    # unblocked
    return (
        f"{user_email}'s Bedrock access was restored — back under cap "
        f"{money}")


def _send_spend_alerts(
    db,
    user: User,
    events: list[str],
    effective_spend: float,
    cap: float,
    enforce_on_estimate: bool,
) -> int:
    """Send the spend-cap notifications for one user's events.

    Email goes to the user + their admins (one per recipient).
    Webhook fires ONCE per event (a channel post is shared, not
    per-recipient). Both transports are independent + optional —
    either being unconfigured is a soft skip. Best-effort throughout:
    a send failure is logged, never raised (governance must not break
    on a failed notification). Returns the count of emails sent."""
    sent = 0
    recipients = _alert_recipients(db, user)
    for event in events:
        for to in recipients:
            audience = "user" if to == user.email else "admin"
            subject, body = _alert_email(
                event, audience, user.email,
                effective_spend, cap, enforce_on_estimate)
            res = notify.send_alert(to, subject, body)
            if res.get("sent"):
                sent += 1
            else:
                log.info(
                    "deny_reconciler: spend alert not sent to %s "
                    "(%s): %s", to, event, res.get("reason"))
        # One webhook announcement per event (channel-shared).
        line = _alert_webhook_line(
            event, user.email, effective_spend, cap)
        wres = notify.send_webhook(line)
        if not wres.get("sent"):
            log.info(
                "deny_reconciler: spend webhook not sent (%s): %s",
                event, wres.get("reason"))
    return sent


def run() -> dict:
    iam   = boto3.client("iam", region_name=REGION)
    account_id = _resolve_account_id()
    policy_arn = f"arn:aws:iam::{account_id}:policy/{POLICY_NAME}"

    with get_db() as db:
        default_cap = get_org_default_quota_usd(db)
        # #746: the org-wide block-list drives the Resource set of
        # the model DENYLIST Deny. Read it inside the session; []
        # means "no block-list configured" → no model statement
        # emitted → allow every model (fail-open, owner posture).
        blocked_models = get_blocked_models(db)

        # Spend-cap alert thresholds, read once for the whole loop.
        # warn_pct is the % of cap at which we email a heads-up;
        # exceeded_on gates whether an over-cap (block) emits an email.
        warn_pct = get_spend_alert_warn_pct(db)
        exceeded_on = get_spend_alert_exceeded(db)
        alerts_sent = 0

        users = db.query(User).all()

        # #643: month grain is gone — the monthly spend total is now
        # SUM over this month's per-day rows (usage_date >=
        # first-of-month). This MUST return the identical per-email
        # monthly figure the cap enforcement relied on.
        spend_rows = (
            db.query(
                CurUserSpend.email,
                func.sum(CurUserSpend.spend_usd).label("total"),
            )
            .filter(CurUserSpend.usage_hour >= month_start_utc())
            .group_by(CurUserSpend.email)
            .all()
        )
        spend_by_email = {r.email: float(r.total or 0) for r in spend_rows}

        # Spend-estimate config. In `enforce` mode the cap check runs
        # against billed + estimated-unbilled (projected MTD) so a
        # runaway user is blocked before AWS bills; in off/warn the
        # reconciler enforces on billed CUR exactly as before — the
        # estimate is display/alert-only. Read inside the session.
        estimate_strategy = get_spend_estimate_strategy(db)
        estimate_enforcement = get_spend_estimate_enforcement(db)
        enforce_on_estimate = (
            estimate_enforcement == ENFORCE_ENFORCE)
        # Audit trail for estimate-driven denies (who/projected-vs-billed).
        estimate_denies: list[dict] = []

        # #810: UNIFORM per-principal quota keying. Every denied
        # principal — person OR machine — keys on
        # `aws:userid = *:<identity_key>`, where identity_key is
        # the role-session-name (the classifier's last-segment
        # key). The #627 dual-keying (aws:PrincipalArn /
        # QuotaDenyByRole for machine roles) is REMOVED: deny
        # ALWAYS on the last segment, never on the role ARN
        # (owner decision). Accepted consequence: a machine
        # session whose last segment is an ephemeral instance-id
        # yields a deny that matches only THAT session — the cap
        # is enforced per observed session, not durably per-role.
        # deny_emails is the back-compat blocked/unblocked delta
        # the API + tests read (keyed on identity_key, == email
        # for humans; for machines it's the session name).
        deny_emails: list[str] = []
        deny_userids: list[str] = []   # ALL denied principals
        # #809: the set of IAM role NAMES that governed/blocked users
        # assume — tg-BedrockQuotaDeny is attached to every one of
        # these (no admin-role guard; denylist semantics make a
        # role-wide attach correct). A deny only evaluates on the role
        # it's attached to, so a user who assumes a role OTHER than
        # the configured consumer role (tg-org-admin → tg-install-...) is
        # only enforced once the policy lands on THAT role.
        governed_role_names: set[str] = set()
        for u in users:
            # Capture the status BEFORE any mutation below so the
            # spend-cap alert step can detect active→blocked /
            # blocked→active transitions for THIS tick.
            old_status = u.status
            cap   = u.cap_usd if u.cap_usd is not None else default_cap
            spend = spend_by_email.get(u.email, 0.0)
            # #836: `governed` is a PRECONDITION for ALL enforcement.
            # tg enforces nothing on a principal it doesn't govern —
            # an unmanaged (governed=False) principal is never denied
            # and never auto-blocked, even over cap or force_blocked.
            # The gate lives HERE at the call site (not inside
            # _should_deny, which stays a pure cap/force-block
            # predicate) so both the over-cap AND force_blocked paths
            # require governed together. Without this gate the
            # reconciler re-blocked an unmanaged over-cap user on its
            # next tick AND attached a live IAM deny to its role — the
            # "Unmanaged" + "blocked" contradiction (#836).
            # In enforce mode, the cap check sees billed + estimated
            # unbilled (projected MTD). The estimate never overwrites
            # billed CurUserSpend; it only widens the number the cap
            # check reads, and only for governed principals under cap on
            # billed alone (no point projecting an already-over user).
            effective_spend = spend
            if (enforce_on_estimate and bool(u.governed)
                    and spend < cap):
                proj = project_for_principal(
                    db, u.email,
                    billed_mtd=spend,
                    strategy=estimate_strategy)
                effective_spend = proj["projected"]
                if effective_spend >= cap:
                    estimate_denies.append({
                        "email": u.email,
                        "billed": proj["billed"],
                        "projected": proj["projected"],
                        "estimated": proj["estimated"],
                        "strategy": proj["strategy"],
                        "cap": cap,
                    })
            enforce = bool(u.governed) and _should_deny(
                u, effective_spend, cap)
            # Collect the role of any governed OR to-be-denied user so
            # the policy is attached wherever it must evaluate. (Only
            # governed users matter now — enforce already implies
            # governed, and an ungoverned user must not pull the policy
            # onto its role.)
            # #1011: never let an AWSReservedSSO_* (IDC) role enter
            # the attach set. _role_name_from_arn DOES return the
            # reserved-SSO role name for an IDC principal, so without
            # this skip a governed IDC user would pull the deny onto
            # its IDC-provisioned role via the attach pass — exactly
            # the direct attach #618 forbids (wiped on re-provision).
            # The governed IDC user is still enforced: its per-person
            # QuotaDeny statement is emitted regardless of role type;
            # only the role-ATTACHMENT is skipped (it lands via
            # tg-consumer or the #1010 permission-set reference).
            if u.governed and not _is_idc(u):
                rn = _role_name_from_arn(u.principal_arn)
                if rn:
                    governed_role_names.add(rn)
            if enforce:
                # #810: key on the identity_key (the last-segment
                # session name) for EVERY principal — one uniform
                # aws:userid statement, no per-role ArnLike branch.
                key = u.identity_key or u.email
                deny_emails.append(key)
                deny_userids.append(key)
                if u.status == "active" and spend >= cap:
                    u.status = "blocked"
            else:
                # #836 self-heal: an UNGOVERNED principal must never
                # carry a blocked/force_blocked status (it can't be
                # enforced, so the status is a lie that surfaces as the
                # "Unmanaged + blocked" contradiction). Reconcile it
                # back to active and clear any stale force-block, so the
                # pre-existing bad rows in the screenshot clear with no
                # manual action — its deny is already absent from the
                # rebuilt doc since it's not in deny_userids.
                if not u.governed:
                    if u.status in ("blocked", "force_blocked"):
                        u.status = "active"
                        u.force_blocked_at = None
                # A governed, now-under-cap user that was auto-blocked
                # returns to active (unchanged behavior).
                elif u.status == "blocked" and spend < cap:
                    u.status = "active"

            # Spend-cap alert notifications. Only governed, capped
            # users can warn (an ungoverned/uncapped principal is never
            # enforced, so there's nothing to alert on). The whole
            # block is wrapped in try/except so a send (or recipient
            # lookup) failure can NEVER abort the reconcile.
            try:
                if bool(u.governed) and cap > 0:
                    warn_latched = u.last_warn_sent_at is not None
                    events = _spend_alert_events(
                        old_status, u.status, effective_spend, cap,
                        warn_pct, exceeded_on, warn_latched)
                    # Warn latch: set it when a warn fires; clear it once
                    # the ratio drops back under the warn threshold so a
                    # later re-cross warns again (a raised cap also drops
                    # the ratio). Month rollover clears it in quota_reset.
                    if "warn" in events:
                        u.last_warn_sent_at = datetime.now(timezone.utc)
                    elif (warn_latched
                            and (effective_spend / cap)
                            < (warn_pct / 100.0)):
                        u.last_warn_sent_at = None
                    # Block/unblock fire once per status change, tracked
                    # via last_status_notified.
                    fire = list(events)
                    if "blocked" in fire and (
                            u.last_status_notified == u.status):
                        fire.remove("blocked")
                    if "unblocked" in fire and (
                            u.last_status_notified == "active"):
                        fire.remove("unblocked")
                    if fire:
                        alerts_sent += _send_spend_alerts(
                            db, u, fire, effective_spend, cap,
                            enforce_on_estimate)
                        if "blocked" in fire:
                            u.last_status_notified = u.status
                        if "unblocked" in fire:
                            u.last_status_notified = "active"
            except Exception as e:
                log.warning(
                    "deny_reconciler: spend-cap alert step failed for "
                    "%s (%s) — reconcile continues", u.email, e)

        # Audit-log every deny that an ESTIMATE (not billed CUR)
        # triggered — enforce mode let a projection drive a real IAM
        # deny, so record who / projected-vs-billed / when for review.
        for d in estimate_denies:
            log.warning(
                "estimate-driven deny: %s projected=$%.2f "
                "(billed=$%.2f est=$%.2f strategy=%s) >= cap=$%.2f",
                d["email"], d["projected"], d["billed"],
                d["estimated"], d["strategy"], d["cap"])

    # #746 + #810: compose the policy from up to two statement
    # classes (order: model-denylist first, then the ONE uniform
    # quota keying). A no-op shape is appended only when NOTHING
    # else was emitted, since IAM rejects an empty Statement list.
    #   1. #746 model-denylist Deny (DenyBlockedModels) — emitted
    #      only when a block-list is configured; role-wide, no
    #      identity condition. Fail-open: empty → no statement →
    #      every model allowed.
    #   2. #810 per-principal quota Deny (QuotaDeny) — EVERY denied
    #      principal (person AND machine), keyed uniformly on
    #      aws:userid *:<last-segment-session-name>. The #627
    #      per-role QuotaDenyByRole (aws:PrincipalArn) statement is
    #      REMOVED — deny never keys on the role ARN.
    statements: list[dict] = []
    model_stmt = _model_denylist_statement(blocked_models)
    if model_stmt is not None:
        statements.append(model_stmt)
    if deny_userids:
        # aws:userid resolves to "<RoleId>:<SessionName>" for an
        # assumed-role session. We match the SessionName half
        # exactly (the role-session-name, == the classifier's
        # last-segment identity_key) and wildcard the RoleId half,
        # so the deny matches that session under ANY role it
        # assumes. Sorted+deduped for a stable, idempotent doc.
        statements.append({
            "Sid":       "QuotaDeny",
            "Effect":    "Deny",
            "Action":    list(_INVOKE_ACTIONS),
            "Resource":  "*",
            "Condition": {
                "StringLike": {
                    "aws:userid": [
                        f"*:{e}" for e in sorted(set(deny_userids))
                    ],
                }
            },
        })
    if not statements:
        # IAM rejects Statement:[]. Use a no-op deny that never
        # matches so the policy stays valid + idempotent.
        statements.append({
            "Sid":       "QuotaDenyNoop",
            "Effect":    "Deny",
            "Action":    "bedrock:InvokeModel",
            "Resource":  "*",
            "Condition": {
                "StringEquals": {"aws:userid": "none"},
            },
        })
    desired = json.dumps(
        {"Version": "2012-10-17", "Statement": statements},
        separators=(",", ":"),
    )

    # #809: attach tg-BedrockQuotaDeny to every role a governed/
    # blocked user assumes (+ always the configured consumer role),
    # detaching from roles no governed user assumes anymore. No
    # admin-role guard — denylist semantics make a role-wide attach
    # correct. Runs every tick (attachments are independent of the
    # doc content). Best-effort: a failure here must not abort the
    # doc-update below. NOTE: an admin with iam:* can self-detach
    # this — a cooperative guardrail, not hard enforcement; an SCP
    # is the durable answer (out of scope, owner aware).
    try:
        _reconcile_policy_attachments(
            iam, policy_arn, governed_role_names)
    except ClientError as e:
        log.warning(
            "deny_reconciler: attachment reconcile failed (%s) — "
            "doc update continues", e)

    desired_set = set(deny_emails)
    prev_set: set[str] = set()
    try:
        try:
            current = iam.get_policy(PolicyArn=policy_arn)
        except ClientError as e:
            code = (e.response or {}).get("Error", {}).get("Code")
            if code == "NoSuchEntity":
                # First run on a fresh account, or the policy was
                # manually deleted. Re-create it from `desired` and
                # exit clean — next tick will pick up the steady-
                # state code path. (#217)
                # Wrap _ensure_policy in its own except so create/
                # attach failures (e.g. AccessDenied on iam:CreatePolicy
                # when the task role lacks the perm) surface their
                # real code instead of being re-raised by the outer
                # handler with the GetPolicy code. (#229)
                try:
                    _ensure_policy(iam, account_id, desired)
                except ClientError as ce:
                    ce_code = (ce.response or {}).get(
                        "Error", {}).get("Code") or "?"
                    # boto3 stores the operation on the
                    # exception itself (positional arg 2 of
                    # ClientError.__init__), NOT on
                    # response["Error"]["Operation"] — that
                    # key is unset on real ClientErrors.
                    op = getattr(
                        ce, "operation_name", None
                    ) or "_ensure_policy"
                    env_acct = ACCOUNT_ID or "<unset>"
                    raise RuntimeError(
                        f"IAM error ({ce_code}) on {op} for "
                        f"{policy_arn} "
                        f"(env AWS_ACCOUNT_ID={env_acct}, "
                        f"resolved={account_id}): {ce}"
                    ) from ce
                return {
                    "detail": (
                        f"created {policy_arn} — "
                        f"{len(deny_emails)} denied"
                    ),
                    "blocked":   sorted(desired_set),
                    "unblocked": [],
                    "alerts_sent": alerts_sent,
                }
            raise
        version_id = current["Policy"]["DefaultVersionId"]
        current_doc = iam.get_policy_version(
            PolicyArn=policy_arn,
            VersionId=version_id,
        )["PolicyVersion"]["Document"]
        prev_set = _emails_from_policy_doc(current_doc)
        current_str = json.dumps(current_doc, separators=(",", ":"),
                                 sort_keys=True)
        if current_str == json.dumps(
            json.loads(desired), separators=(",", ":"), sort_keys=True
        ):
            return {
                "detail":    f"no change — {len(deny_emails)} denied",
                "blocked":   [],
                "unblocked": [],
                "alerts_sent": alerts_sent,
            }

        # Prune old non-default versions
        versions = iam.list_policy_versions(
            PolicyArn=policy_arn
        )["Versions"]
        non_default = [v for v in versions if not v["IsDefaultVersion"]]
        if len(non_default) >= 4:
            oldest = sorted(non_default, key=lambda v: v["CreateDate"])[0]
            iam.delete_policy_version(
                PolicyArn=policy_arn,
                VersionId=oldest["VersionId"],
            )

        iam.create_policy_version(
            PolicyArn=policy_arn,
            PolicyDocument=desired,
            SetAsDefault=True,
        )
    except ClientError as e:
        # Surface resolved account + ARN so an admin sees account
        # mismatches without digging through CW Logs. (#217)
        code = (e.response or {}).get("Error", {}).get("Code") or "?"
        env_acct = ACCOUNT_ID or "<unset>"
        raise RuntimeError(
            f"IAM error ({code}) on {policy_arn} "
            f"(env AWS_ACCOUNT_ID={env_acct}, resolved={account_id}): "
            f"{e}"
        )

    blocked   = sorted(desired_set - prev_set)
    unblocked = sorted(prev_set - desired_set)
    return {
        "detail":    f"updated — {len(deny_emails)} denied",
        "blocked":   blocked,
        "unblocked": unblocked,
        "alerts_sent": alerts_sent,
    }
