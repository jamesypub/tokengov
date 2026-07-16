#!/usr/bin/env bash
# tg-ecs-install.sh
#
# Idempotent installer for the container stack on
# ECS Fargate (the target account in us-east-1).
#
# Deploys (or upgrades):

#   2. tg-bedrock-role             (CFN, with caller
#                                   trust principal)
#   3. tg-container-stack          (CFN: VPC + ALB +
#                                   ECS + RDS + ECR)
#   4. docker build + push to ECR
#   5. ECS service force-new-deployment
#   6. wait for steady state + ALB /api/version
#   7. seed bootstrap admin via /api/roles
#
# Required env vars:
#   AWS_PROFILE              deploy profile (e.g.
#                            tg-install-<account>)
#   TG_TARGET_ACCOUNT_ID        12-digit account; must
#                            match caller's account
#   TG_BOOTSTRAP_ADMIN_EMAIL    seeded as org_admin.
#                            If unset, the script
#                            prompts on a TTY.
#
# Optional:
#   AWS_REGION               default us-east-1
#   (#497: the ALB is always provisioned — the api runs in
#    private subnets behind it. The EnableAlb=false /
#    public-task-IP path is retired. AlbScheme controls
#    internal vs internet-facing; see #495.)
#   TG_ECS_IMAGE_URI         deploy a PREBUILT image instead of
#                            building locally (#877). Set it to a
#                            pullable image URI — e.g. the public
#                            public.ecr.aws/e9y1g4o2/tg-container:
#                            <version> the publish pipeline keeps —
#                            and the installer skips docker build +
#                            ECR push entirely (no Docker needed).
#                            Unset (default) → build from source.
#
# Required (the ALB ingress allowlist):
#   TG_ALLOWED_INGRESS_CIDRS comma-separated CIDR allowlist
#                            for the ALB. e.g.
#                            "203.0.113.0/24,198.51.100.7/32"
#                            Up to 4 CIDRs are wired into CFN
#                            param slots (extras dropped with a
#                            warning). 0.0.0.0/0 is allowed only
#                            when the login wall is on (#183/#416);
#                            see TG_REQUIRE_IP_ALLOWLIST below.
#   TG_REQUIRE_IP_ALLOWLIST  '1' restores the strict Amazon/internal
#                            posture: 0.0.0.0/0 rejected
#                            unconditionally (AppSec V2226500622 /
#                            GH #183). DEFAULT OFF for the public
#                            product — a customer may open ingress
#                            and rely on the login wall. Lock-step
#                            with tg_cli/validate.cidrs.
#
# Auth provider (#782):
#   TG_AUTH_PROVIDER         cognito (DEFAULT) | okta.
#     cognito — the always-on base login: the installer
#       deploys tg-cognito-pool after the ALB exists, seeds the
#       bootstrap admin, and derives TG_OIDC_* from its outputs.
#       NO Okta tenant required. (Okta/SAML federation is added
#       later via the admin UI, not at install.)
#     okta   — bring your own OIDC issuer. Selected automatically
#       when TG_OIDC_ISSUER is set, or force it explicitly. The
#       TG_OIDC_* trio + secret below are then required up-front.
#
# Required when TG_AUTH_REQUIRE_LOGIN=1 AND TG_AUTH_PROVIDER=okta
# (the cognito default fills these from the pool — leave unset):
#   TG_OIDC_ISSUER           e.g. https://example.okta.com
#   TG_OIDC_CLIENT_ID        Okta app client id
#   TG_OIDC_CLIENT_SECRET    Okta app client secret
#   TG_OIDC_REDIRECT_URI     e.g. http://<alb-dns>/auth/callback
#                            Must match the Okta app's
#                            registered redirect URI exactly.
#
# Optional:
#   TG_AUTH_REQUIRE_LOGIN    '1' (default) gates SPA + /docs
#                            behind /auth/login. '0' opens
#                            the SPA — only safe behind a
#                            tight CIDR allowlist.
#   TG_DESIRED_COUNT         ECS task count for api + worker
#                            (#979). Unset (default): a fresh
#                            install scales to 1; a RE-RUN
#                            preserves the deployed count (never
#                            silently scales a live service to 0
#                            — the #979 stage-down). Set
#                            explicitly (0/1/2…) to override,
#                            incl. a deliberate scale-to-0.
#   TG_ON_ORPHAN             recovery action when a re-run finds a
#                            tg resource (legacy tg-alb / tg-api-tg)
#                            left by a prior FAILED install that CFN
#                            can't adopt (#982): delete | retry |
#                            abort. Unset on a TTY → the installer
#                            explains + recommends + prompts [d/r/a];
#                            unset on a non-TTY → abort (never deletes
#                            unattended). delete clears the orphan(s)
#                            then continues.
#   TG_ASSUME_YES            '1' auto-confirms the pre-flight
#                            account go/no-go prompt (deploy into
#                            the resolved account). REQUIRED for a
#                            non-TTY/headless run; interactive runs
#                            prompt [y/N] instead. Does NOT bypass
#                            the wrong-account hard-fail (that gate
#                            is non-overridable).
#   TG_REGION_CONFIRM        '1' acknowledges an AWS_REGION that
#                            differs from us-east-1 (headless escape
#                            for the region go/no-go).
#   TG_ENV / TG_ENVIRONMENT  deployment environment (#570).
#                            Derived from TG_ENV (dev→dev,
#                            stage→stage, unset/other → prod),
#                            overridable via TG_ENVIRONMENT.
#                            dev/stage auto-enable the test-auth
#                            bypass (X-Tg-Test-Email → org-admin,
#                            no SigV4) for zero-config seeding +
#                            persona walk-throughs behind a tight
#                            CIDR allowlist; prod LOCKS it off
#                            (structurally — CFN forces
#                            TG_AUTH_TEST_TRUST=0 and rejects the
#                            on-state). No separate opt-in flag;
#                            the environment IS the control
#                            (#443/#468/#496/#570).
#
# TLS (cert-agnostic — #484/#474 dec 6). The installer never
# generates or imports a cert. Pick exactly one; with none set
# the deploy fails (HTTP is never silent):
#   TG_CERT_ARN              ACM cert ARN → HTTPS :443. Bring
#                            your own, or run
#                            scripts/tg-make-selfsigned-cert.sh
#                            and pass the ARN it prints.
#   TG_ISSUE_ACM_CERT=1      + TG_DOMAIN_NAME + TG_HOSTED_ZONE_ID
#                            → CFN auto-issues a public cert via
#                            Route53 DNS validation.
#   TG_ALLOW_PLAINTEXT_ALB=1 explicit HTTP-only opt-in (no TLS).
#                            Only safe behind a tight CIDR
#                            allowlist + login wall.
#
# Re-run safe:
#   - aws cloudformation deploy is idempotent
#   - docker build/push is layer-diff
#   - update-service --force-new-deployment rolls cleanly
#   - bootstrap admin POST is idempotent (200 on conflict)

set -euo pipefail

# ── Color helpers (mirror scripts/deploy-all.sh) ─────
step()  {
  printf '\n\033[1;34m== %s ==\033[0m\n' "$*"
}
ok()    {
  printf '\033[1;32m✓\033[0m %s\n' "$*"
}
warn()  {
  printf '\033[1;33m! %s\033[0m\n' "$*"
}
# A hard failure / operator abort. Exits with a RESERVED code (3) so the
# `tg` wizard can tell a deliberate abort or pre-deploy hard-fail (e.g.
# the account/region confirm gate, a validation error, a core-stack
# rollback) apart from a merely COSMETIC non-zero exit in the trailing
# post-"Done" summary block (which exits with a generic, non-3 code).
# The wizard treats exit 3 as FATAL regardless of stack health — it must
# NOT "ignore non-zero and continue to CUR" on an abort, even on a re-run
# where the container stack already exists from a prior install.
TG_ABORT_EXIT=3
fail()  {
  printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2
  exit "$TG_ABORT_EXIT"
}

# assert_stack_succeeded <stack-name> — fail LOUD if a CFN stack is in
# a rollback / failed terminal state. `aws cloudformation deploy` can
# return 0 even when the change set rolled back (e.g. a CREATE_FAILED
# on one resource → UPDATE_ROLLBACK_COMPLETE), which previously let the
# installer sail on to "Install complete" while the core stack was
# half-deployed and CUR silently never ran. Asserting the real terminal
# StackStatus turns that into an actionable non-zero abort BEFORE the
# CUR step (a core-stack rollback is fatal; only a CUR-only failure
# stays a best-effort warning). A *_COMPLETE that isn't ROLLBACK is the
# only success set.
assert_stack_succeeded() {
  local stack="$1" status
  status=$(aws cloudformation describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    --stack-name "$stack" \
    --query "Stacks[0].StackStatus" \
    --output text 2>/dev/null) || status="MISSING"
  case "$status" in
    CREATE_COMPLETE|UPDATE_COMPLETE)
      return 0 ;;
    *ROLLBACK*|*FAILED*)
      fail "$stack is in state $status — the deploy rolled back, so \
the install did NOT complete. Inspect the stack events: \
aws cloudformation describe-stack-events --stack-name $stack \
--region $AWS_REGION --query \
\"StackEvents[?ResourceStatus=='CREATE_FAILED'||\
ResourceStatus=='UPDATE_FAILED'].[LogicalResourceId,\
ResourceStatusReason]\" --output table . A common cause on a re-run \
is an orphaned tg-alb / tg-api-tg from a prior rolled-back install — \
clear it (or tear down + reinstall) and retry." ;;
    *)
      fail "$stack is in unexpected state '$status' — refusing to \
report success. Check the stack in the CloudFormation console." ;;
  esac
}

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# #921: bootstrap-admin Cognito password helper (Option A random /
# Option B operator-provided; both → CONFIRMED so forgot-password
# works). Defines tg_set_bootstrap_admin_password, used in step 7a.
# shellcheck source=tg-cognito-bootstrap-pw.sh
. "$REPO_ROOT/scripts/tg-cognito-bootstrap-pw.sh"

# ── 1. Validate required env vars ────────────────────
step "Validating environment"

# #768: AWS_PROFILE is optional. When set, every aws call passes
# "${PROFILE_ARGS[@]}"; when unset, we omit the flag and let
# boto/CLI resolve the default credential chain (instance role,
# SSO default, env creds) — a greenfield operator on instance-role
# creds shouldn't hit a cryptic `:?` failure. PROFILE_ARGS is the
# array spliced into each aws invocation ("${PROFILE_ARGS[@]}");
# empty array → no flag.
if [[ -n "${AWS_PROFILE:-}" ]]; then
  PROFILE_ARGS=(--profile "$AWS_PROFILE")
  PROFILE_HINT="--profile $AWS_PROFILE "
else
  PROFILE_ARGS=()
  PROFILE_HINT=""
  warn "AWS_PROFILE not set — using the default AWS credential \
chain (instance role / SSO default / env creds). Export \
AWS_PROFILE=tg-install-<account> to pin a named profile."
fi
: "${TG_TARGET_ACCOUNT_ID:?\
must export TG_TARGET_ACCOUNT_ID=<12-digit account>\
}"
# Derive Environment up front (dev→dev, stage→stage,
# unset/anything else → prod) so the bootstrap-admin gate below can
# relax for non-prod. The full rationale + the test-trust gate it
# feeds live with the export a few lines down; resolved here only so
# the require can read it.
case "${TG_ENVIRONMENT:-${TG_ENV:-}}" in
  dev)   TG_ENVIRONMENT=dev ;;
  stage) TG_ENVIRONMENT=stage ;;
  prod)  TG_ENVIRONMENT=prod ;;
  *)     TG_ENVIRONMENT=prod ;;   # unset / anything else → prod
esac
# Bootstrap admin is REQUIRED on prod (the seeded org_admin / SigV4
# caller). On a non-prod environment it may be left empty: such envs
# run with the test-trust bypass (X-Tg-Test-Email), and an empty
# bootstrap is deliberate — a set BOOTSTRAP_ADMIN_EMAIL would make a
# headerless request auto-authenticate as that admin, masking the real
# SAML/Cognito login path the env is also used to test (the
# auth.py:_resolve_caller headerless-auto-login gotcha).
if [[ -z "${TG_BOOTSTRAP_ADMIN_EMAIL:-}" ]]; then
  if [[ "$TG_ENVIRONMENT" == "prod" ]]; then
    if [[ -t 0 ]]; then
      read -rp "Bootstrap admin email (org_admin): " \
        TG_BOOTSTRAP_ADMIN_EMAIL
    else
      fail "TG_BOOTSTRAP_ADMIN_EMAIL not set and stdin \
is not a TTY; export it or run interactively"
    fi
  else
    warn "TG_BOOTSTRAP_ADMIN_EMAIL empty on \
Environment=$TG_ENVIRONMENT — no bootstrap auto-login (header-authed \
UAT + real login both testable). Set it to seed an org_admin."
  fi
fi
EMAIL_RE='^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'
# Validate only when set — an empty bootstrap is allowed on non-prod.
if [[ -n "${TG_BOOTSTRAP_ADMIN_EMAIL:-}" ]] \
    && ! [[ "$TG_BOOTSTRAP_ADMIN_EMAIL" =~ $EMAIL_RE ]]; then
  fail "Invalid email: $TG_BOOTSTRAP_ADMIN_EMAIL"
fi
# Normalize to a defined (possibly empty) value so the downstream bare
# "$TG_BOOTSTRAP_ADMIN_EMAIL" uses are safe under `set -u` when it's
# left unset on a non-prod install.
export TG_BOOTSTRAP_ADMIN_EMAIL="${TG_BOOTSTRAP_ADMIN_EMAIL:-}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
# #768: us-east-1 is the canonical deployment region — CUR 2.0 only
# operates there and Bedrock/the container stack follow it (CLAUDE.md
# env assumption #5). A stale AWS_REGION in the environment (e.g.
# us-west-2 left over from another project) would silently deploy the
# stack to the wrong region and orphan the us-east-1 CUR/Bedrock data.
# Warn loudly on a mismatch and require an explicit acknowledgement
# (interactive y/N, or TG_REGION_CONFIRM=1 for headless installs) so
# it can't slip by unnoticed. To deploy elsewhere on purpose, set
# TG_REGION_CONFIRM=1.
TG_EXPECTED_REGION="${TG_EXPECTED_REGION:-us-east-1}"
if [[ "$AWS_REGION" != "$TG_EXPECTED_REGION" ]]; then
  warn "AWS_REGION=$AWS_REGION differs from the expected \
deployment region $TG_EXPECTED_REGION. CUR 2.0 operates only in \
$TG_EXPECTED_REGION and Bedrock/the stack follow it — deploying to \
$AWS_REGION risks orphaning $TG_EXPECTED_REGION data."
  if [[ "${TG_REGION_CONFIRM:-}" == "1" ]]; then
    warn "TG_REGION_CONFIRM=1 — proceeding with $AWS_REGION."
  elif [[ -t 0 ]]; then
    read -rp "Deploy to $AWS_REGION anyway? [y/N] " _region_ok
    [[ "$_region_ok" =~ ^[Yy]$ ]] || fail "Aborted — re-run with \
AWS_REGION=$TG_EXPECTED_REGION (or set TG_REGION_CONFIRM=1 to \
deploy to $AWS_REGION on purpose)."
  else
    fail "AWS_REGION=$AWS_REGION != $TG_EXPECTED_REGION and stdin \
is not a TTY. Set AWS_REGION=$TG_EXPECTED_REGION, or \
TG_REGION_CONFIRM=1 to deploy to $AWS_REGION on purpose."
  fi
