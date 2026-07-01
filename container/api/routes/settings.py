from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import AdminConfig, QuotaPolicy
from db.org_config import (
    get_org_default_quota_usd,
    set_org_default_quota_usd,
    get_blocked_models,
    set_blocked_models,
    get_blocked_models_updated_at,
    get_spend_estimate_strategy,
    set_spend_estimate_strategy,
    get_spend_estimate_enforcement,
    set_spend_estimate_enforcement,
    get_spend_alert_warn_pct,
    set_spend_alert_warn_pct,
    get_spend_alert_exceeded,
    set_spend_alert_exceeded,
)
from db.notify_config import (
    get_smtp_config,
    set_smtp_config,
    set_webhook_url,
    webhook_configured,
)
from db.jira_feature import is_jira_enabled, set_jira_enabled
from db.vc_feature import is_vc_enabled, set_vc_enabled
from db.auth_config import (
    get_sso_button_label,
    set_sso_button_label,
    get_saml_config,
    get_saml_metadata_xml,
    set_saml_config,
    clear_saml_config,
    saml_provider_name,
)
from api import cognito_saml
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


def _cfg_get(db: Session, key: str, default=None):
    row = db.query(AdminConfig).filter(AdminConfig.key == key).first()
    return row.value if row else default


def _cfg_set(db: Session, key: str, value: str):
    row = db.query(AdminConfig).filter(AdminConfig.key == key).first()
    if row:
        row.value = value
    else:
        db.add(AdminConfig(key=key, value=value))
    db.flush()