fi
# #497: the EnableAlb=false / public-task-IP path is retired. The
# ALB is the only endpoint — the installer no longer takes
# TG_ENABLE_ALB and always deploys behind the ALB.
# #495 (#474 6a): ALB scheme. Default internet-facing (reachable,
# SG-locked to corp CIDRs); 'internal' is the opt-in for
# customers with a corp↔VPC bridge. Maps to the AlbScheme CFN
# param.
export TG_ALB_SCHEME="${TG_ALB_SCHEME:-internet-facing}"
case "$TG_ALB_SCHEME" in
  internet-facing|internal) : ;;
  *) fail "TG_ALB_SCHEME must be internet-facing|internal \
(got '$TG_ALB_SCHEME')" ;;
esac
export TG_AUTH_REQUIRE_LOGIN="${TG_AUTH_REQUIRE_LOGIN:-1}"
# #782: admin auth provider for the ECS install. Cognito is the
# always-on BASE login (the bootstrap-admin path lives there);
# Okta/SAML is an OPTIONAL federated layer added later via the
# admin UI, NEVER required at install. So: default to cognito
# UNLESS the operator brought their own OIDC issuer (then okta —
# the bring-your-own-Okta path stays fully supported). When
# provider=cognito the installer stands up tg-cognito-pool after
# the ALB exists (step 7a) and wires its OIDC outputs; no Okta
# tenant is needed to get a working login.
export TG_AUTH_PROVIDER="${TG_AUTH_PROVIDER:-}"
if [[ -z "$TG_AUTH_PROVIDER" ]]; then
  if [[ -n "${TG_OIDC_ISSUER:-}" ]]; then
    TG_AUTH_PROVIDER=okta
  else
    TG_AUTH_PROVIDER=cognito
  fi
fi
case "$TG_AUTH_PROVIDER" in
  cognito|okta) : ;;
  *) fail "TG_AUTH_PROVIDER must be cognito|okta \
(got '$TG_AUTH_PROVIDER'). Cognito is the default base login; \
set okta only when bringing your own OIDC issuer." ;;
esac
# #570: the test-trust auth bypass (X-Tg-Test-Email → org-admin,
# no SigV4) is gated by a named Environment, not inferred. The
# derivation (TG_ENV dev→dev, stage→stage, unset/anything else →
# prod, overridable via TG_ENVIRONMENT) runs earlier — before the
# bootstrap-admin gate, which relaxes for non-prod. prod is the
# fail-safe default: an un-parameterized deploy gets the locked
# behavior. Export it here for the deploy steps below.
export TG_ENVIRONMENT
# #538/#791: the product version /api/version reports + the UI footer
# show. #791: computed HERE (early) so BOTH container-stack deploys
# pass it as the TgVersion task-def env — stamping the version at
# DEPLOY time. It's also baked into the image (Dockerfile build-arg)
# but the #553 image-skip path reuses a prior image, so the baked
# value can go stale ('dev'); the task-def env overrides it at
# runtime so the deploy-time version always wins.
# #1000: honor a pre-set TG_VERSION (the `tg` CLI derives the build
# version ONCE — runner.build_version — and passes it down, so the
# install-start banner and this deploy stamp can't drift). Standalone
# runs (no CLI) self-derive IDENTICALLY below.
# #1088: derive from the committed VERSION file + short HEAD SHA, NOT
# `git describe` — a force-moved release tag that a naive `git pull`
# leaves stale makes describe fall through to a bare SHA on customer
# clones. The VERSION file is checkout-independent. Fall back to the
# bare short SHA when VERSION is unreadable, then 'dev'.
if [[ -z "${TG_VERSION:-}" ]]; then
  _tg_ver=$(tr -d '[:space:]' < "$REPO_ROOT/VERSION" 2>/dev/null || true)
  _tg_sha=$(git rev-parse --short HEAD 2>/dev/null || true)
  _tg_dirty=""
  # Dirty iff an IMAGE-SOURCE file changed vs HEAD — scope to the paths
  # the Docker build COPYs (container, admin-ui/web), matching the
  # deploy-skip check below. Bare `git status --porcelain` counted
  # untracked scratch files (e.g. internal/researcher2/) and flipped
  # -dirty on a pristine tree.
  [[ -n "$(git status --porcelain -- container admin-ui/web 2>/dev/null)" ]] \
    && _tg_dirty="-dirty"
  if [[ -n "$_tg_ver" && -n "$_tg_sha" ]]; then
    TG_VERSION="v${_tg_ver}-g${_tg_sha}${_tg_dirty}"
  elif [[ -n "$_tg_ver" ]]; then
    TG_VERSION="v${_tg_ver}${_tg_dirty}"
  elif [[ -n "$_tg_sha" ]]; then
    TG_VERSION="${_tg_sha}${_tg_dirty}"
  else
    TG_VERSION="dev"
  fi
fi
# Login is always on for a real (prod) install. A login-off deploy
# (TG_AUTH_REQUIRE_LOGIN=0) is permitted ONLY on a dev/stage/test
# environment — for a throwaway click-through behind a tight CIDR
# allowlist. On prod it hard-fails here so an install can never finish
# in an unauthenticated, internet-facing half-state. This is a
# code-level guarantee, not a doc convention — it holds whether the
# installer is reached via `tg install` or run directly.
if [[ "$TG_AUTH_REQUIRE_LOGIN" == "0" \
      && "$TG_ENVIRONMENT" == "prod" ]]; then
  fail "Login cannot be disabled on a prod install \
(TG_AUTH_REQUIRE_LOGIN=0 + Environment=prod). Leave login on, or set \
TG_ENVIRONMENT=dev for a throwaway test environment (login-off is \
allowed only on dev/stage/test, behind a tight CIDR allowlist)."
fi
# dev/stage enable the bypass automatically (zero-config seeding +
# persona walk-throughs behind the CIDR allowlist); prod locks it
# off. No separate opt-in flag — the environment IS the control.
# (The CFN template belt-and-braces this: prod forces
# TG_AUTH_TEST_TRUST=0 even if EnableTestAuthTrust=true is passed,
# and the TestTrustNeverInProd Rule rejects the combination.)
if [[ "$TG_ENVIRONMENT" == "prod" ]]; then
  TG_TEST_TRUST_CFN=false
else
  TG_TEST_TRUST_CFN=true
  warn "Environment=$TG_ENVIRONMENT — test-auth bypass \
(X-Tg-Test-Email) is ON. Only safe behind a tight CIDR \
allowlist; never use a prod-facing endpoint this way \
(#443/#570)."
fi
# #481 (epic #474 dec 3): RDS DataProtection defaults to
# 'disposable' so the one-click install → tg destroy flow leaves
# a true clean slate (Delete policy, no DeletionProtection, 1-day
# backups → frictionless teardown, no orphaned paid snapshots).
# Set TG_DATA_PROTECTION=protected on any deployment whose data
# must survive a stack delete (Snapshot policies, DeletionProtection
# on, 7-day backups). Matches the CFN template default.
export TG_DATA_PROTECTION="${TG_DATA_PROTECTION:-disposable}"
case "$TG_DATA_PROTECTION" in
  protected|disposable) : ;;
  *) fail "TG_DATA_PROTECTION must be protected|disposable \
(got '$TG_DATA_PROTECTION')" ;;
esac
# #583: app-log retention (CloudWatch /ecs/tg-container). Default
# 7 days; must be a CloudWatch-allowed value (the CFN param's
# AllowedValues enforce it too — fail early here with a clear msg).
export TG_LOG_RETENTION_DAYS="${TG_LOG_RETENTION_DAYS:-7}"
case "$TG_LOG_RETENTION_DAYS" in
  1|3|5|7|14|30|60|90|180|365) : ;;
  *) fail "TG_LOG_RETENTION_DAYS must be one of \
1 3 5 7 14 30 60 90 180 365 (got '$TG_LOG_RETENTION_DAYS')" ;;
esac
# #595: app log level (TG_LOG_LEVEL → LogLevel CFN param).
export TG_LOG_LEVEL="${TG_LOG_LEVEL:-INFO}"
case "$TG_LOG_LEVEL" in
  DEBUG|INFO|WARNING|ERROR) : ;;
  *) fail "TG_LOG_LEVEL must be DEBUG|INFO|WARNING|ERROR \
(got '$TG_LOG_LEVEL')" ;;
esac

# ── TLS (cert-agnostic) ──────────────────────────────
# #484 (#474 dec 6, Option D): the installer is cert-agnostic —
# it never generates or imports a cert. Three operator-chosen
# TLS modes, no self-signed generation here:
#   1. TG_CERT_ARN set — bring your own ACM cert (prod-style),
#      or the ARN printed by scripts/tg-make-selfsigned-cert.sh
#      (the relocated self-signed path: run it out-of-band, it
#      does openssl + acm import under your own creds, then
#      feed the ARN here).
#   2. TG_ISSUE_ACM_CERT=1 + TG_DOMAIN_NAME + TG_HOSTED_ZONE_ID
#      — CFN auto-issues a public ACM cert via Route53 DNS
#      validation. The IssuedCert resource handles it during
#      pass-2; stack creation blocks ~3-5 min for validation.
#   3. neither — HTTP only, but ONLY with the explicit
#      TG_ALLOW_PLAINTEXT_ALB=1 opt-in (the CFN
#      NoSilentPlaintextAlb Rule rejects the deploy otherwise,
#      so HTTP is never silent). Resolved here (pre-deploy) so
#      pass-1 satisfies the Rule too — Option D removed the
#      self-signed step that used to need the post-pass-1
#      ALB DNS.
TG_CERT_ARN="${TG_CERT_ARN:-}"
TG_ISSUE_ACM_CERT="${TG_ISSUE_ACM_CERT:-0}"
TG_ALLOW_PLAINTEXT_ALB="${TG_ALLOW_PLAINTEXT_ALB:-0}"
TG_DOMAIN_NAME="${TG_DOMAIN_NAME:-}"
TG_HOSTED_ZONE_ID="${TG_HOSTED_ZONE_ID:-}"
case "$TG_ALLOW_PLAINTEXT_ALB" in
  0|1) : ;;
  *) fail "TG_ALLOW_PLAINTEXT_ALB must be 0 or 1 \
(got '$TG_ALLOW_PLAINTEXT_ALB')" ;;
esac

# Sanity: auto-issue requires both domain + hosted zone.
if [[ "$TG_ISSUE_ACM_CERT" == "1" && -z "$TG_CERT_ARN" ]]; then
  if [[ -z "$TG_DOMAIN_NAME" || -z "$TG_HOSTED_ZONE_ID" ]]; then
    fail "TG_ISSUE_ACM_CERT=1 requires TG_DOMAIN_NAME + TG_HOSTED_ZONE_ID"
  fi
fi

# Fail-fast on the same condition the CFN NoSilentPlaintextAlb
# Rule enforces, but with a friendlier message pointing at the
# fix (the Rule's AssertDescription only surfaces on the deploy
# API call). #497 retired EnableAlb=false — the ALB is now the
# sole, always-present endpoint — so this fires UNCONDITIONALLY:
# no-cert must hard-fail unless AllowPlaintextAlb=true.
if [[ -z "$TG_CERT_ARN" && "$TG_ISSUE_ACM_CERT" != "1" \
      && "$TG_ALLOW_PLAINTEXT_ALB" != "1" ]]; then
  fail "No TLS configured for the ALB. Choose one:
  • HTTPS, your cert:  export TG_CERT_ARN=<acm-arn>
  • HTTPS, self-signed: run
      scripts/tg-make-selfsigned-cert.sh
    then export TG_CERT_ARN=<printed-arn>
  • HTTPS, auto-issue: TG_ISSUE_ACM_CERT=1 +
      TG_DOMAIN_NAME + TG_HOSTED_ZONE_ID
  • HTTP (no TLS), on purpose:
      export TG_ALLOW_PLAINTEXT_ALB=1