@router.get("/policies/default")  # UI alias
@router.get("/policy/default")
def get_default_policy(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    row = db.query(QuotaPolicy).filter(QuotaPolicy.scope == "DEFAULT").first()
    return {"monthly_cap_usd": row.monthly_cap_usd if row else 0}


@router.post("/settings/alerts/test")
def test_alert(
    body: dict | None = None,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """
    Exercises a notification transport so the UI's two test buttons
    each have a target. Body {"channel": "email"|"webhook"} (default
    email). Email goes to the configured alert_recipient (or the
    caller's own email); webhook posts a one-line test announcement.

    Returns the soft {"sent": bool, "reason": ...} dict from the
    shared notify primitive — an unconfigured transport is sent:False
    (a soft skip), never a hard error, so the UI shows a hint rather
    than a 500. The --ui-write test treats sent=false as SKIP.
    """
    scope.require_org_admin()
    from worker import notify
    channel = ((body or {}).get("channel") or "email").strip().lower()
    if channel == "webhook":
        return notify.send_webhook("tg test alert")
    recipient = _cfg_get(db, "alert_recipient", "") or scope.email
    return notify.send_alert(
        recipient, "tg test alert",
        f"Test alert sent by {scope.email}")


def _notifications_response(db: Session) -> dict:
    """The Notifications settings payload — never the SMTP password or
    the webhook URL (both bearer secrets), only whether each is set."""
    cfg = get_smtp_config(db)
    return {
        "smtp_host": cfg["host"],
        "smtp_port": cfg["port"],
        "smtp_username": cfg["username"],
        "smtp_from": cfg["from"],
        "smtp_tls": cfg["tls"],
        "smtp_password_configured": bool(cfg["password"]),
        "webhook_configured": webhook_configured(db),
    }


@router.get("/settings/notifications")
def get_notifications(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """The notification transport config: SMTP host/port/user/from/tls,
    plus whether the SMTP password and the webhook URL are set (the
    secrets themselves are NEVER returned)."""
    scope.require_org_admin()
    return _notifications_response(db)


@router.put("/settings/notifications")
def put_notifications(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Set the notification transport config. Accepts any of
    smtp_host, smtp_port, smtp_username, smtp_password, smtp_from,
    smtp_tls, alert_webhook_url. A blank/absent password or webhook
    means "keep existing" (we never overwrite a stored secret with an
    empty value). Validates the tls enum + port range. Returns the
    same shape as GET (secrets never echoed).

    To CLEAR a stored SMTP password, send an explicit
    ``clear_smtp_password: true`` — an empty/blank ``smtp_password``
    alone never clears it (that stays keep-existing). The explicit
    flag avoids a magic-sentinel that could collide with a real
    password, and gives the admin a way to fully remove the stored
    credential via the UI."""
    scope.require_org_admin()
    body = body or {}
    fields: dict = {}
    for k in ("smtp_host", "smtp_username", "smtp_from"):
        if k in body:
            fields[k] = body[k]
    if "smtp_tls" in body:
        fields["smtp_tls"] = body["smtp_tls"]
    if "smtp_port" in body:
        fields["smtp_port"] = body["smtp_port"]
    # Explicit clear wins over a (blank) keep-existing password: an
    # admin asked to remove the stored credential.
    if body.get("clear_smtp_password"):
        fields["smtp_password"] = ""
    # Blank/absent password = keep existing (don't clobber the secret).
    elif str(body.get("smtp_password") or "").strip():
        fields["smtp_password"] = body["smtp_password"]
    if fields:
        try:
            set_smtp_config(db, **fields)
        except ValueError as e:
            raise HTTPException(400, str(e))
    # Blank/absent webhook = keep existing.
    if str(body.get("alert_webhook_url") or "").strip():
        set_webhook_url(db, body["alert_webhook_url"])
    return _notifications_response(db)


@router.get("/admin/config")
def get_admin_config(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Org-wide admin config. Returns at minimum the
    org default monthly quota in USD; additional keys may
    be added without breaking the response shape.

    `jira_enabled` (#447) is the runtime feature flag for the
    Velocity & Cost Jira tab + jira_* worker jobs. Default
    false; flipped from the Org Settings "Experimental
    features" toggle — no redeploy."""
    scope.require_org_admin()
    return {
        "org_default_quota_usd": get_org_default_quota_usd(db),
        # #1056: runtime feature flag for the Velocity & Cost page;
        # default OFF, flipped from the same Experimental-features card.
        "vc_enabled": is_vc_enabled(db),
        "jira_enabled": is_jira_enabled(db),
        # #746: surfaced read-only here for convenience; edits go
        # through PUT /settings/blocked-models.
        "blocked_models": get_blocked_models(db),
        # Spend-estimate config: estimator strategy (average|p90|peak)
        # and how the estimate is used (off|warn|enforce). Defaults
        # average + off (display-only, no behavior change).
        "spend_estimate_strategy": get_spend_estimate_strategy(db),
        "spend_estimate_enforcement":
            get_spend_estimate_enforcement(db),
        # Spend-cap alert config: the warn threshold (% of cap) and
        # whether an over-cap event emails. Defaults 80 + on.
        "spend_alert_warn_pct": get_spend_alert_warn_pct(db),
        "spend_alert_exceeded": get_spend_alert_exceeded(db),
    }


@router.put("/admin/config")
def put_admin_config(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    if "org_default_quota_usd" in body:
        v = body["org_default_quota_usd"]
        if not isinstance(v, (int, float)) or v < 0:
            raise HTTPException(
                400,
                "org_default_quota_usd must be a "
                "non-negative number",
            )
        set_org_default_quota_usd(db, float(v))
    if "vc_enabled" in body:
        v = body["vc_enabled"]
        if not isinstance(v, bool):
            raise HTTPException(
                400, "vc_enabled must be a boolean")
        set_vc_enabled(db, v)
    if "jira_enabled" in body:
        v = body["jira_enabled"]
        if not isinstance(v, bool):
            raise HTTPException(
                400, "jira_enabled must be a boolean")
        set_jira_enabled(db, v)
    if "spend_estimate_strategy" in body:
        try:
            set_spend_estimate_strategy(
                db, body["spend_estimate_strategy"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    if "spend_estimate_enforcement" in body:
        try:
            set_spend_estimate_enforcement(
                db, body["spend_estimate_enforcement"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    return {
        "org_default_quota_usd": get_org_default_quota_usd(db),
        "vc_enabled": is_vc_enabled(db),
        "jira_enabled": is_jira_enabled(db),
        "spend_estimate_strategy": get_spend_estimate_strategy(db),
        "spend_estimate_enforcement":
            get_spend_estimate_enforcement(db),
    }


@router.get("/settings/spend-alerts")
def get_spend_alerts_route(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Spend-cap email alert config: the warn threshold (% of cap at
    which the user + their admin get a heads-up) and whether an
    over-cap (block) event emails. Defaults 80% + on. Delivery still
    needs a notification transport (SMTP and/or webhook) configured in
    Settings → Notifications — the UI shows a soft hint when neither
    is set."""
    scope.require_org_admin()
    return {
        "warn_pct": get_spend_alert_warn_pct(db),
        "exceeded": get_spend_alert_exceeded(db),
    }


@router.put("/settings/spend-alerts")
def put_spend_alerts_route(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Set the spend-cap alert config. Body: {"warn_pct": 1..100,
    "exceeded": bool} — either field may be omitted to leave it
    unchanged. 400 on an out-of-range warn_pct or a non-bool
    exceeded."""
    scope.require_org_admin()
    if "warn_pct" in body:
        v = body["warn_pct"]
        if not isinstance(v, int) or isinstance(v, bool):
            raise HTTPException(
                400, "warn_pct must be an integer 1..100")
        try:
            set_spend_alert_warn_pct(db, v)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if "exceeded" in body:
        v = body["exceeded"]
        if not isinstance(v, bool):
            raise HTTPException(400, "exceeded must be a boolean")
        set_spend_alert_exceeded(db, v)
    return {
        "warn_pct": get_spend_alert_warn_pct(db),
        "exceeded": get_spend_alert_exceeded(db),
    }


@router.get("/settings/blocked-models")
def get_blocked_models_route(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """#746: the org-wide blocked-model list — the model_ids the
    DENYLIST Deny blocks for every managed principal. Empty list =
    no block-list configured → every model allowed (fail-open,
    owner posture reversal of #618/#626). Global for v1.1;
    per-user/team sets are v1.1.1."""
    scope.require_org_admin()
    _updated = get_blocked_models_updated_at(db)
    return {
        "blocked_models": get_blocked_models(db),
        # When the list was last saved — the apply-status UI compares
        # this to the last deny_reconciler run to show pending vs
        # enforced (reload-durable; not a client-only "just saved").
        "updated_at": _updated.isoformat() if _updated else None,
    }


@router.put("/settings/blocked-models")
def put_blocked_models_route(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """#746: replace the blocked-model list. Body:
    {"blocked_models": ["us.anthropic.claude-opus-4-1-...", ...]}.
    Each entry must be a non-empty model_id; the list is stored
    deduped + order-preserving. An empty list clears the block-list
    (every model allowed). The reconciler reduces each id to a
    region/profile-agnostic token, so list the model once and it's
    blocked under us.* / global.* / every region."""
    scope.require_org_admin()
    if "blocked_models" not in body:
        raise HTTPException(
            400, "blocked_models field required")
    try:
        cleaned = set_blocked_models(
            db, body["blocked_models"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"blocked_models": cleaned}


@router.put("/policies/default")  # UI alias
@router.put("/policy/default")
def set_default_policy(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    scope.require_org_admin()
    cap = body.get("monthly_cap_usd")
    if cap is None or not isinstance(cap, (int, float)) or cap < 0:
        raise HTTPException(400, "monthly_cap_usd must be a non-negative number")
    row = db.query(QuotaPolicy).filter(QuotaPolicy.scope == "DEFAULT").first()
    if row:
        row.monthly_cap_usd = cap
        row.updated_by = scope.email
    else:
        db.add(QuotaPolicy(scope="DEFAULT", monthly_cap_usd=cap, updated_by=scope.email))
    db.flush()
    return {"monthly_cap_usd": cap}


# ── runtime SAML / SSO login config ──────────────────────────────────


def _saml_response(db: Session) -> dict:
    """The full Settings payload: stored config + editable button
    label + live pool status + the Cognito-SP values to register on
    the IdP / IAM Identity Center side. Status + registration values
    are best-effort (never 500 the GET)."""
    cfg = get_saml_config(db)
    name = cfg.get("provider_name")
    status = (
        cognito_saml.provider_live_status(name)
        if name else
        {"present": False, "on_app_client": False, "error": None})
    return {
        **cfg,
        "sso_button_label": get_sso_button_label(db),
        "status": status,
        # The "config info to take to IDC" — Cognito is the SAML SP,
        # so these are the pool's values (entity id + ACS URL), entered
        # manually on the IdP side (Cognito publishes no SP metadata).
        "registration": cognito_saml.registration_values(
            cfg.get("email_attribute") or "email"),
    }


@router.get("/settings/saml")
def get_saml_settings(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """The runtime SSO login config. Returns the stored SAML
    connection, the editable button label (default
    ``Login with Your SSO``), live pool status (IdP present? on the
    app client?), and the Cognito-SP registration values to paste
    into the IdP. A bad pool/lookup surfaces in ``status.error`` —
    the GET never 500s."""
    scope.require_org_admin()
    return _saml_response(db)


@router.put("/settings/saml")
def put_saml_settings(
    body: dict,
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Configure SAML and/or the button label at runtime.

    A **label-only** change (``sso_button_label`` present, no
    ``provider_name``) persists with NO Cognito call. When SAML
    connection fields are present, validate + persist + apply to the
    live Cognito pool (create/update the SAML IdP, add it to the app
    client). A bad metadata URL etc. surfaces the Cognito reason as a
    400 — not a 500. The COGNITO password path is always preserved on
    the app client (lockout guard)."""
    scope.require_org_admin()

    has_conn = any(
        k in body for k in
        ("provider_name", "metadata_url", "metadata_xml"))

    # Label-only change: persist, no Cognito call.
    if "sso_button_label" in body:
        label = body["sso_button_label"]
        if not isinstance(label, str):
            raise HTTPException(
                400, "sso_button_label must be a string")
        set_sso_button_label(db, label)

    if has_conn:
        try:
            cfg = set_saml_config(
                db,
                provider_name=body.get("provider_name", ""),
                metadata_url=body.get("metadata_url"),
                metadata_xml=body.get("metadata_xml"),
                email_attribute=body.get("email_attribute"),
                idp_signout=bool(body.get("idp_signout", False)),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        # Pass the XML only when the URL form isn't in use (the two
        # are mutually exclusive in storage).
        xml = None if cfg["metadata_url"] else get_saml_metadata_xml(db)
        try:
            cognito_saml.apply_saml_provider(
                provider_name=cfg["provider_name"],
                email_attribute=cfg["email_attribute"],
                metadata_url=cfg["metadata_url"],
                metadata_xml=xml,
                idp_signout=cfg["idp_signout"],
            )
        except cognito_saml.CognitoApplyError as e:
            # Config persisted but Cognito rejected it — surface the
            # reason so the admin can fix the metadata/url. The stored
            # config reflects the attempt; status will show present=False.
            raise HTTPException(
                400, f"Cognito rejected the SAML config: {e}")

    return _saml_response(db)


@router.delete("/settings/saml")
def delete_saml_settings(
    scope: Scope = Depends(_scope),
    db: Session = Depends(_db),
):
    """Revert to Cognito-only login — remove the IdP from the
    app client + delete the provider, then clear the stored config.
    The password/bootstrap path stays intact. Idempotent."""
    scope.require_org_admin()
    name = saml_provider_name(db)
    if name:
        try:
            cognito_saml.delete_saml_provider(name)
        except cognito_saml.CognitoApplyError as e:
            raise HTTPException(
                400, f"Cognito delete failed: {e}")
    clear_saml_config(db)
    return _saml_response(db)