HTTP is never silent (#474 dec 6)."
fi

TG_ALLOW_PLAINTEXT_CFN=$( \
  [[ "$TG_ALLOW_PLAINTEXT_ALB" == "1" ]] \
    && echo true || echo false)

# #1018: the TLS/cert existence check used to live HERE (before the
# account preflight), so a wrong-account run failed at the cert
# `describe` under the unintended account — a red herring that hid the
# real account-mismatch cause. It now runs AFTER the preflight block
# (resolve identity → mismatch hard-fail → confirm), so a wrong-account
# run dies at the clear account-mismatch message and never reaches the
# cert describe. See "TLS / cert existence check" below the preflight.

if ! [[ "$TG_TARGET_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  fail "TG_TARGET_ACCOUNT_ID must be 12 digits"
fi

# ── CIDR allowlist (#183) ────────────────────────────
# Fail-closed: an empty allowlist means CFN creates no
# public ingress rules at all.
#
# #416 / #183 risk-acceptance (2026-06-02): 0.0.0.0/0 is
# permitted ONLY as an explicit opt-in when the app login
# gate is on (TG_AUTH_REQUIRE_LOGIN=1) — the verified login
# wall is the compensating control. With the login gate OFF,
# world-open ingress is the genuinely-unsafe combo the AppSec
# finding (V2226500622) was about, so it still hard-fails.
# The default/recommendation remains a real admin/VPN CIDR.
#
# #875: TG_REQUIRE_IP_ALLOWLIST (default OFF for the public
# product) restores the strict Amazon/internal posture — when
# on, 0.0.0.0/0 is rejected unconditionally regardless of the
# login gate. Kept in lock-step with tg_cli/validate.cidrs.
if [[ -z "${TG_ALLOWED_INGRESS_CIDRS:-}" ]]; then
  fail "TG_ALLOWED_INGRESS_CIDRS must be set to a \
comma-separated CIDR allowlist (your office / VPN \
egress). Empty = no public ingress; refuse to deploy \
silently. See INSTALL.md § Auth + ingress allowlist."
fi
case "${TG_REQUIRE_IP_ALLOWLIST:-}" in
  1|y|Y|yes|YES|true|TRUE) _TG_STRICT_ALLOWLIST=1 ;;
  *) _TG_STRICT_ALLOWLIST=0 ;;
esac
IFS=',' read -ra _CIDR_ARR \
  <<< "$TG_ALLOWED_INGRESS_CIDRS"
TG_CIDR_1="${_CIDR_ARR[0]:-}"
TG_CIDR_2="${_CIDR_ARR[1]:-}"
TG_CIDR_3="${_CIDR_ARR[2]:-}"
TG_CIDR_4="${_CIDR_ARR[3]:-}"
if [[ ${#_CIDR_ARR[@]} -gt 4 ]]; then
  warn "Only the first 4 CIDRs are used (CFN has 4 \
slots); ignoring the rest."
fi
for c in "$TG_CIDR_1" "$TG_CIDR_2" \
         "$TG_CIDR_3" "$TG_CIDR_4"; do
  [[ -z "$c" ]] && continue
  if [[ "$c" == "0.0.0.0/0" ]]; then
    if [[ "$_TG_STRICT_ALLOWLIST" == "1" ]]; then
      fail "0.0.0.0/0 is rejected: TG_REQUIRE_IP_ALLOWLIST \
is on (Amazon/internal posture, AppSec V2226500622 / GH \
#183). Use a real admin/VPN CIDR."
    elif [[ "$TG_AUTH_REQUIRE_LOGIN" == "1" ]]; then
      warn "0.0.0.0/0 in TG_ALLOWED_INGRESS_CIDRS — \
world-open ingress, permitted only because the login gate \
is on (TG_AUTH_REQUIRE_LOGIN=1) as the compensating control \
(#183/#416). Prefer a real admin/VPN CIDR for prod."
    else
      fail "0.0.0.0/0 is not allowed with the login gate \
OFF (TG_AUTH_REQUIRE_LOGIN=0) — unauthenticated + world-open \
is the forbidden combo (AppSec V2226500622 / GH #183). Set \
TG_AUTH_REQUIRE_LOGIN=1 or use a real admin/VPN CIDR."
    fi
  fi
done

# ── OIDC required when login gate is on ──────────────
# #782: only the OKTA (bring-your-own-OIDC) path needs the
# TG_OIDC_* trio + secret supplied here. On the COGNITO path
# (the default) the installer deploys tg-cognito-pool AFTER the
# ALB DNS exists (step 7a) and derives ISSUER / CLIENT_ID /
# REDIRECT_URI / secret from its outputs — so demanding them
# up-front would make the always-on Cognito login unreachable
# via the ECS installer (the #782 bug). Skip the hard-fail for
# cognito; the pool guarantees the values before pass-2.
#
# ISSUER / CLIENT_ID / REDIRECT_URI are required for okta.
# CLIENT_SECRET is special (#432, replacing #398/#403): the
# secret now lives in Secrets Manager, written post-deploy in
# step 7a. On an UPGRADE (stack already exists) an absent
# secret means "keep the value already in Secrets Manager" —
# don't hard-fail. Only a truly first-time install (no stack)
# needs the secret supplied here.
if [[ "$TG_AUTH_REQUIRE_LOGIN" == "1" \
      && "$TG_AUTH_PROVIDER" == "okta" ]]; then
  for v in TG_OIDC_ISSUER TG_OIDC_CLIENT_ID \
           TG_OIDC_REDIRECT_URI; do
    if [[ -z "${!v:-}" ]]; then
      fail "$v must be set for TG_AUTH_PROVIDER=okta when \
TG_AUTH_REQUIRE_LOGIN=1. Use TG_AUTH_PROVIDER=cognito (the \
default) to stand up a Cognito login with no Okta, or set \
TG_AUTH_REQUIRE_LOGIN=0 ONLY behind a tight CIDR allowlist + \
private network."
    fi
  done
  if [[ -z "${TG_OIDC_CLIENT_SECRET:-}" ]]; then
    # Empty secret is fine ONLY if a prior tg-container-stack
    # exists to preserve the value from. First install must
    # supply it.
    if aws cloudformation describe-stacks \
         "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
         --stack-name tg-container-stack \
         >/dev/null 2>&1; then
      ok "TG_OIDC_CLIENT_SECRET empty — preserving the \
value already in Secrets Manager (upgrade)"
    else
      fail "TG_OIDC_CLIENT_SECRET must be set on a \
first-time okta install (no tg-container-stack to preserve it \
from). On a later upgrade you may leave it empty to keep \
the secret already deployed."
    fi
  fi
fi

ok "AWS_REGION           = $AWS_REGION"
ok "TG_TARGET_ACCOUNT_ID    = $TG_TARGET_ACCOUNT_ID"
ok "TG_BOOTSTRAP_ADMIN_EMAIL= $TG_BOOTSTRAP_ADMIN_EMAIL"
ok "TG_AUTH_REQUIRE_LOGIN   = $TG_AUTH_REQUIRE_LOGIN"
# #570: the operator must SEE which auth mode they got.
if [[ "$TG_ENVIRONMENT" == "prod" ]]; then
  ok "Environment             = prod  → auth ENFORCED \
(test-auth bypass locked off)"
else
  ok "Environment             = $TG_ENVIRONMENT  → \
test-auth bypass ON (X-Tg-Test-Email)"
fi
ok "TG_DATA_PROTECTION      = $TG_DATA_PROTECTION"
ok "TG_ALLOWED_INGRESS_CIDRS=\
$TG_CIDR_1${TG_CIDR_2:+,$TG_CIDR_2}\
${TG_CIDR_3:+,$TG_CIDR_3}\
${TG_CIDR_4:+,$TG_CIDR_4}"

# ── 2. Pre-flight checks ─────────────────────────────
step "Pre-flight checks"

# #1115: INFORM the detected Bash version — do NOT gate on it. The
# product stays Bash-3.2-compatible (stock macOS); requiring Bash 4+
# would re-impose a `brew install bash` prerequisite (#1105/#1112), and
# the #1114 CI gate is what enforces 3.2-safety. Soft note on <4 only.
ok "Using Bash ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"
if [[ "${BASH_VERSINFO[0]}" -lt 4 ]]; then
  echo "    (Bash 3.x detected — supported; if you hit a shell \
error, please report it.)"
fi
# (Python is gated up front by the tg CLI — runner.check_python, #1115.)

CALLER_JSON=$(aws sts get-caller-identity \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
  --output json) \
  || fail "aws sts get-caller-identity failed"

CALLER_ACCT=$(printf '%s' "$CALLER_JSON" \
  | python3 -c \
  'import sys,json;\
print(json.load(sys.stdin)["Account"])')
CALLER_ARN=$(printf '%s' "$CALLER_JSON" \
  | python3 -c \
  'import sys,json;\
print(json.load(sys.stdin)["Arn"])')

# Credential-source label: the profile NAME when pinned, else the
# default chain (instance role / SSO / env). Never the creds.
if [[ -n "${AWS_PROFILE:-}" ]]; then
  CRED_SOURCE="profile $AWS_PROFILE"
else
  CRED_SOURCE="default chain (instance role / SSO / env)"
fi

# Labeled credential/account summary so the operator sees, at a
# glance, the credential SOURCE → resolved account → identity together
# (#1018: the AWS_PROFILE/source echo folds in here, adjacent to the
# resolved account, instead of a separate block far above — so a
# wrong-profile foot-gun is obvious at the point of decision). The
# resolved account is on its own line (no ARN-eyeballing needed).
ok "AWS_PROFILE       = ${AWS_PROFILE:-<default credential chain>}"
ok "Credential source : $CRED_SOURCE"
ok "Resolved identity : $CALLER_ARN"
ok "Resolved account  : $CALLER_ACCT"

# Wrong-account hard-fail (a safety control) — preserved, NON-
# overridable (TG_ASSUME_YES does not bypass it). Message names the
# credential source + both account IDs so the fix is obvious.
if [[ "$CALLER_ACCT" != "$TG_TARGET_ACCOUNT_ID" ]]; then
  fail "Credentials ($CRED_SOURCE) resolve to account \
$CALLER_ACCT, but you are targeting $TG_TARGET_ACCOUNT_ID. \
Export AWS_PROFILE=tg-install-<account> (or fix \
TG_TARGET_ACCOUNT_ID) and re-run."
fi
ok "Target account    : $TG_TARGET_ACCOUNT_ID   ✓ match"

# Account go/no-go (accounts already match). An ADDITIONAL gate on
# top of the hard-fail above: a wrong-account run is stopped by an
# explicit human "yes," not just the exact-match guard. Mirrors the
# AWS_REGION confirm idiom — interactive [y/N], TG_ASSUME_YES=1 for
# headless, fail fast (never hang on stdin) on a non-TTY without it.
if [[ "${TG_ASSUME_YES:-}" == "1" ]]; then
  ok "TG_ASSUME_YES=1 — proceeding with account $CALLER_ACCT."
elif [[ -t 0 ]]; then
  warn "About to deploy tg into account $CALLER_ACCT (region \
$AWS_REGION) using the $CRED_SOURCE."
  read -rp "Proceed? [y/N] " _acct_ok
  [[ "$_acct_ok" =~ ^[Yy]$ ]] || fail "Aborted — no resources \
created. Re-run when the account/profile is correct."
else
  fail "Account confirmation required but stdin is not a TTY. \
Re-run interactively, or set TG_ASSUME_YES=1 to proceed with \
account $CALLER_ACCT unattended."
fi

# aws CLI v2
AWSCLI_VER=$(aws --version 2>&1 | head -1)
if [[ "$AWSCLI_VER" != aws-cli/2.* ]]; then
  fail "aws CLI v2 required (got $AWSCLI_VER)"
fi
ok "$AWSCLI_VER"

# ── TLS / cert existence check (#888; reordered #1018) ────────
# Runs AFTER the account preflight above: CALLER_ACCT is resolved and
# == TG_TARGET_ACCOUNT_ID (the mismatch hard-fail already aborted a
# wrong-account run), so the cert describe runs under the INTENDED
# account and a failure here is a genuine cert problem, never a
# wrong-account red herring.
if [[ -n "$TG_CERT_ARN" ]]; then
  # #888: a shape-valid but NONEXISTENT/placeholder ARN (e.g.
  # .../certificate/dummy) otherwise prints "TLS enabled" here and
  # only blows up much later at the CFN ALB listener with an opaque
  # CertificateNotFound. Verify the cert exists AND is ISSUED in the
  # target account/region NOW, and fail fast with an actionable
  # message. (Shape is the validator's job — tg_cli/validate.cert_arn;
  # existence needs creds, so it lives here, in lock-step.)
  CERT_STATUS=$(aws acm describe-certificate \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --certificate-arn "$TG_CERT_ARN" \
    --query 'Certificate.Status' --output text 2>/dev/null || true)
  if [[ -z "$CERT_STATUS" || "$CERT_STATUS" == "None" ]]; then
    # #1018: report the account ACTUALLY queried ($CALLER_ACCT — set
    # and == target by now) plus the ARN's own embedded account (field
    # 5 of arn:aws:acm:REGION:ACCOUNT:certificate/...). A mismatch
    # between the two is itself a signal (the cert lives in a different
    # account than the creds), and CALLER_ACCT is never the misleading
    # target-fallback the old message printed.
    CERT_ARN_ACCT=$(printf '%s' "$TG_CERT_ARN" | cut -d: -f5)
    fail "TG_CERT_ARN not found in ACM (queried account \
$CALLER_ACCT, region $AWS_REGION; ARN names account \
${CERT_ARN_ACCT:-<unparseable>}): $TG_CERT_ARN
  The ARN is well-formed but no such certificate exists in the \
queried account — a placeholder/stale value, or a cert that lives \
in a different account, is the usual cause. Fix one of:
  • list real certs:  aws acm list-certificates \
${PROFILE_HINT}--region $AWS_REGION \\
      --query 'CertificateSummaryList[].CertificateArn'
  • self-signed:      scripts/tg-make-selfsigned-cert.sh \
(prints a real ARN)
  • auto-issue:       TG_ISSUE_ACM_CERT=1 + TG_DOMAIN_NAME + \
TG_HOSTED_ZONE_ID"
  fi
  if [[ "$CERT_STATUS" != "ISSUED" ]]; then
    fail "TG_CERT_ARN exists but is not ISSUED (status=\
$CERT_STATUS) in $AWS_REGION: $TG_CERT_ARN
  An ALB can only attach an ISSUED cert. If it's PENDING_VALIDATION, \
complete DNS/email validation first; if FAILED/EXPIRED, issue or \
import a new one."
  fi
  ok "TLS      : enabled (cert=$TG_CERT_ARN, status=ISSUED)"
  if [[ -n "$TG_DOMAIN_NAME" ]]; then
    ok "Domain   : $TG_DOMAIN_NAME (zone $TG_HOSTED_ZONE_ID)"
  fi
elif [[ "$TG_ISSUE_ACM_CERT" == "1" ]]; then
  ok "TLS      : auto-issue via CFN (domain=$TG_DOMAIN_NAME)"
  warn "Pass-2 deploy will block ~3-5 min for DNS validation"
else
  warn "TLS      : DISABLED — serving plain HTTP on :80 \
(TG_ALLOW_PLAINTEXT_ALB=1). Only safe behind a tight \
CIDR allowlist + login wall."
fi

# Docker build-capability preflight — ONLY when building from source
# (a prebuilt TG_ECS_IMAGE_URI needs no local Docker). `docker info`
# alone is insufficient: it SUCCEEDS even when Docker Desktop's BUILD is
# org-sign-in/policy blocked, so the install used to reach the build
# step (stack partly up) before failing. So we run a trivial throwaway
# build that exercises the SAME path the policy gates, BEFORE any deploy
# work. This is the backstop for a direct `bash tg-ecs-install.sh` run;
# the `tg` wizard runs the same probe earlier (and offers the prebuilt
# image).
if [[ -z "${TG_ECS_IMAGE_URI:-}" ]]; then
  if ! docker --version >/dev/null 2>&1; then
    fail "docker not found on PATH — building from source needs it. \
Install Docker (or pass TG_ECS_IMAGE_URI=<prebuilt image> to skip the \
build), then re-run."
  fi
  if ! docker info >/dev/null 2>&1; then
    fail "docker daemon not reachable — start Docker (Docker Desktop / \
systemctl start docker), then re-run."
  fi
  # Build-capability smoke: a trivial inline build, kept nowhere
  # (cacheonly). Catches the sign-in/policy block that docker info misses.
  _dbuild_err="$(printf 'FROM scratch\n' \
    | docker buildx build --output type=cacheonly - 2>&1)" || {
    if printf '%s' "$_dbuild_err" | grep -qiE \
       'sign[ -]?in|membership in the|organization is required|enforced by your administrator'; then
      fail "Docker can't build — Docker Desktop requires sign-in (org \
policy enforced by your administrator). Sign in to Docker Desktop, then \
re-run — or pass TG_ECS_IMAGE_URI=<prebuilt public image> to skip the \
build (no Docker)."
    fi
    fail "Docker can't build a trivial image (docker buildx build \
failed): ${_dbuild_err}. Fix the local Docker build, or pass \
TG_ECS_IMAGE_URI=<prebuilt image> to skip the build."
  }
  ok "$(docker --version) — build capability verified"
fi

# ECS service-linked role — required before any ECS
# cluster can be created. Idempotent: returns
# AlreadyExists if present.
SLR_OUT=$(aws iam create-service-linked-role \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
  --aws-service-name ecs.amazonaws.com 2>&1) || true
if echo "$SLR_OUT" | grep -qiE "has been taken|already"; then
  ok "AWS ECS Container Service ready"
elif echo "$SLR_OUT" | grep -q "RoleName"; then
  ok "AWS ECS Container Service ready"
else
  warn "ECS SLR check inconclusive: $SLR_OUT"
fi

# #772: VPC / Internet-Gateway quota pre-flight. tg-container-stack
# always creates a fresh 2-AZ VPC + IGW; on an account already at
# its VPC/IGW limit the stack only discovers this MID-DEPLOY and
# rolls back ROLLBACK_COMPLETE (after creating tg-bedrock-role + a
# template bucket). Catch it up front with an actionable message.
# Best-effort: counts come from describe-* (always available); the
# limit comes from Service Quotas (L-F678F1CE = VPCs per Region,
# L-A4707A72 = IGWs per Region) with a fallback to the AWS default
# 5 when the quota API isn't reachable. If we can't even count
# (describe failed), warn and proceed — don't block an install on a
# flaky read; the stack-level error still backstops.
_quota_count() {  # $1 = describe subcommand, $2 = json key
  aws ec2 "$1" "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --query "length($2)" --output text 2>/dev/null
}
_service_quota() {  # $1 = quota code; echoes integer limit or ""
  aws service-quotas get-service-quota \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --service-code vpc --quota-code "$1" \
    --query 'Quota.Value' --output text 2>/dev/null \
    | awk '{printf "%d", $1}'
}
_preflight_limit() {  # $1=label $2=describe $3=key $4=quota-code
  local label="$1" sub="$2" key="$3" qcode="$4"
  local count limit
  count=$(_quota_count "$sub" "$key")
  if ! [[ "$count" =~ ^[0-9]+$ ]]; then
    warn "$label: could not read current count (skipping \
quota pre-flight; the stack will still error if at limit)."
    return 0
  fi
  limit=$(_service_quota "$qcode")
  [[ "$limit" =~ ^[0-9]+$ && "$limit" -gt 0 ]] || limit=5  # AWS default
  if [[ "$count" -ge "$limit" ]]; then
    fail "$label: account is at its limit ($count/$limit) in \
$AWS_REGION. tg-container-stack creates a new one and cannot \
succeed. Re-run with an existing VPC (set TG_VPC_ID + \
TG_SUBNET_IDS, or pick 'Use an existing VPC' in the wizard), or \
raise the quota (Service Quotas console) / delete an unused one, \
then re-run. (#774)"
  fi
  ok "$label: $count/$limit in $AWS_REGION"
}

# #774/#779: BYO-VPC subnet egress pre-flight. The Fargate task must
# reach Secrets Manager + ECR + CloudWatch Logs to start at all (it
# pulls the DB-password secret + the image). tg creates NO networking
# on the BYO path, so the chosen subnets must provide that egress one
# of three ways, and the task's AssignPublicIp must match:
#   * PUBLIC  — subnet routes to an IGW → the task needs a PUBLIC IP
#     to egress → AssignPublicIp=ENABLED.
#   * NAT     — subnet routes to a NAT → egress with no public IP →
#     AssignPublicIp=DISABLED.
#   * ENDPOINTS — private, no NAT, but the VPC has interface endpoints
#     for secretsmanager + ecr.api + ecr.dkr + logs → DISABLED.
# #779: the prior check only looked for any igw-/nat- route and
# declared "egress OK" — which let a public-subnet + DISABLED combo
# through (no public IP, no NAT → zero egress → ASM init failure).
# This classifies the subnets and SETS TG_ASSIGN_TASK_PUBLIC_IP, and
# fails loud on a mix / on a private subnet without NAT-or-endpoints.
_byo_required_endpoints() {  # echoes count of the 4 needed iface eps
  aws ec2 describe-vpc-endpoints "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    --filters "Name=vpc-id,Values=$TG_VPC_ID" \
              "Name=vpc-endpoint-type,Values=Interface" \
    --query "length(VpcEndpoints[?contains(['com.amazonaws.${AWS_REGION}.secretsmanager','com.amazonaws.${AWS_REGION}.ecr.api','com.amazonaws.${AWS_REGION}.ecr.dkr','com.amazonaws.${AWS_REGION}.logs'], ServiceName)])" \
    --output text 2>/dev/null
}

_byo_egress_preflight() {
  local sids="${TG_SUBNET_IDS:-}"
  [[ -n "$sids" ]] || fail "TG_VPC_ID is set but TG_SUBNET_IDS is \
empty — supply ≥2 subnet ids (≥2 AZs) in $TG_VPC_ID."
  local sid routes az az_list="" n_public=0 n_nat=0 n_none=0
  IFS=',' read -ra _subs <<< "$sids"
  [[ ${#_subs[@]} -ge 2 ]] || fail "TG_SUBNET_IDS needs ≥2 subnets \
(RDS + ALB 2-AZ floor, #480); got ${#_subs[@]}."
  for sid in "${_subs[@]}"; do
    sid="$(echo "$sid" | xargs)"
    az=$(aws ec2 describe-subnets "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
      --region "$AWS_REGION" --subnet-ids "$sid" \
      --query 'Subnets[0].AvailabilityZone' --output text 2>/dev/null)
    [[ -n "$az" && "$az" != "None" ]] || fail "subnet $sid not found \
in $AWS_REGION (is it in $TG_VPC_ID?)."
    az_list="$az_list $az"
    # The subnet's route table (explicit assoc, else the VPC main).
    routes=$(aws ec2 describe-route-tables "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
      --region "$AWS_REGION" \
      --filters "Name=association.subnet-id,Values=$sid" \
      --query 'RouteTables[].Routes[].{g:GatewayId,n:NatGatewayId}' \
      --output text 2>/dev/null)
    if [[ -z "$routes" || "$routes" == "None" ]]; then
      routes=$(aws ec2 describe-route-tables "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
        --region "$AWS_REGION" \
        --filters "Name=vpc-id,Values=$TG_VPC_ID" \
                  "Name=association.main,Values=true" \
        --query 'RouteTables[].Routes[].{g:GatewayId,n:NatGatewayId}' \
        --output text 2>/dev/null)
    fi
    if echo "$routes" | grep -qiE 'nat-'; then
      n_nat=$((n_nat + 1))
    elif echo "$routes" | grep -qiE 'igw-'; then
      n_public=$((n_public + 1))
    else
      n_none=$((n_none + 1))
    fi
  done
  local n_az
  n_az=$(echo "$az_list" | tr ' ' '\n' | sort -u | grep -c .)
  [[ "$n_az" -ge 2 ]] || fail "TG_SUBNET_IDS span only $n_az AZ(s); \
need ≥2 (RDS + ALB floor, #480)."

  local total=${#_subs[@]}
  # #779: decide AssignPublicIp + verify the task can actually reach
  # Secrets Manager / ECR / Logs. Don't mix public + private subnets
  # (one AssignPublicIp value applies to all task ENIs).
  if [[ "$n_public" -eq "$total" ]]; then
    export TG_ASSIGN_TASK_PUBLIC_IP=ENABLED
    ok "BYO VPC $TG_VPC_ID: $total public subnets across $n_az AZs \
→ tasks get a public IP (AssignPublicIp=ENABLED) for SM/ECR/Logs egress"
  elif [[ "$n_nat" -eq "$total" ]]; then
    export TG_ASSIGN_TASK_PUBLIC_IP=DISABLED
    ok "BYO VPC $TG_VPC_ID: $total NAT-routed private subnets across \
$n_az AZs → AssignPublicIp=DISABLED (egress via NAT)"
  elif [[ "$n_none" -eq "$total" ]]; then
    # Private, no NAT — only viable with the interface endpoints.
    local n_ep
    n_ep=$(_byo_required_endpoints)
    if [[ "$n_ep" =~ ^[0-9]+$ && "$n_ep" -ge 4 ]]; then
      export TG_ASSIGN_TASK_PUBLIC_IP=DISABLED
      ok "BYO VPC $TG_VPC_ID: $total private subnets + SM/ECR/Logs \
interface endpoints → AssignPublicIp=DISABLED (egress via endpoints)"
    else
      fail "BYO subnets are private with no NAT and the VPC lacks the \
required interface endpoints (need secretsmanager + ecr.api + ecr.dkr \
+ logs in $AWS_REGION; found ${n_ep:-0}/4). The ECS task can't reach \
Secrets Manager/ECR/Logs and won't start. Use subnets with a NAT, add \
those 4 VPC endpoints, or pick public subnets — see #779."
    fi
  else
    fail "BYO subnets mix egress types (public IGW: $n_public, NAT: \
$n_nat, neither: $n_none). A task's AssignPublicIp is one value for \
all subnets — choose a consistent set (all public, or all \
NAT/endpoint private). See #779."
  fi
}

if [[ -n "${TG_VPC_ID:-}" ]]; then
  # Existing-VPC path: tg creates no VPC/IGW, so the VPC/IGW quota
  # checks don't apply — verify the supplied subnets' egress + set
  # TG_ASSIGN_TASK_PUBLIC_IP for the *Byo task definitions (#779).
  _byo_egress_preflight
else
  # create-new path: tasks run in tg's private subnets behind the NAT.
  export TG_ASSIGN_TASK_PUBLIC_IP=DISABLED
  _preflight_limit "VPC quota" describe-vpcs "Vpcs" L-F678F1CE
  _preflight_limit "Internet-gateway quota" \
    describe-internet-gateways "InternetGateways" L-A4707A72
fi

# #967 (#963 fix #2): an in-use DBSubnetGroup subnet swap is rejected
# by RDS ("subnets to be deleted are currently in use") — RDS can't
# re-point a subnet group while the DB is live. On a BYO re-run that
# supplies a DIFFERENT subnet set than the deployed stack's
# DBSubnetGroup, the update would fail mid-deploy. Detect + REFUSE
# up-front with actionable guidance (detect + refuse is the
# less-destructive option) instead of attempting the swap. Only the
# BYO path can change the set via TG_SUBNET_IDS; the create-new set is
# tg's own fixed private subnets. #962's upgrade-aware defaults lock
# the subnets forward, so in practice this is the belt-and-suspenders
# backstop for an operator overriding TG_SUBNET_IDS on a re-run.
_dbsubnetgroup_inuse_guard() {
  [[ -n "${TG_SUBNET_IDS:-}" ]] || return 0   # create-new / no override
  # Only meaningful when a stack (hence an RDS + DBSubnetGroup) already
  # exists. The DBSubnetGroup physical id is a stack resource.
  local grp
  grp=$(aws cloudformation describe-stack-resources "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" --stack-name tg-container-stack \
    --logical-resource-id DBSubnetGroup \
    --query 'StackResources[0].PhysicalResourceId' \
    --output text 2>/dev/null) || return 0
  [[ -n "$grp" && "$grp" != "None" ]] || return 0
  local deployed requested
  deployed=$(aws rds describe-db-subnet-groups "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" --db-subnet-group-name "$grp" \
    --query 'DBSubnetGroups[0].Subnets[].SubnetIdentifier' \
    --output text 2>/dev/null) || return 0
  [[ -n "$deployed" && "$deployed" != "None" ]] || return 0
  # Compare as sorted sets (order/whitespace-independent). Split on
  # commas + whitespace, drop blanks, sort-unique, re-join.
  requested=$(printf '%s' "$TG_SUBNET_IDS" | tr ', \t' '\n\n\n\n' \
    | sed '/^$/d' | sort -u | paste -sd, -)
  local dep_sorted
  dep_sorted=$(printf '%s' "$deployed" | tr ', \t' '\n\n\n\n' \
    | sed '/^$/d' | sort -u | paste -sd, -)
  if [[ "$requested" != "$dep_sorted" ]]; then
    fail "The deployed RDS DBSubnetGroup ($grp) uses subnets \
[$dep_sorted], but this run requests [$requested]. RDS can't move a \
subnet group while the database is live — the stack update would fail \
with 'subnets to be deleted are currently in use'. Keep the original \
subnets (re-run with TG_SUBNET_IDS=$dep_sorted), or destroy + \
reinstall if you must change the DB's subnets. (#963/#967)"
  fi
}
_dbsubnetgroup_inuse_guard

# #982: orphaned-tg-resource recovery. A prior FAILED install (or a
# pre-#971 install, when the ALB/target-group carried the fixed names
# tg-alb / tg-api-tg) can leave a physical resource that CFN can't
# adopt on a re-run → AlreadyExists, rollback, stuck. #971 stops the
# stack from CREATING a colliding name going forward, but cannot delete
# or import the PRE-EXISTING orphan — so the re-run needs a recovery
# path. Detect a physical tg-alb / tg-api-tg / tg-alb-sg that is NOT a
# resource of the current tg-container-stack, recommend an action
# (DELETE when the stack is rolled-back/failed and the orphan serves no
# healthy app; RETRY/abort when it looks live), and let the operator
# choose. Owner decision (2026-06-12): offer the choice with a
# recommendation — never auto-delete, never just abort.
#
# TG_ON_ORPHAN = delete | retry | abort. Default (unset) = abort, so an
# unattended run NEVER deletes without an explicit opt-in.

# echoes the physical-ids the current stack legitimately OWNS, so an
# orphan check never flags the stack's own resource.
_stack_owned_physical_ids() {
  aws cloudformation describe-stack-resources "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" --stack-name tg-container-stack \
    --query 'StackResources[].PhysicalResourceId' \
    --output text 2>/dev/null || true
}

# echoes "true" if the named ALB has >=1 healthy target in any of its
# target groups (→ likely serving a live app; do NOT recommend delete).
_alb_has_healthy_targets() {
  local alb_arn="$1" tg_arns tg_arn
  tg_arns=$(aws elbv2 describe-target-groups "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" --load-balancer-arn "$alb_arn" \
    --query 'TargetGroups[].TargetGroupArn' --output text 2>/dev/null) \
    || { echo false; return; }
  for tg_arn in $tg_arns; do
    local n
    n=$(aws elbv2 describe-target-health "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
      --region "$AWS_REGION" --target-group-arn "$tg_arn" \
      --query "length(TargetHealthDescriptions[?\
TargetHealth.State=='healthy'])" --output text 2>/dev/null)
    [[ "$n" =~ ^[0-9]+$ && "$n" -gt 0 ]] && { echo true; return; }
  done
  echo false
}

_orphan_recovery_preflight() {
  local owned stack_status orphan_alb_arn orphan_alb_name=""
  owned=$(_stack_owned_physical_ids)
  # Legacy fixed-name ALB (pre-#971). Find an ALB literally named
  # tg-alb; post-#971 stacks auto-name, so a `tg-alb` is by definition
  # not this stack's — but cross-check the owned set anyway (an old
  # stack that still owns a tg-alb is NOT an orphan).
  orphan_alb_arn=$(aws elbv2 describe-load-balancers "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" --names tg-alb \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null) \
    || orphan_alb_arn=""
  [[ "$orphan_alb_arn" == "None" ]] && orphan_alb_arn=""
  # Owned by the current stack? then it's not an orphan.
  if [[ -n "$orphan_alb_arn" ]] \
     && grep -qF "$orphan_alb_arn" <<<"$owned"; then
    orphan_alb_arn=""
  fi
  [[ -n "$orphan_alb_arn" ]] && orphan_alb_name="tg-alb"

  # Nothing orphaned → nothing to do.
  [[ -n "$orphan_alb_arn" ]] || return 0

  stack_status=$(aws cloudformation describe-stacks "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" --stack-name tg-container-stack \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null) \
    || stack_status="(no stack)"

  # Recommend: delete a leftover from a rolled-back/failed stack that
  # serves no healthy app; otherwise (live targets) recommend abort.
  local healthy recommend
  healthy=$(_alb_has_healthy_targets "$orphan_alb_arn")
  if [[ "$healthy" == "true" ]]; then
    recommend="abort"
  elif [[ "$stack_status" == *ROLLBACK* || "$stack_status" == *FAILED* \
          || "$stack_status" == "(no stack)" ]]; then
    recommend="delete"
  else
    recommend="abort"
  fi

  warn "A leftover load balancer from a previous failed install is in the way:"
  printf '    • load balancer  %s  (%s)\n' \
    "$orphan_alb_name" "$orphan_alb_arn" >&2
  printf '  It blocks the install — CFN can'\''t adopt a resource it \
doesn'\''t track. Stack status: %s; orphan has healthy targets: %s.\n' \
    "$stack_status" "$healthy" >&2
  printf '  Recommended: %s\n' \
    "$([[ $recommend == delete ]] \
        && echo 'DELETE (leftover; no live app served)' \
        || echo 'ABORT/RETRY (it may be serving a live app — investigate)')" >&2

  # Resolve the choice: TG_ON_ORPHAN wins; else interactive [d/r/a];
  # else (non-TTY, unset) default to abort — never delete unattended.
  local choice="${TG_ON_ORPHAN:-}"
  if [[ -z "$choice" ]]; then
    if [[ -t 0 ]]; then
      printf '    [d] delete the orphan(s) and continue%s\n' \
        "$([[ $recommend == delete ]] && echo '   (recommended)')" >&2
      printf '    [r] retry the install as-is (will re-fail unless cleared)\n' >&2
      printf '    [a] abort — leave everything, handle it manually%s\n' \
        "$([[ $recommend == abort ]] && echo '   (recommended)')" >&2
      local _ans=""
      read -rp "  Choice [d/r/a]: " _ans || true   # EOF → empty → abort
      case "$_ans" in
        d|D) choice=delete ;;
        r|R) choice=retry ;;
        *)   choice=abort ;;
      esac
    else
      choice=abort
    fi
  fi

  case "$choice" in
    delete)
      step "Removing the leftover load balancer $orphan_alb_name"
      # Listeners cascade on ALB delete; delete the ALB, then its
      # now-free target groups, then the orphaned SG. Best-effort on
      # the dependents (they may not exist / already be freed).
      aws elbv2 delete-load-balancer "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
        --region "$AWS_REGION" --load-balancer-arn "$orphan_alb_arn" \
        || fail "could not delete orphaned ALB $orphan_alb_arn — \
delete it manually (elbv2 delete-load-balancer) and re-run."
      aws elbv2 wait load-balancers-deleted "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
        --region "$AWS_REGION" --load-balancer-arns "$orphan_alb_arn" \
        2>/dev/null || true
      # Legacy fixed-name target group, if it lingers.
      local tg_arn
      tg_arn=$(aws elbv2 describe-target-groups "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
        --region "$AWS_REGION" --names tg-api-tg \
        --query 'TargetGroups[0].TargetGroupArn' --output text \
        2>/dev/null) || tg_arn=""
      if [[ -n "$tg_arn" && "$tg_arn" != "None" ]]; then
        aws elbv2 delete-target-group "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
          --region "$AWS_REGION" --target-group-arn "$tg_arn" \
          2>/dev/null || warn "orphaned target group tg-api-tg \
($tg_arn) not deleted — remove it manually if the deploy re-collides."
      fi
      ok "Orphan(s) cleared — continuing the install."
      ;;
    retry)
      warn "Retrying as-is — the deploy will re-fail unless the orphan \
was cleared elsewhere. (You chose retry.)"
      ;;
    *)
      fail "Aborting per your choice (or TG_ON_ORPHAN=abort / non-TTY \
default). Clear the orphan(s) manually, then re-run:
  aws elbv2 delete-load-balancer --region $AWS_REGION \
--load-balancer-arn $orphan_alb_arn
  # then its target group tg-api-tg + SG tg-alb-sg if they linger.
Or re-run with TG_ON_ORPHAN=delete to have the installer clear them."
      ;;
  esac
}
_orphan_recovery_preflight

# ── 3. Resolve trust principal for tg-bedrock-role ──
step "Configuring who can use Bedrock"

# What CFN trusts as the installer/admin principal on tg-consumer.
# Two outputs feed the deploy:
#   TRUST_PRINCIPAL    → TrustedIamPrincipals (Principal: AWS, exact ARN)
#   TRUST_SSO_ARNLIKE  → TrustedSsoPrincipalArnLike (ArnLike on
#                        aws:PrincipalArn, for IDC/permission-set roles)
# Exactly one is populated per caller class; the other stays empty so
# the matching CFN Condition stays off.
TRUST_PRINCIPAL=""
TRUST_SSO_ARNLIKE=""

# Trust derivation is single-path (auto-derive from the installer's
# own caller). The former env-var escape hatch for naming trusted
# principal(s) verbatim was removed: it was undocumented, never
# exercised by a behavioral test, and its only real use — "trust a
# principal other than the caller" — is reachable directly via the
# tg-bedrock-role CFN param TrustedSsoPrincipalArnLike on
# `aws cloudformation deploy`. Note also that the CFN template now adds
# the name-agnostic AWSReservedSSO_* SSO trust UNCONDITIONALLY (every
# deploy carries it, regardless of caller type), so the SSO ArnLike
# below is the installer telling CFN the same value its default would
# compute — not the only source of SSO trust.
if [[ "$CALLER_ARN" == *":assumed-role/AWSReservedSSO_"* ]]; then
  # IDC / SSO permission-set caller. The bare role/ROLE sed produces a
  # MALFORMED ARN (the real SSO role lives under the
  # /aws-reserved/sso.amazonaws.com/ path), which CFN rejects with
  # "Invalid principal in policy". Emit the valid PATH-FORM ARN
  # referenced with an ArnLike condition.
  #
  # Name-agnostic: trust EVERY permission set in the account
  # (AWSReservedSSO_*), not just the caller's own permset. We can't
  # know a customer's permission-set name(s) at build time, and tg is
  # principal-agnostic by design — it governs whatever principal shows
  # up in CUR. Same-account scope is enforced separately by the
  # Principal: AWS = <acct>:root clause, so the wildcard can't be
  # assumed cross-account. The trailing _* also absorbs the IDC
  # re-provisioning suffix, so trust survives permission-set churn.
  #
  # Account comes from $CALLER_ACCT (the account the SSO session
  # actually originates in), not $TG_TARGET_ACCOUNT_ID — the line-639
  # preflight guarantees they're equal, but CALLER_ACCT is the
  # semantically-correct source for where the install runs.
  TRUST_SSO_ARNLIKE=\
"arn:aws:iam::${CALLER_ACCT}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_*"
  ok "Access configured"
elif [[ "$CALLER_ARN" == *":assumed-role/"* ]]; then
  # Plain assumed-role caller: convert to the underlying role ARN so
  # the api task can sts:AssumeRole tg-consumer for tests later.
  #   arn:aws:sts::ACCT:assumed-role/ROLE/SESSION
  #   → arn:aws:iam::ACCT:role/ROLE
  ROLE_NAME=$(printf '%s' "$CALLER_ARN" \
    | sed -E \
    's#.*:assumed-role/([^/]+)/.*#\1#')
  TRUST_PRINCIPAL=\
"arn:aws:iam::${TG_TARGET_ACCOUNT_ID}:role/${ROLE_NAME}"
  ok "Access configured"
else
  TRUST_PRINCIPAL="$CALLER_ARN"
  ok "Access configured"
fi

# ── 4. (removed) Bedrock invocation logging — #725 (#720 slice 3)
# tg no longer enables Bedrock invocation logging or deploys
# tg-bedrock-logging: CUR 2.0 (tg-cur-athena) is the sole spend +
# discovery source. The stack/template/teardown are deleted.

# ── 5. Deploy tg-bedrock-role ────────────────────────
step "Setting up the Bedrock access role"

ROLE_TPL="cfn/tg-bedrock-role.yaml"
[[ -f "$ROLE_TPL" ]] || \
  fail "template not found: $ROLE_TPL"

aws cloudformation deploy \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
  --region "$AWS_REGION" \
  --stack-name tg-bedrock-role \
  --template-file "$ROLE_TPL" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "TargetAccountId=$TG_TARGET_ACCOUNT_ID" \
    "TrustedIamPrincipals=$TRUST_PRINCIPAL" \
    "TrustedSsoPrincipalArnLike=$TRUST_SSO_ARNLIKE"

ROLE_ARN=$(aws cloudformation describe-stacks \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
  --region "$AWS_REGION" \
  --stack-name tg-bedrock-role \
  --query \
  "Stacks[0].Outputs[?OutputKey=='RoleArn']\
.OutputValue | [0]" \
  --output text)
ok "tg-consumer: $ROLE_ARN"

# ── 6. Deploy tg-container-stack ─────────────────────
step "Deploying the application"

# #408: cfn/tg-container-stack.yaml is past CFN's 51,200-byte
# inline limit (grew via #357/#346/#406), so `aws
# cloudformation deploy` must upload it to S3 via --s3-bucket.
# Ensure a dedicated per-account template bucket exists
# (idempotent — head-bucket first; create only if absent).
# us-east-1 create-bucket takes no LocationConstraint.
CFN_TEMPLATE_BUCKET="tg-cfn-templates-${TG_TARGET_ACCOUNT_ID}"
if ! aws s3api head-bucket \
     --bucket "$CFN_TEMPLATE_BUCKET" \
     "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" 2>/dev/null; then
  echo "  Creating template bucket $CFN_TEMPLATE_BUCKET"
  if [[ "$AWS_REGION" == "us-east-1" ]]; then
    aws s3api create-bucket \
      --bucket "$CFN_TEMPLATE_BUCKET" \
      --region "$AWS_REGION" \
      "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" >/dev/null
  else
    aws s3api create-bucket \
      --bucket "$CFN_TEMPLATE_BUCKET" \
      --region "$AWS_REGION" \
      --create-bucket-configuration \
        "LocationConstraint=$AWS_REGION" \
      "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" >/dev/null
  fi
fi
ok "CFN template bucket: $CFN_TEMPLATE_BUCKET"

# Pre-read tg-cur-athena outputs if the stack exists, so
# the api task gets ATHENA_* env vars baked in on first
# install. If the CUR stack isn't deployed yet,
# tg-cur-deploy.sh will patch the running container-stack
# afterward (#195).
TG_CUR_STACK="${TG_CUR_STACK:-tg-cur-athena}"
CUR_ATHENA_RESULTS=""
CUR_ATHENA_WG="tg-cur-analytics"
CUR_ATHENA_DB="tg_cur"
CUR_TABLE="data"
if aws cloudformation describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --stack-name "$TG_CUR_STACK" >/dev/null 2>&1; then
  cur_out() {
    aws cloudformation describe-stacks \
      "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
      --stack-name "$TG_CUR_STACK" \
      --query "Stacks[0].Outputs[?OutputKey=='$1']\
.OutputValue | [0]" --output text
  }
  ar=$(cur_out AthenaResultsBucketName)
  CUR_ATHENA_RESULTS="${ar:+s3://$ar/}"
  CUR_ATHENA_WG=$(cur_out AthenaWorkgroupName)
  CUR_ATHENA_DB=$(cur_out GlueDatabaseName)
  CUR_TABLE=$(cur_out CurTableName)
  ok "Pre-wired CUR/Athena env from $TG_CUR_STACK"
fi

# #590 (#566C): tg-ApiRunner is gone. The api queries CUR/Athena
# under its own task role (tg-app), which carries the query perms
# inline (tg-container-stack.yaml) — no second role to deploy or
# wire. The CUR/Athena ENV vars (AthenaResultsBucket, workgroup,
# db, table) are still pre-read above and passed to the container
# stack below; that's all the api needs now.

# #979: resolve the ECS task count so a RE-RUN never silently scales a
# live stack to zero. The two-pass dance below uses DesiredCount=0 on
# pass 1 ONLY because a brand-new ECS service created with desired>0
# hangs forever (no image in ECR yet). On a re-run the service already
# exists + has an image, so forcing 0 here would scale a running 1/1
# stack to 0/0 (every request 503s) — the #979 stage-down. Read the
# DEPLOYED count and:
#   * FIRST_PASS_DESIRED — 0 only for a genuinely fresh stack (no
#     service yet); an existing stack keeps its deployed count, so the
#     re-run never drops the live service to 0.
#   * DESIRED_COUNT (the pass-2 / final value) — an explicit operator
#     override (TG_DESIRED_COUNT) wins; else the deployed count when
#     >0 (preserve what's running); else 1 (fresh default). Clamp +
#     warn if it would otherwise resolve to 0 on a stack that was
#     running >0 without an explicit opt-out.
DEPLOYED_DESIRED=$(aws cloudformation describe-stacks \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
  --stack-name tg-container-stack \
  --query "Stacks[0].Parameters[?ParameterKey=='DesiredCount']\
.ParameterValue | [0]" --output text 2>/dev/null) || DEPLOYED_DESIRED=""
[[ "$DEPLOYED_DESIRED" == "None" ]] && DEPLOYED_DESIRED=""

if [[ -n "${TG_DESIRED_COUNT:-}" ]]; then
  DESIRED_COUNT="$TG_DESIRED_COUNT"          # explicit operator override
elif [[ "$DEPLOYED_DESIRED" =~ ^[0-9]+$ && "$DEPLOYED_DESIRED" -gt 0 ]]; then
  DESIRED_COUNT="$DEPLOYED_DESIRED"          # preserve the running count
else
  DESIRED_COUNT=2                            # fresh-install default:
  # 2 tasks so a single unhealthy task or a rolling deploy always
  # leaves >=1 healthy target (DesiredCount=1 + an unhealthy-swap left
  # zero healthy targets for ~40-60s → the 502). The operator override
  # and the preserve-running-count branch above are unchanged, so an
  # existing install keeps its count and an operator can still force one.
fi
# Guard: a re-run must not zero a live service unless the operator
# explicitly asked for 0 via TG_DESIRED_COUNT.
if [[ "$DESIRED_COUNT" == "0" && "$DEPLOYED_DESIRED" =~ ^[0-9]+$ \
      && "$DEPLOYED_DESIRED" -gt 0 && -z "${TG_DESIRED_COUNT:-}" ]]; then
  warn "Resolved DesiredCount=0 on a stack running \
$DEPLOYED_DESIRED — clamping to $DEPLOYED_DESIRED so the re-run \
doesn't scale the app to zero. Set TG_DESIRED_COUNT=0 to override."
  DESIRED_COUNT="$DEPLOYED_DESIRED"
fi
# First pass: 0 ONLY for a fresh stack (no deployed service); an
# existing stack keeps its target count through both passes.
if [[ -z "$DEPLOYED_DESIRED" ]]; then
  FIRST_PASS_DESIRED=0
else
  FIRST_PASS_DESIRED="$DESIRED_COUNT"
fi

# First-pass deploy with DesiredCount=$FIRST_PASS_DESIRED — 0 on a
# fresh install (the ECR repo has no image yet; an ECS Service created
# with desired>0 hangs waiting for a task), the deployed count on a
# re-run (never scale a live service to 0). Pass 2 (step 10) sets the
# final DESIRED_COUNT after the image is pushed.
aws cloudformation deploy \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
  --region "$AWS_REGION" \
  --stack-name tg-container-stack \
  --template-file cfn/tg-container-stack.yaml \
  --s3-bucket "$CFN_TEMPLATE_BUCKET" \
  --s3-prefix tg-container-stack \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "BootstrapAdminEmail=$TG_BOOTSTRAP_ADMIN_EMAIL" \
    "InvocationLogRegions=${TG_INVLOGS_REGIONS:-}" \
    "Environment=$TG_ENVIRONMENT" \
    "TgVersion=$TG_VERSION" \
    "EnableTestAuthTrust=$TG_TEST_TRUST_CFN" \
    "AlbScheme=$TG_ALB_SCHEME" \
    "ExistingVpcId=${TG_VPC_ID:-}" \
    "ExistingSubnetIds=${TG_SUBNET_IDS:-}" \
    "AssignTaskPublicIp=${TG_ASSIGN_TASK_PUBLIC_IP:-DISABLED}" \
    "AllowedIngressCidr1=$TG_CIDR_1" \
    "AllowedIngressCidr2=$TG_CIDR_2" \
    "AllowedIngressCidr3=$TG_CIDR_3" \
    "AllowedIngressCidr4=$TG_CIDR_4" \
    "RequireLogin=$( \
      [[ $TG_AUTH_REQUIRE_LOGIN == 1 ]] \
        && echo true || echo false)" \
    "OidcIssuer=${TG_OIDC_ISSUER:-}" \
    "OidcClientId=${TG_OIDC_CLIENT_ID:-}" \
    "OidcRedirectUri=${TG_OIDC_REDIRECT_URI:-}" \
    "AthenaResultsBucket=${CUR_ATHENA_RESULTS}" \
    "AthenaWorkgroup=${CUR_ATHENA_WG}" \
    "AthenaDatabase=${CUR_ATHENA_DB}" \
    "CurTableName=${CUR_TABLE}" \
    "ReusedCurBucketArn=${TG_REUSED_CUR_BUCKET_ARN:-}" \
    "DataProtection=${TG_DATA_PROTECTION}" \
    "LogRetentionDays=${TG_LOG_RETENTION_DAYS}" \
    "LogLevel=${TG_LOG_LEVEL}" \
    "CertificateArn=${TG_CERT_ARN:-}" \
    "DomainName=${TG_DOMAIN_NAME:-}" \
    "HostedZoneId=${TG_HOSTED_ZONE_ID:-}" \
    "IssueAcmCert=$( \
      [[ $TG_ISSUE_ACM_CERT == 1 ]] \
        && echo true || echo false)" \
    "AllowPlaintextAlb=${TG_ALLOW_PLAINTEXT_CFN}" \
    "DesiredCount=$FIRST_PASS_DESIRED"
# #963: `deploy` can exit 0 on a rolled-back change set — assert the
# real terminal status so a half-deployed core stack aborts loudly
# here, not silently as "Install complete" with CUR quietly missing.
assert_stack_succeeded tg-container-stack
ok "Application deployed (starting up)"

# ── 7. Read stack outputs ────────────────────────────
step "Finishing setup"

read_output() {
  local key="$1"
  aws cloudformation describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    --stack-name tg-container-stack \
    --query \
    "Stacks[0].Outputs[?OutputKey=='${key}']\
.OutputValue | [0]" \
    --output text
}

ECR_URI=$(read_output EcrRepositoryUri)
CLUSTER=$(read_output EcsClusterName)
API_SVC=$(read_output EcsApiServiceName)
WRK_SVC=$(read_output EcsWorkerServiceName)

[[ -n "$ECR_URI" && "$ECR_URI" != "None" ]] || \
  fail "EcrRepositoryUri output missing"

# #497: ALB is the only endpoint — always read its DNS.
ALB_DNS=$(read_output AlbDnsName)
[[ -n "$ALB_DNS" && "$ALB_DNS" != "None" ]] || \
  fail "AlbDnsName output missing"

# The public origin the browser hits: a custom domain when set,
# else the raw ALB DNS. HTTPS unless the explicit plaintext
# opt-in is on. Used for the Cognito callback + redirect URI.
if [[ -n "$TG_DOMAIN_NAME" ]]; then
  PUBLIC_HOST="$TG_DOMAIN_NAME"
else
  PUBLIC_HOST="$ALB_DNS"
fi
if [[ "$TG_ALLOW_PLAINTEXT_ALB" == "1" ]]; then
  PUBLIC_ORIGIN="http://${PUBLIC_HOST}"
else
  PUBLIC_ORIGIN="https://${PUBLIC_HOST}"
fi

# ── 7a. Cognito pool (the default always-on login) ───
# #782: Cognito is the BASE admin login (the bootstrap-admin
# path). The ECS installer had no Cognito path at all — it
# only knew the bring-your-own-Okta env vars — so a real ECS
# install couldn't stand up a login without an Okta tenant.
# Mirror tg-local-install.sh §5b, but here the CallbackUrl
# needs the ALB DNS, which only exists after the pass-1 deploy
# above — so the pool is deployed HERE, then its OIDC outputs
# feed the pass-2 (scale-to-1) deploy below. Skipped when the
# operator brought their own OIDC issuer (provider=okta).
COGNITO_POOL_ID=""
COGNITO_POOL_ARN=""
if [[ "$TG_AUTH_PROVIDER" == "cognito" ]]; then
  step "Setting up admin sign-in"
  COGNITO_TPL="cfn/tg-cognito-pool.yaml"
  [[ -f "$COGNITO_TPL" ]] || fail "$COGNITO_TPL not found"
  COGNITO_CALLBACK="${PUBLIC_ORIGIN}/auth/callback"
  aws cloudformation deploy \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    --stack-name tg-cognito-pool \
    --template-file "$COGNITO_TPL" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --parameter-overrides \
      "CallbackUrl=$COGNITO_CALLBACK"
  cog_out() {
    aws cloudformation describe-stacks \
      "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
      --stack-name tg-cognito-pool \
      --query "Stacks[0].Outputs[?OutputKey=='$1']\
.OutputValue | [0]" --output text 2>/dev/null
  }
  COGNITO_POOL_ID=$(cog_out UserPoolId)
  [[ -n "$COGNITO_POOL_ID" && "$COGNITO_POOL_ID" != "None" ]] \
    || fail "tg-cognito-pool deployed but UserPoolId output \
missing"
  COGNITO_POOL_ARN="arn:aws:cognito-idp:${AWS_REGION}:\
${TG_TARGET_ACCOUNT_ID}:userpool/${COGNITO_POOL_ID}"
  # Derive the OIDC trio from the pool — these feed pass-2.
  # Don't clobber an explicitly-supplied value (lets an
  # operator override, e.g. a custom-domain redirect).
  TG_OIDC_ISSUER="${TG_OIDC_ISSUER:-$(cog_out OidcIssuer)}"
  TG_OIDC_CLIENT_ID="${TG_OIDC_CLIENT_ID:-$(cog_out OidcClientId)}"
  TG_OIDC_REDIRECT_URI="${TG_OIDC_REDIRECT_URI:-$COGNITO_CALLBACK}"
  # Cognito doesn't expose the app-client secret as a CFN output
  # (security), so read it live; it lands in Secrets Manager in
  # step 7b just like the Okta secret would.
  if [[ -z "${TG_OIDC_CLIENT_SECRET:-}" ]]; then
    TG_OIDC_CLIENT_SECRET=$(aws cognito-idp \
      describe-user-pool-client \
      "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
      --user-pool-id "$COGNITO_POOL_ID" \
      --client-id "$TG_OIDC_CLIENT_ID" \
      --query 'UserPoolClient.ClientSecret' \
      --output text 2>/dev/null)
    [[ -n "$TG_OIDC_CLIENT_SECRET" \
       && "$TG_OIDC_CLIENT_SECRET" != "None" ]] \
      || fail "could not read Cognito app-client secret"
  fi
  ok "Cognito pool : $COGNITO_POOL_ID"
  ok "Cognito issuer: $TG_OIDC_ISSUER"
  ok "Callback URL  : $COGNITO_CALLBACK"

  # #921: put the bootstrap admin in CONFIRMED (permanent password),
  # not FORCE_CHANGE_PASSWORD. This is what makes forgot-password
  # work — Cognito ForgotPassword requires CONFIRMED, and the
  # temp-password invite both may never arrive (COGNITO_DEFAULT
  # sender spam-filtered) AND leaves the user FORCE_CHANGE_PASSWORD,
  # locking the operator out both ways (demo2). The operator chooses:
  # Option B (TG_BOOTSTRAP_ADMIN_PASSWORD or a TTY prompt) logs in
  # directly; Option A (default/headless) sets a random throwaway
  # that is DISCARDED — sign in via Forgot password. Both end
  # CONFIRMED. (Supersedes #915's temp-password print, which kept
  # FORCE_CHANGE_PASSWORD.) The secret is never logged/printed/
  # stored. Idempotent: an already-CONFIRMED admin is left untouched.
  #
  # #937: create the bootstrap admin here (idempotent), not via a
  # create-only CFN resource in tg-cognito-pool — that resource failed
  # `User already exists` on every callback-URL reconcile update where
  # the pool outlived a recreated container stack (the #935 self-heal).
  # An empty bootstrap email (allowed on non-prod) means no admin to
  # create in Cognito — the env relies on the test-trust bypass. Skip
  # both calls; they'd fail on an empty Username.
  if [[ -n "$TG_BOOTSTRAP_ADMIN_EMAIL" ]]; then
    tg_ensure_bootstrap_admin_user \
      "$COGNITO_POOL_ID" "$TG_BOOTSTRAP_ADMIN_EMAIL" "$AWS_REGION"
    tg_set_bootstrap_admin_password \
      "$COGNITO_POOL_ID" "$TG_BOOTSTRAP_ADMIN_EMAIL" "$AWS_REGION"
  else
    ok "Bootstrap admin empty (Environment=$TG_ENVIRONMENT) — \
no Cognito admin seeded; test-trust + real login both available."
  fi
fi

# ── 7b. OIDC client secret → Secrets Manager (#432) ──
# The OIDC secret is no longer a CFN parameter — pass-1
# created an empty/placeholder SM secret
# (OidcClientSecretManaged). Write the real value here.
# When TG_OIDC_CLIENT_SECRET is empty on an UPGRADE, the
# secret already holds the live value (CFN never regenerates
# on update), so we leave it untouched — preserving it
# WITHOUT the old `UsePreviousValue` literal that took stage
# login down (#431). A first install with an empty secret was
# already rejected in the pre-flight checks above.
if [[ "$TG_AUTH_REQUIRE_LOGIN" == "1" ]]; then
  OIDC_SECRET_ARN=$(read_output OidcClientSecretArn)
  [[ -n "$OIDC_SECRET_ARN" && "$OIDC_SECRET_ARN" != "None" ]] || \
    fail "OidcClientSecretArn output missing \
(RequireLogin=true)"
  if [[ -n "${TG_OIDC_CLIENT_SECRET:-}" ]]; then
    aws secretsmanager put-secret-value \
      "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
      --region "$AWS_REGION" \
      --secret-id "$OIDC_SECRET_ARN" \
      --secret-string "$TG_OIDC_CLIENT_SECRET" \
      >/dev/null \
      || fail "Failed writing OIDC secret to \
Secrets Manager"
    ok "OIDC client secret written to Secrets Manager"
  else
    ok "TG_OIDC_CLIENT_SECRET empty — keeping the \
value already in Secrets Manager (upgrade)"
  fi
fi

ok "ECR repo : $ECR_URI"
ok "ALB DNS  : $ALB_DNS"
ok "Cluster  : $CLUSTER"
ok "Api svc  : $API_SVC"
ok "Worker   : $WRK_SVC"

# ── 8. Build container image ─────────────────────────
# #877: TG_ECS_IMAGE_URI lets the operator deploy a PREBUILT image
# (e.g. the public.ecr.aws/e9y1g4o2/tg-container:<version> the
# publish pipeline maintains) and skip the local build entirely —
# no Docker required. When set, we pin the deploy to that exact URI
# and bypass the whole build+push+skip-detection block below. When
# unset (the default), the build path runs unchanged.
if [[ -n "${TG_ECS_IMAGE_URI:-}" ]]; then
  step "Using prebuilt image (skipping build)"
  ECS_IMAGE_URI="$TG_ECS_IMAGE_URI"
  ok "Deploy image: $ECS_IMAGE_URI (no local build; TG_ECS_IMAGE_URI)"
else
step "Building container image"

[[ -f container/Dockerfile ]] || \
  fail "container/Dockerfile not found"

# #538/#791: TG_VERSION is computed earlier (right after
# TG_ENVIRONMENT) so both container-stack deploys can stamp it as
# the TgVersion task-def env. Still passed as the image build-arg
# below so the baked default is also real.
ECR_REPO_NAME="${ECR_URI##*/}"
ECR_REGISTRY="${ECR_URI%/*}"

# #553: skip build+push+roll when the container image is
# unchanged. A CFN/param-only redeploy (CIDR bump, branch rename,
# env tweak) otherwise rebuilds a byte-identical image and pays a
# full ECS rolling replacement for nothing (~3-5m wasted). The
# image identity is a git tree-hash of everything the build bakes
# in: container/, admin-ui/web/ (the UI stage, #479), and the
# Dockerfile. We tag the pushed image `src-<hash>` and, on the
# next run, skip the build if that tag already exists in ECR.
# TG_FORCE_REBUILD=1 forces the full path.
TG_FORCE_REBUILD="${TG_FORCE_REBUILD:-0}"

# The source-hash skip keys off HEAD:<tree>, so a host checkout that's
# BEHIND the intended deploy SHA hashes a stale (older) tree — which may
# already have a pushed image — and the skip path silently reuses it.
# The deploy then reports green with a correct-looking /api/version (from
# the CFN TgVersion param) while serving a stale SPA bundle. Assert the
# build context IS the intended
# deploy target before hashing, and fail LOUD on a behind-HEAD deploy —
# mirroring the dirty-build-context guard below (both refuse to skip when
# the build context isn't what the operator thinks). The target is an
# explicit TG_DEPLOY_SHA if the caller pins one, else origin/main.
# TG_FORCE_REBUILD=1 skips the assert (it rebuilds from the working tree
# regardless). A dirty context also skips it (the dirty guard already
# forces a rebuild, so no stale-skip risk). Non-fatal when git/remote
# can't be resolved (e.g. a tarball deploy) — the skip key still reflects
# HEAD, and we don't want to block a deploy on a transient fetch failure.
if [[ "$TG_FORCE_REBUILD" != "1" \
      && -z "$(git status --porcelain -- container admin-ui/web \
               2>/dev/null)" ]] \
   && git rev-parse --git-dir >/dev/null 2>&1; then
  DEPLOY_HEAD=$(git rev-parse HEAD 2>/dev/null || true)
  if [[ -n "${TG_DEPLOY_SHA:-}" ]]; then
    DEPLOY_TARGET=$(git rev-parse "$TG_DEPLOY_SHA" 2>/dev/null || true)
    TARGET_LABEL="TG_DEPLOY_SHA ($TG_DEPLOY_SHA)"
  else
    # Best-effort refresh so the comparison sees the latest main; a
    # fetch failure (offline / no remote) just leaves origin/main as-is.
    git fetch origin main --quiet 2>/dev/null || true
    DEPLOY_TARGET=$(git rev-parse origin/main 2>/dev/null || true)
    TARGET_LABEL="origin/main"
  fi
  if [[ -n "$DEPLOY_HEAD" && -n "$DEPLOY_TARGET" \
        && "$DEPLOY_HEAD" != "$DEPLOY_TARGET" ]] \
     && git merge-base --is-ancestor "$DEPLOY_HEAD" "$DEPLOY_TARGET" \
        2>/dev/null; then
    # HEAD is strictly behind the target → the skip key would hash a
    # stale tree and could reuse a stale image. Abort loud.
    BEHIND_N=$(git rev-list --count \
      "${DEPLOY_HEAD}..${DEPLOY_TARGET}" 2>/dev/null || echo "?")
    fail "checkout is $BEHIND_N commit(s) behind $TARGET_LABEL \
(HEAD ${DEPLOY_HEAD:0:12}, target ${DEPLOY_TARGET:0:12}) — a deploy from \
a behind checkout can silently reuse a stale image. Fast-forward the \
checkout to the deploy target (or pass TG_DEPLOY_SHA=<sha> to pin it, \
or TG_FORCE_REBUILD=1 to rebuild from the working tree) and re-run."
  fi
fi

SRC_HASH=$(git rev-parse "HEAD:container" 2>/dev/null | cut -c1-12)
UI_HASH=$(git rev-parse "HEAD:admin-ui/web" 2>/dev/null | cut -c1-12)
# Combine the two tree hashes + the version into one tag-safe id.
# (Falls back to a timestamp-free constant only if git can't
# resolve the trees, which forces a rebuild — safe.)
if [[ -n "$SRC_HASH" && -n "$UI_HASH" ]]; then
  IMG_SRC_TAG="src-${SRC_HASH}-${UI_HASH}"
else
  IMG_SRC_TAG=""
fi
# A dirty build context defeats commit-tree hashing: `docker build`
# reads the working tree on disk, but HEAD:<tree> hashes only the
# COMMITTED tree, so uncommitted edits to container/ or
# admin-ui/web/ leave IMG_SRC_TAG unchanged → the skip path would
# deploy the prior image and silently discard the local edits.
# Treat any dirt in the build context as "changed" → never skip.
if [[ -n "$(git status --porcelain -- container admin-ui/web \
            2>/dev/null)" ]]; then
  [[ -n "$IMG_SRC_TAG" ]] && warn "dirty build context \
(container/ or admin-ui/web/) — forcing rebuild from working tree"
  IMG_SRC_TAG=""
fi

EXISTING_SRC_DIGEST=""
if [[ "$TG_FORCE_REBUILD" != "1" && -n "$IMG_SRC_TAG" ]]; then
  EXISTING_SRC_DIGEST=$(aws ecr describe-images \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --repository-name "$ECR_REPO_NAME" \
    --image-ids "imageTag=$IMG_SRC_TAG" \
    --query 'imageDetails[0].imageDigest' --output text 2>/dev/null \
    || true)
fi

if [[ -n "$EXISTING_SRC_DIGEST" && "$EXISTING_SRC_DIGEST" == sha256:* ]]; then
  # ── 8/9. SKIP build+push — image unchanged ──────────
  step "Image unchanged ($IMG_SRC_TAG) — skipping build/push"
  ok "Reusing pushed image $EXISTING_SRC_DIGEST (set \
TG_FORCE_REBUILD=1 to force a rebuild)"
  IMAGE_DIGEST="$EXISTING_SRC_DIGEST"
  ECS_IMAGE_URI="${ECR_URI}@${IMAGE_DIGEST}"
  # CFN still runs below (a real template/param change applies);
  # since EcsImageUri is the SAME digest, the task def is
  # unchanged → no needless service replacement.
else
  # ── 8. Build the container image ────────────────────
  if [[ "$TG_FORCE_REBUILD" == "1" ]]; then
    step "Building container image (TG_FORCE_REBUILD=1)"
  else
    step "Building container image (source changed)"
  fi
  # Multi-stage build — context is the REPO ROOT so the
  # Dockerfile's `ui` stage can build admin-ui/web/ from source
  # and bake it into the image. No pre-built container/static/
  # dependency, no node at runtime.
  #
  # --platform linux/amd64 is LOAD-BEARING: the task def carries no
  # runtimePlatform, so ECS Fargate defaults cpuArchitecture to
  # X86_64. A bare `docker build` produces an image for the build
  # host's NATIVE arch, so building on an Apple-Silicon (arm64) Mac
  # would yield an arm64 image that x86 Fargate can't exec — every
  # task dies with "exec format error" and crash-loops, wedging the
  # deploy and bricking the install. Pinning amd64 makes any build
  # host (arm64 Macs build it under Docker Desktop's bundled QEMU,
  # slower but correct) produce a Fargate-runnable image.
  # Cache-bust the SPA build stage on any admin-ui/web change. A stale
  # `ui`-stage layer has served an old bundle (a "feature missing"
  # that's a build-cache artifact, not a code bug); passing a hash of
  # the admin-ui/web tree as UI_SRC_HASH makes the Dockerfile's ui
  # stage invalidate whenever the source changes. Prefer the git tree
  # hash (fast, content-exact); fall back to a find-based digest when
  # not in a work tree.
  UI_SRC_HASH=$(git rev-parse "HEAD:admin-ui/web" 2>/dev/null \
    || (find admin-ui/web -type f -not -path '*/node_modules/*' \
          -not -path '*/dist/*' -print0 2>/dev/null \
        | sort -z | xargs -0 cat 2>/dev/null | shasum 2>/dev/null \
        | cut -d' ' -f1) \
    || echo none)
  docker build \
    --platform linux/amd64 \
    -t tg-container:latest \
    -f container/Dockerfile \
    --build-arg "TG_VERSION=$TG_VERSION" \
    --build-arg "UI_SRC_HASH=${UI_SRC_HASH:-none}" \
    .
  ok "docker build done (version $TG_VERSION, ui $UI_SRC_HASH)"

  # Fail LOUD on an arch mismatch — the bug this guards was a SILENT
  # arm64-on-x86 image that only surfaced as an ECS crash loop. Assert
  # the built image is amd64 here so a future --platform regression is
  # caught at build, not in production.
  built_arch="$(docker image inspect tg-container:latest \
    --format '{{.Architecture}}' 2>/dev/null)"
  if [[ "$built_arch" != "amd64" ]]; then
    fail "built image arch is '$built_arch', expected 'amd64' — \
ECS Fargate runs x86 (no runtimePlatform set), so a non-amd64 image \
crash-loops with 'exec format error'. Ensure 'docker build \
--platform linux/amd64' ran (arm64 hosts need Docker Desktop's QEMU \
emulation enabled)."
  fi
  ok "image arch verified amd64 (Fargate-runnable)"

  # ── 9. ECR login + push ────────────────────────────
  step "Pushing image to ECR"
  # Drop ONLY Docker's generic "credentials stored unencrypted"
  # notice — it's printed to stderr by `docker login` itself
  # (unrelated to tg) and reads as a scary security problem to a
  # first-time installer. The filter is surgical: a real auth failure
  # still surfaces on stderr (never blanket-2>/dev/null the login, or
  # a bad ECR push would proceed silently).
  aws ecr get-login-password \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    | docker login \
      --username AWS \
      --password-stdin \
      "$ECR_REGISTRY" >/dev/null \
      2> >(grep -v -E \
        'credentials are stored unencrypted|credential-store|credential helper' \
        >&2)
  ok "docker login → $ECR_REGISTRY"

  docker tag tg-container:latest "${ECR_URI}:latest"
  docker push "${ECR_URI}:latest"
  # #553: also push the src-hash tag so the NEXT run can detect
  # "unchanged" and skip. Both tags point at the same image.
  if [[ -n "$IMG_SRC_TAG" ]]; then
    docker tag tg-container:latest "${ECR_URI}:${IMG_SRC_TAG}"
    docker push "${ECR_URI}:${IMG_SRC_TAG}" >/dev/null \
      || warn "could not push $IMG_SRC_TAG tag (skip-detection \
degrades to always-rebuild next run)"
  fi
  ok "Pushed ${ECR_URI}:latest ($IMG_SRC_TAG)"

  # #531: deploy by IMMUTABLE DIGEST, not the :latest tag. Both
  # task defs default to ...:latest; if the installer re-pushes
  # :latest while a task is mid-placement, the tag moves to a new
  # digest and the already-scheduled task's pinned ref 404s
  # (CannotPullContainerError — the #475 worker rollback). Resolve
  # the digest we JUST pushed and pin both services to
  # <repo>@sha256:<digest> via EcsImageUri, so api + worker pull
  # the exact image and no tag-move race exists.
  IMAGE_DIGEST=$(aws ecr describe-images \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --repository-name "$ECR_REPO_NAME" \
    --image-ids imageTag=latest \
    --query 'imageDetails[0].imageDigest' --output text 2>/dev/null)
  if [[ -n "$IMAGE_DIGEST" && "$IMAGE_DIGEST" == sha256:* ]]; then
    ECS_IMAGE_URI="${ECR_URI}@${IMAGE_DIGEST}"
    ok "Pinned deploy image: $ECS_IMAGE_URI"
  else
    # Fall back to :latest if the digest can't be resolved — the
    # deploy still works, just without the race guard. Warn loudly.
    ECS_IMAGE_URI="${ECR_URI}:latest"
    warn "Could not resolve pushed image digest — falling back to \
:latest (re-exposes the #531 tag-move race; investigate ECR perms)"
  fi
fi   # end #553 image-unchanged skip guard
fi   # end #877 TG_ECS_IMAGE_URI prebuilt-image guard

# ── 10. Scale services to the target count (image pushed) ────
step "Scaling ECS services to $DESIRED_COUNT via CFN"

aws cloudformation deploy \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
  --region "$AWS_REGION" \
  --stack-name tg-container-stack \
  --template-file cfn/tg-container-stack.yaml \
  --s3-bucket "$CFN_TEMPLATE_BUCKET" \
  --s3-prefix tg-container-stack \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "BootstrapAdminEmail=$TG_BOOTSTRAP_ADMIN_EMAIL" \
    "InvocationLogRegions=${TG_INVLOGS_REGIONS:-}" \
    "Environment=$TG_ENVIRONMENT" \
    "TgVersion=$TG_VERSION" \
    "EnableTestAuthTrust=$TG_TEST_TRUST_CFN" \
    "AlbScheme=$TG_ALB_SCHEME" \
    "ExistingVpcId=${TG_VPC_ID:-}" \
    "ExistingSubnetIds=${TG_SUBNET_IDS:-}" \
    "AssignTaskPublicIp=${TG_ASSIGN_TASK_PUBLIC_IP:-DISABLED}" \
    "AllowedIngressCidr1=$TG_CIDR_1" \
    "AllowedIngressCidr2=$TG_CIDR_2" \
    "AllowedIngressCidr3=$TG_CIDR_3" \
    "AllowedIngressCidr4=$TG_CIDR_4" \
    "RequireLogin=$( \
      [[ $TG_AUTH_REQUIRE_LOGIN == 1 ]] \
        && echo true || echo false)" \
    "OidcIssuer=${TG_OIDC_ISSUER:-}" \
    "OidcClientId=${TG_OIDC_CLIENT_ID:-}" \
    "OidcRedirectUri=${TG_OIDC_REDIRECT_URI:-}" \
    "EnableCognitoAdminProvisioning=$( \
      [[ $TG_AUTH_PROVIDER == cognito ]] \
        && echo true || echo false)" \
    "CognitoUserPoolId=${COGNITO_POOL_ID:-}" \
    "CognitoUserPoolArn=${COGNITO_POOL_ARN:-}" \
    "CertificateArn=${TG_CERT_ARN:-}" \
    "DomainName=${TG_DOMAIN_NAME:-}" \
    "HostedZoneId=${TG_HOSTED_ZONE_ID:-}" \
    "IssueAcmCert=$( \
      [[ $TG_ISSUE_ACM_CERT == 1 ]] \
        && echo true || echo false)" \
    "AllowPlaintextAlb=${TG_ALLOW_PLAINTEXT_CFN}" \
    "EcsImageUri=${ECS_IMAGE_URI}" \
    "LogRetentionDays=${TG_LOG_RETENTION_DAYS}" \
    "LogLevel=${TG_LOG_LEVEL}" \
    "DesiredCount=$DESIRED_COUNT"
# #963: same rollback-honesty assertion on the scale-to-1 update.
assert_stack_succeeded tg-container-stack
ok "Services scaled to $DESIRED_COUNT"

# ── 11. Wait for api service steady state ───────────
step "Waiting for api service steady (max 10 min)"

DEADLINE=$(( $(date +%s) + 600 ))
while (( $(date +%s) < DEADLINE )); do
  SVC_JSON=$(aws ecs describe-services \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    --cluster "$CLUSTER" \
    --services "$API_SVC" \
    --output json) || \
    fail "describe-services failed"

  STATUS=$(printf '%s' "$SVC_JSON" \
    | python3 -c '
import sys, json
d = json.load(sys.stdin)
s = d["services"][0]
running = s.get("runningCount", 0)
desired = s.get("desiredCount", 0)
deps = len(s.get("deployments", []))
print(f"{running} {desired} {deps}")')

  RUN=$(echo "$STATUS" | awk '{print $1}')
  DES=$(echo "$STATUS" | awk '{print $2}')
  DEP=$(echo "$STATUS" | awk '{print $3}')

  if [[ "$RUN" == "$DES" && "$DEP" == "1" \
        && "$DES" != "0" ]]; then
    ok "api steady: running=$RUN desired=$DES \
deployments=$DEP"
    break
  fi
  printf '  …running=%s desired=%s deployments=%s\n' \
    "$RUN" "$DES" "$DEP"
  sleep 5   # #553: 15s→5s — shave rounding slop off the wait
done

if (( $(date +%s) >= DEADLINE )); then
  warn "api service did not reach steady state in 10m"
  warn "Check: aws ecs describe-services \
--cluster $CLUSTER --services $API_SVC"
  fail "ECS service never stabilized"
fi

# ── 12. Resolve api endpoint (ALB DNS or task IP) ───
step "Resolving api endpoint"

API_HOST=""
# TLS turns http→https and forces curl -k (self-signed
# is the dev/stage default; prod customers bring their
# own ACM cert so -k is harmless either way).
CURL_FLAGS="-fsS"
if [[ -n "$TG_CERT_ARN" || "$TG_ISSUE_ACM_CERT" == "1" ]]; then
  API_SCHEME="https"
  # ACM-issued certs verify cleanly, but self-signed (the
  # default dev/stage path) needs -k. Use -k uniformly here
  # to keep the smoke test mode-agnostic — production
  # customers will validate cert chains via their own
  # monitoring, not this one-shot installer step.
  CURL_FLAGS="-fkS"
else
  API_SCHEME="http"
fi
# #497: ALB is the only endpoint — DomainName alias if set,
# else the ALB DNS. (The retired EnableAlb=false path discovered
# a per-task public IP via the ENI; gone with the no-ALB path.)
if [[ -n "$TG_DOMAIN_NAME" ]]; then
  API_HOST="$TG_DOMAIN_NAME"
else
  API_HOST="${ALB_DNS}"
fi
API_URL="${API_SCHEME}://${API_HOST}/api/version"
ok "Endpoint: $API_URL (ALB)"

# ── 12b. Wait for /api/version 200 (max 5 min) ──────
step "Waiting for $API_URL (max 5 min)"

DEADLINE=$(( $(date +%s) + 300 ))
while (( $(date +%s) < DEADLINE )); do
  if curl $CURL_FLAGS "$API_URL" >/dev/null 2>&1; then
    ok "Healthy: $(curl $CURL_FLAGS "$API_URL")"
    break
  fi
  printf '  …polling %s\n' "$API_URL"
  sleep 5
done

if ! curl $CURL_FLAGS "$API_URL" >/dev/null 2>&1; then
  warn "Tail logs:"
  warn "  aws logs tail /ecs/tg-container --follow \
${PROFILE_HINT}--region $AWS_REGION"
  fail "Endpoint never returned 200 at $API_URL"
fi

# ── 13. Verify bootstrap admin ───────────────────────
step "Verifying bootstrap admin"

# The api auto-seeds TG_BOOTSTRAP_ADMIN_EMAIL on lifespan
# startup IF the admin_roles table is empty. The only
# unauthenticated way to read /api/roles here is the
# test-trust bypass — which is ON only for dev/stage and
# LOCKED OFF for prod (#570). So this check runs only when the
# bypass is enabled (Environment=dev/stage). On prod (the
# secure default) we skip it rather than fail: the auto-seed
# still ran; verify by logging in via the UI.
if [[ -z "$TG_BOOTSTRAP_ADMIN_EMAIL" ]]; then
  ok "No bootstrap admin set (Environment=$TG_ENVIRONMENT) — \
nothing to verify; this env runs header-authed UAT and a real \
SAML/Cognito login (no auto-login)."
elif [[ "$TG_TEST_TRUST_CFN" != "true" ]]; then
  ok "Skipping bypass-based admin check (test-trust off — \
Environment=$TG_ENVIRONMENT). Verify by logging in as \
$TG_BOOTSTRAP_ADMIN_EMAIL via the UI."
else
  ROLES_CURL=(curl -sS -o /tmp/tg-roles.out
    -w '%{http_code}'
    "${API_SCHEME}://${API_HOST}/api/roles"
    -H 'Authorization: AWS4-HMAC-SHA256 testbypass'
    -H "X-Tg-Test-Email: ${TG_BOOTSTRAP_ADMIN_EMAIL}")
  if [[ -n "$TG_CERT_ARN" ]]; then ROLES_CURL+=(-k); fi
  ROLES_HTTP=$("${ROLES_CURL[@]}") || \
    fail "curl GET /api/roles failed"

  if [[ "$ROLES_HTTP" == "200" ]] && \
     grep -q "$TG_BOOTSTRAP_ADMIN_EMAIL" /tmp/tg-roles.out; then
    ok "Bootstrap admin present: $TG_BOOTSTRAP_ADMIN_EMAIL"
  else
    warn "Response (HTTP $ROLES_HTTP):"
    cat /tmp/tg-roles.out >&2 || true
    warn "auto-seed may not have run yet — restart the \
api task or run 'aws ecs update-service \
--cluster $CLUSTER --service $API_SVC \
--force-new-deployment'"
  fi
fi

# ── 13b. Cognito CallbackUrl drift guard (#911) ──────
# An invite email is FROZEN with whatever console URL was
# current when Cognito sent it. A later tg-container-stack
# recreate mints a fresh tg-alb-<random> host, so an
# already-sent invite can point at a dead ALB (the #911
# stage incident: emailed tg-alb-898484032 after the live
# host became tg-alb-412289149). The pool's CallbackUrl is
# re-pointed to the live host on every install (§7a above),
# so this guard verifies that reconcile actually took — and,
# if a prior deploy left the pool stale, tells the operator
# exactly how to re-issue outstanding invites so the next
# email carries the correct host. Read-only; never fails the
# install (the deploy already succeeded).
if [[ "$TG_AUTH_PROVIDER" == "cognito" \
      && -n "${COGNITO_POOL_ID:-}" ]]; then
  step "Verifying Cognito sign-in URL matches the live host"
  # The pool client's first callback is what the invite's
  # ConsoleUrl is derived from (CallbackUrl minus the
  # /auth/callback suffix). Compare its host to the host we
  # just deployed against.
  LIVE_CALLBACK="${PUBLIC_ORIGIN}/auth/callback"
  POOL_CALLBACK=$(aws cognito-idp describe-user-pool-client \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --user-pool-id "$COGNITO_POOL_ID" \
    --client-id "${TG_OIDC_CLIENT_ID:-}" \
    --query 'UserPoolClient.CallbackURLs | [0]' \
    --output text 2>/dev/null)
  if [[ -z "$POOL_CALLBACK" || "$POOL_CALLBACK" == "None" ]]; then
    warn "Could not read the Cognito CallbackURL to verify \
the invite host — check it manually if a new admin's \
sign-in link doesn't resolve."
  elif [[ "$POOL_CALLBACK" == "$LIVE_CALLBACK" ]]; then
    ok "Cognito sign-in URL matches the live host \
($PUBLIC_HOST)"
  else
    warn "Cognito CallbackURL ($POOL_CALLBACK) does NOT \
match the live host ($LIVE_CALLBACK)."
    warn "Any invite already sent points at a stale host \
and its sign-in link is dead. The pool is now reconciled, \
but sent emails can't be edited — re-issue them: from the \
admin UI use 'Re-send invite', or POST \
/api/admin-roles/<email>/reinvite. New invites will carry \
the correct host. (To avoid this churn entirely, deploy \
with a stable TG_DOMAIN_NAME — see INSTALL.md.)"
  fi
fi

# ── 13c. Bedrock invocation-logging capture (optional) ──
# The analytics capture stream (separate from CUR spend): per selected
# region, deploy the per-region S3+KMS+Glue stack + logging config
# (cfn/tg-bedrock-invocation-logs.yaml), then seed the admin_config
# catalog so Settings + a future analysis layer know where the data
# lives. Skipped entirely when TG_INVLOGS_REGIONS is unset/blank (the
# wizard's Skip). Bedrock logging is a per-region same-region
# singleton, so one stack per region.
if [[ -n "${TG_INVLOGS_REGIONS:-}" ]]; then
  step "Enabling Bedrock invocation logging (analytics capture)"
  INVLOG_TPL="cfn/tg-bedrock-invocation-logs.yaml"
  if [[ ! -f "$INVLOG_TPL" ]]; then
    warn "template not found: $INVLOG_TPL — skipping invocation logging"
  else
    # Split the comma list WITHOUT mapfile/readarray (Bash 3.2). Trim
    # spaces; skip blanks; de-dupe.
    _seen_regions=" "
    _old_ifs="$IFS"; IFS=','
    for _r in $TG_INVLOGS_REGIONS; do
      IFS="$_old_ifs"
      _r=$(printf '%s' "$_r" | tr -d '[:space:]')
      [[ -z "$_r" ]] && { IFS=','; continue; }
      case "$_seen_regions" in *" $_r "*) IFS=','; continue;; esac
      _seen_regions="$_seen_regions$_r "
      echo "  Region $_r: deploying invocation-log capture stack"
      # Per-region stack in that region (bucket must be same-region as
      # the logging config). Idempotent; a failure warns but does not
      # abort the install (analytics is optional).
      if aws cloudformation deploy \
          "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
          --region "$_r" \
          --stack-name tg-bedrock-invocation-logs \
          --template-file "$INVLOG_TPL" \
          --capabilities CAPABILITY_IAM \
          --no-fail-on-empty-changeset >/dev/null; then
        ok "invocation-log stack up in $_r"
      else
        warn "invocation-log stack deploy failed in $_r — skipping it"
      fi
      IFS=','
    done
    IFS="$_old_ifs"
    # The per-region stacks are up. The admin_config CATALOG (what
    # Settings + the analysis layer read) is seeded by the app on
    # startup from the TG_INVLOGS_REGIONS env the container carries
    # (mirrors the bootstrap-admin auto-seed) — no auth-gated HTTP call
    # from the headless installer. TG_INVLOGS_REGIONS was already
    # threaded onto the container stack's task env by the deploy above
    # (it's in the installer's environment). Nothing more to do here;
    # the admin can adjust regions/Text in Settings → Invocation logs.
    ok "invocation-log capture stacks deployed for the selected \
region(s); the catalog seeds on app startup"
  fi
fi

# ── 14. Final summary ────────────────────────────────
step "Done"

# Lead with the one thing that matters to a non-technical installer:
# the app is up, here's how to sign in. The developer/operator URLs
# (/api/version, /docs, ECS console, log tail) are demoted under an
# "Advanced / troubleshooting" group. The login line names the
# provider that was actually configured (Cognito vs Okta).
if [[ "$TG_AUTH_PROVIDER" == "okta" ]]; then
  LOGIN_PROVIDER="Okta"
else
  LOGIN_PROVIDER="Cognito"
fi
# TG_BOOTSTRAP_ADMIN_EMAIL is required earlier, but fall back to a
# generic phrase rather than printing a blank if it's somehow empty.
SIGNIN_AS="${TG_BOOTSTRAP_ADMIN_EMAIL:-the admin email you configured}"

# #921: the bootstrap admin is CONFIRMED (permanent password). Print
# a sign-in hint that covers BOTH options without surfacing any
# secret: if the operator set a password, they sign in with it; if a
# random throwaway was used (default/headless), they enter via
# "Forgot password" (a reset code goes to the admin email). The host
# is the live ${API_HOST} (custom domain or current ALB DNS).
# (Open-Q2: small copy add, recommended.) No password is ever printed.
if [[ "$LOGIN_PROVIDER" == "Cognito" ]]; then
  CREDS_BLOCK="$(cat <<CREDS

Signing in (you are the first admin):
  • If you set an admin password during install, sign in with it.
  • Otherwise click "Forgot password" on the login page — a reset
    code is sent to ${SIGNIN_AS}; set your password, then sign in.
  (Forgot password works because the admin is pre-confirmed.)
CREDS
)"
else
  CREDS_BLOCK=""
fi

# #1119: under the `tg` wizard the ORCHESTRATOR owns the single "Done"
# banner — it must print exactly once, LAST, after CUR also finishes
# (today CUR runs AFTER this script, so a summary here lands before
# "deploying CUR…"). When TG_SUMMARY_OUT is set the wizard wants the
# values, not the printed block: emit a machine-readable KEY=VALUE file
# (a temp path the wizard reads) and DON'T print the summary. A
# standalone `bash tg-ecs-install.sh` run (var unset) still prints the
# full human summary below, unchanged.
if [[ -n "${TG_SUMMARY_OUT:-}" ]]; then
  {
    echo "API_SCHEME=${API_SCHEME}"
    echo "API_HOST=${API_HOST}"
    echo "SIGNIN_AS=${SIGNIN_AS}"
    echo "LOGIN_PROVIDER=${LOGIN_PROVIDER}"
    echo "AWS_REGION=${AWS_REGION}"
    echo "PROFILE_HINT=${PROFILE_HINT}"
  } > "$TG_SUMMARY_OUT"
else
cat <<EOF

$(printf '\033[1;32m✓ Install complete — tg is running.\033[0m')

Next step — open the admin console and sign in:
  1. Open:        ${API_SCHEME}://${API_HOST}/
  2. Sign in as:  ${SIGNIN_AS}
     (you're the first admin; you'll set up everyone else from here)
  3. First sign-in uses the ${LOGIN_PROVIDER} login you configured.
${CREDS_BLOCK}
EOF
  # The Advanced/troubleshooting block is noise for the common case —
  # print it only under TG_VERBOSE=1 (the `tg` wizard sets it from
  # --verbose; a direct script run opts in the same way). PROFILE_HINT
  # already carries its trailing space, so ${PROFILE_HINT}--region
  # renders with the separating space here.
  if [[ "${TG_VERBOSE:-}" == "1" ]]; then
cat <<EOF

Advanced / troubleshooting:
  Health check   ${API_SCHEME}://${API_HOST}/api/version
  API docs       ${API_SCHEME}://${API_HOST}/docs
  ECS console    https://us-east-1.console.aws.amazon.com/ecs/v2/clusters/tg-cluster
  Live logs      aws logs tail /ecs/tg-container --follow \\
                   ${PROFILE_HINT}--region $AWS_REGION
  Tear down      scripts/tg-ecs-destroy.sh
EOF
  fi
fi
