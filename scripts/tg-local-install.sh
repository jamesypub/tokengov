#!/usr/bin/env bash
# tg-local-install.sh
#
# Idempotent installer for the container stack on this
# EC2 host. Brings up postgres + api + worker via
# docker compose, after deploying the two prerequisite
# CFN stacks (tg-bedrock-role) and
# building the React UI bundle.
#
# Required env vars:
#   AWS_PROFILE              deploy profile (e.g.
#                            tg-install-<account>)
#   TG_TARGET_ACCOUNT_ID        12-digit account; must
#                            match caller's account
#   TG_BOOTSTRAP_ADMIN_EMAIL    seeded into admin_roles
#                            with role=org_admin.
#                            If unset, the script
#                            prompts on a TTY.
#
# Optional:
#   AWS_REGION               default us-east-1
#   TG_SKIP_UI_BUILD=1          skip React build/copy
#                            (auto-set if node missing)
#
# Re-running after a successful run is a no-op:
#   - aws cloudformation deploy is idempotent
#   - docker compose up -d --build is idempotent
#   - the SQL seed uses ON CONFLICT DO NOTHING

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
fail()  {
  printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2
  exit 1
}

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Resolve TG_ENV → COMPOSE_PROJECT_NAME, host ports,
# env-file path. With TG_ENV unset, this is a no-op
# (defaults match the legacy public-INSTALL.md path).
# See scripts/tg-env.sh + issue #145.
# shellcheck source=tg-env.sh
. "$REPO_ROOT/scripts/tg-env.sh"

# #921: shared bootstrap-admin Cognito password helper (Option A
# random / Option B operator-provided; both → CONFIRMED so
# forgot-password works). Defines tg_set_bootstrap_admin_password.
# shellcheck source=tg-cognito-bootstrap-pw.sh
. "$REPO_ROOT/scripts/tg-cognito-bootstrap-pw.sh"

# ── 1. Validate required env vars ────────────────────
step "Validating environment"

# #768: AWS_PROFILE is optional — fall back to the default AWS
# credential chain when unset (instance role / SSO default / env
# creds) instead of a cryptic `:?` failure. PROFILE_ARGS is spliced
# into each aws call ("${PROFILE_ARGS[@]}"); empty → no --profile.
if [[ -n "${AWS_PROFILE:-}" ]]; then
  PROFILE_ARGS=(--profile "$AWS_PROFILE")
else
  PROFILE_ARGS=()
  warn "AWS_PROFILE not set — using the default AWS credential \
chain. Export AWS_PROFILE=tg-install-<account> to pin a profile."
fi
: "${TG_TARGET_ACCOUNT_ID:?\
must export TG_TARGET_ACCOUNT_ID=<12-digit account>\
}"
if [[ -z "${TG_BOOTSTRAP_ADMIN_EMAIL:-}" ]]; then
  if [[ -t 0 ]]; then
    read -rp "Bootstrap admin email (org_admin): " \
      TG_BOOTSTRAP_ADMIN_EMAIL
  else
    fail "TG_BOOTSTRAP_ADMIN_EMAIL not set and stdin \
is not a TTY; export it or run interactively"
  fi
fi
EMAIL_RE='^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'
if ! [[ "$TG_BOOTSTRAP_ADMIN_EMAIL" =~ $EMAIL_RE ]]; then
  fail "Invalid email: $TG_BOOTSTRAP_ADMIN_EMAIL"
fi
export AWS_REGION="${AWS_REGION:-us-east-1}"
TG_SKIP_UI_BUILD="${TG_SKIP_UI_BUILD:-0}"

# #357: admin auth provider. cognito | okta | desktop.
# OQ1 resolved as validate-when-set + preserve-default
# rather than strict force-choose: the existing default
# path is headless test-trust dev mode (TG_AUTH_TEST_TRUST),
# used by the stage dryrun harness — a hard fail on unset
# would regress every headless install. So: when set it
# must be a known value; when unset it stays empty (current
# behavior) and we just nudge the operator.
TG_AUTH_PROVIDER="${TG_AUTH_PROVIDER:-}"
case "$TG_AUTH_PROVIDER" in
  cognito|okta|desktop|"") : ;;
  *) fail "TG_AUTH_PROVIDER must be cognito|okta|desktop \
(got '$TG_AUTH_PROVIDER')" ;;
esac
if [[ -z "$TG_AUTH_PROVIDER" ]]; then
  warn "TG_AUTH_PROVIDER unset — using the default \
test-trust dev login. Set cognito|okta|desktop to pick an \
admin auth path (see docs/admin-setup.md § Auth provider \
choices)."
fi

if ! [[ "$TG_TARGET_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  fail "TG_TARGET_ACCOUNT_ID must be 12 digits"
fi

ok "AWS_PROFILE          = ${AWS_PROFILE:-<default credential chain>}"
ok "AWS_REGION           = $AWS_REGION"
ok "TG_TARGET_ACCOUNT_ID    = $TG_TARGET_ACCOUNT_ID"
ok "TG_BOOTSTRAP_ADMIN_EMAIL= $TG_BOOTSTRAP_ADMIN_EMAIL"
ok "TG_ENV                  = ${TG_ENV:-<unset, default>}"
ok "compose project         = $COMPOSE_PROJECT_NAME"
ok "host ports              = api:$TG_API_PORT pg:$TG_PG_PORT"
ok "env-file                = $TG_ENV_FILE"

# ── 2. Pre-flight checks ─────────────────────────────
step "Pre-flight checks"

# AWS caller identity must match TG_TARGET_ACCOUNT_ID
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

if [[ "$CALLER_ACCT" != "$TG_TARGET_ACCOUNT_ID" ]]; then
  fail "caller account $CALLER_ACCT != \
TG_TARGET_ACCOUNT_ID $TG_TARGET_ACCOUNT_ID"
fi
ok "AWS caller: $CALLER_ARN"

# docker + docker compose
if ! docker --version >/dev/null 2>&1; then
  fail "docker not found on PATH"
fi
ok "$(docker --version)"

if ! docker compose version >/dev/null 2>&1; then
  fail "docker compose not found"
fi
ok "$(docker compose version | head -1)"

# node + npm (skippable)
if [[ "$TG_SKIP_UI_BUILD" != "1" ]]; then
  if ! node --version >/dev/null 2>&1 \
    || ! npm --version >/dev/null 2>&1; then
    warn "node/npm not found — \
setting TG_SKIP_UI_BUILD=1"
    TG_SKIP_UI_BUILD=1
  else
    ok "$(node --version) / npm $(npm --version)"
  fi
fi

# ── 3. Resolve trust principal for tg-bedrock-role ──
step "Configuring who can use Bedrock"

# Convert assumed-role ARN to underlying role ARN so
# the installer (and any session it spawns) can
# sts:AssumeRole tg-consumer later for tests.
#   arn:aws:sts::ACCT:assumed-role/ROLE/SESSION
#   → arn:aws:iam::ACCT:role/ROLE
# Two outputs feed the deploy (exactly one populated):
#   TRUST_PRINCIPAL    → TrustedIamPrincipals (exact ARN)
#   TRUST_SSO_ARNLIKE  → TrustedSsoPrincipalArnLike (ArnLike on
#                        aws:PrincipalArn, for IDC permission-set roles)
TRUST_PRINCIPAL=""
TRUST_SSO_ARNLIKE=""
if [[ "$CALLER_ARN" == *":assumed-role/AWSReservedSSO_"* ]]; then
  # IDC / SSO permission-set caller. The bare role/ROLE sed would
  # produce a MALFORMED ARN (the real SSO role lives under the
  # /aws-reserved/sso.amazonaws.com/ path) that CFN rejects with
  # "Invalid principal in policy". Emit the valid PATH-FORM ARN and
  # reference it with an ArnLike condition. Name-agnostic
  # (AWSReservedSSO_*): trust every permission set in the account —
  # tg is principal-agnostic, and same-account scope is enforced by
  # the Principal: AWS = <acct>:root clause. Account is $CALLER_ACCT
  # (where the SSO session originates).
  TRUST_SSO_ARNLIKE=\
"arn:aws:iam::${CALLER_ACCT}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_*"
  ok "Access configured"
elif [[ "$CALLER_ARN" == *":assumed-role/"* ]]; then
  ROLE_NAME=$(printf '%s' "$CALLER_ARN" \
    | sed -E \
    's#.*:assumed-role/([^/]+)/.*#\1#')
  TRUST_PRINCIPAL=\
"arn:aws:iam::${TG_TARGET_ACCOUNT_ID}:role/${ROLE_NAME}"
  ok "Access configured"
else
  # Plain user / role ARN — pass through.
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
  fail "$ROLE_TPL not found"

# NOTE: the current template hardcodes role/policy
# names (tg-consumer, tg-BedrockQuotaDeny,
# tg-BedrockSharedPolicy). Those names are NOT
# parameters yet, so we don't pass overrides for them
# — passing unknown CFN parameters would error.
# (#591/#566D: tg-BedrockAdmin + the tg-admin-dist stack are
# gone — the desktop client was deleted in #574/#576; no admin
# role to provision here.)
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

# #591 (#566D): the tg-admin-dist stack deploy (the desktop
# binary's S3 bucket + tg-BedrockAdmin role) is removed — the
# desktop client was deleted in #574/#576 and cfn/tg-admin-dist.yaml
# no longer exists. Admins use the web UI (Cognito/OIDC) login.

# ── 5b. Cognito pool (only when provider=cognito) ────
# #357: deploy tg-cognito-pool and capture its outputs so
# the container reads TG_OIDC_* + TG_COGNITO_USER_POOL_ID.
# Mirrors the TG_BOOTSTRAP_ADMIN_EMAIL env-driven pattern;
# skipped entirely for okta / desktop / unset.
COGNITO_POOL_ID=""
COGNITO_ISSUER=""
COGNITO_CLIENT_ID=""
if [[ "$TG_AUTH_PROVIDER" == "cognito" ]]; then
  step "Setting up admin sign-in"
  COGNITO_TPL="cfn/tg-cognito-pool.yaml"
  [[ -f "$COGNITO_TPL" ]] || fail "$COGNITO_TPL not found"
  aws cloudformation deploy \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    --stack-name tg-cognito-pool \
    --template-file "$COGNITO_TPL" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset
  COGNITO_POOL_ID=$(aws cloudformation describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --stack-name tg-cognito-pool \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue | [0]" \
    --output text 2>/dev/null)
  COGNITO_ISSUER=$(aws cloudformation describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --stack-name tg-cognito-pool \
    --query "Stacks[0].Outputs[?OutputKey=='OidcIssuer'].OutputValue | [0]" \
    --output text 2>/dev/null)
  COGNITO_CLIENT_ID=$(aws cloudformation describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --stack-name tg-cognito-pool \
    --query "Stacks[0].Outputs[?OutputKey=='OidcClientId'].OutputValue | [0]" \
    --output text 2>/dev/null)
  [[ -n "$COGNITO_POOL_ID" \
     && "$COGNITO_POOL_ID" != "None" ]] \
    || fail "tg-cognito-pool deployed but UserPoolId \
output missing"
  ok "Cognito pool: $COGNITO_POOL_ID"
  ok "Cognito issuer: $COGNITO_ISSUER"

  # #921: put the bootstrap admin in CONFIRMED (permanent password)
  # so forgot-password works — same choice as the ECS installer
  # (Option B if TG_BOOTSTRAP_ADMIN_PASSWORD / TTY prompt, else a
  # random throwaway). Never logs/prints/stores the secret.
  #
  # #937: create the bootstrap admin here (idempotent), not via a
  # create-only CFN resource in tg-cognito-pool — see the ECS
  # installer + tg-cognito-bootstrap-pw.sh for why.
  tg_ensure_bootstrap_admin_user \
    "$COGNITO_POOL_ID" "$TG_BOOTSTRAP_ADMIN_EMAIL" "$AWS_REGION"
  tg_set_bootstrap_admin_password \
    "$COGNITO_POOL_ID" "$TG_BOOTSTRAP_ADMIN_EMAIL" "$AWS_REGION"
fi

# ── 6. Build React UI ────────────────────────────────
step "Building React UI"

if [[ "$TG_SKIP_UI_BUILD" == "1" ]]; then
  warn "TG_SKIP_UI_BUILD=1 — skipping admin-ui build"
  warn "Browser UI at / will 404. Re-run without \
TG_SKIP_UI_BUILD=1 to enable."
else
  (
    cd admin-ui/web
    npm ci
    npm run build
  )
  mkdir -p container/static
  # FastAPI mounts container/static/ at / when index.html
  # is present. The mount is wired in container/api/main.py.
  rm -rf container/static/*
  cp -R admin-ui/web/dist/. container/static/
  ok "Built React UI → container/static/"
fi

# ── 7. Generate env-file ─────────────────────────────
step "Generating $TG_ENV_FILE"

# Ensure .env.tg* glob is gitignored before we write the
# file. We rely on a single `.env.tg*` entry that covers
# .env.tg, .env.tg.dev, .env.tg.stage in one shot.
if ! grep -qxF '.env.tg*' .gitignore 2>/dev/null; then
  printf '\n# tg-local-install env file(s)\n.env.tg*\n' \
    >> .gitignore
  ok "Appended .env.tg* to .gitignore"
else
  ok ".env.tg* already in .gitignore"
fi

# Discover CUR/Athena outputs if tg-cur-athena exists.
# These let /api/analytics/run actually execute queries.
ATHENA_RESULTS=""
ATHENA_DB=""
ATHENA_WG=""
CUR_TABLE=""
if aws cloudformation describe-stacks \
     "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
     --stack-name tg-cur-athena \
     >/dev/null 2>&1; then
  ATHENA_RESULTS=$(aws cloudformation \
    describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --stack-name tg-cur-athena \
    --query "Stacks[0].Outputs[?OutputKey=='AthenaResultsBucketName'].OutputValue | [0]" \
    --output text)
  ATHENA_DB=$(aws cloudformation describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --stack-name tg-cur-athena \
    --query "Stacks[0].Outputs[?OutputKey=='GlueDatabaseName'].OutputValue | [0]" \
    --output text)
  ATHENA_WG=$(aws cloudformation describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --stack-name tg-cur-athena \
    --query "Stacks[0].Outputs[?OutputKey=='AthenaWorkgroupName'].OutputValue | [0]" \
    --output text)
  # CurTableName output added 2026-05-22; older stacks
  # won't have it. Default to 'data' (BCM CUR 2.0 layout).
  CUR_TABLE=$(aws cloudformation describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --stack-name tg-cur-athena \
    --query "Stacks[0].Outputs[?OutputKey=='CurTableName'].OutputValue | [0]" \
    --output text 2>/dev/null)
  if [[ -z "$CUR_TABLE" || "$CUR_TABLE" == "None" ]]; then
    CUR_TABLE="data"
  fi
  ok "CUR/Athena: results=$ATHENA_RESULTS db=$ATHENA_DB \
wg=$ATHENA_WG table=$CUR_TABLE"

  # #590 (#566C): no tg-ApiRunner re-deploy. The api queries
  # CUR/Athena under the container's own credentials (boto3
  # native chain — the mounted ~/.aws/ profile on local-compose,
  # the tg-app task role on ECS), so there's no dedicated role
  # to deploy or assume. The ATHENA_* env vars below are all the
  # api needs.
else
  CUR_TABLE="data"
  warn "Cost reporting not set up yet — the Cost Reports page \
will be empty until you run scripts/tg-cur-deploy.sh"
fi

cat > "$TG_ENV_FILE" <<EOF
DATABASE_URL=postgresql://tg:tg@postgres:5432/tg
AWS_REGION=$AWS_REGION
AWS_ACCOUNT_ID=$TG_TARGET_ACCOUNT_ID
ALLOWED_DOMAINS=
LOG_GROUP=/aws/bedrock/invocations
ROLE_NAME_FILTER=tg-consumer
DENY_POLICY_NAME=tg-BedrockQuotaDeny
TG_TOKEN_CONSUMER_ROLE_NAME=tg-consumer
TG_AUTH_TEST_TRUST=${TG_AUTH_TEST_TRUST:-1}
BOOTSTRAP_ADMIN_EMAIL=$TG_BOOTSTRAP_ADMIN_EMAIL
ATHENA_RESULTS_BUCKET=${ATHENA_RESULTS:+s3://$ATHENA_RESULTS/}
ATHENA_DATABASE=$ATHENA_DB
ATHENA_WORKGROUP=$ATHENA_WG
CUR_TABLE_NAME=$CUR_TABLE
AWS_PROFILE=$AWS_PROFILE
COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME
TG_API_PORT=$TG_API_PORT
TG_PG_PORT=$TG_PG_PORT
TG_AUTH_REQUIRE_LOGIN=${TG_AUTH_REQUIRE_LOGIN:-}
TG_OIDC_ISSUER=${TG_OIDC_ISSUER:-$COGNITO_ISSUER}
TG_OIDC_CLIENT_ID=${TG_OIDC_CLIENT_ID:-$COGNITO_CLIENT_ID}
TG_OIDC_CLIENT_SECRET=${TG_OIDC_CLIENT_SECRET:-}
TG_OIDC_REDIRECT_URI=${TG_OIDC_REDIRECT_URI:-}
TG_COOKIE_INSECURE=${TG_COOKIE_INSECURE:-}
TG_AUTH_PROVIDER=$TG_AUTH_PROVIDER
TG_COGNITO_USER_POOL_ID=$COGNITO_POOL_ID
EOF

ok "$TG_ENV_FILE written"

# ── 8. docker compose up ─────────────────────────────
step "Starting docker compose stack"

# #538: real product version into the image (compose reads
# TG_VERSION via build.args) so the UI footer shows it, not "vdev".
# #1000: honor a pre-set TG_VERSION (the CLI derives it ONCE via
# runner.build_version and threads it here) so the banner == the
# deployed stamp. #1088: self-derive from the committed VERSION file +
# short HEAD SHA, NOT `git describe` (a stale force-moved tag on a
# naive `git pull` makes describe fall back to a bare SHA). Fall back
# to the bare short SHA when VERSION is unreadable, then 'dev'.
if [[ -z "${TG_VERSION:-}" ]]; then
  _tg_ver=$(tr -d '[:space:]' < "$REPO_ROOT/VERSION" 2>/dev/null || true)
  _tg_sha=$(git rev-parse --short HEAD 2>/dev/null || true)
  _tg_dirty=""
  [[ -n "$(git status --porcelain 2>/dev/null)" ]] && _tg_dirty="-dirty"
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
export TG_VERSION

# boto3 in the api/worker uses AWS_PROFILE + the mounted
# host ~/.aws/ to call AssumeRole itself (#116). No
# materialized creds, no expiry timer.
docker compose --env-file "$TG_ENV_FILE" up -d --build
ok "compose up -d --build returned 0 (version $TG_VERSION)"

# ── 9. Wait for /api/version ─────────────────────────
step "Waiting for api → /api/version (max 60s)"

API_URL="http://localhost:${TG_API_PORT}/api/version"
WAITED=0
while (( WAITED < 60 )); do
  if curl -fs "$API_URL" >/dev/null 2>&1; then
    break
  fi
  sleep 1
  WAITED=$(( WAITED + 1 ))
done

if ! curl -fs "$API_URL" >/dev/null 2>&1; then
  docker compose --env-file "$TG_ENV_FILE" \
    logs --tail=50 api || true
  fail "api never returned 200 at $API_URL"
fi
ok "api healthy: $(curl -fs "$API_URL")"

# ── 10. Seed bootstrap admin ─────────────────────────
step "Seeding bootstrap admin"

# admin_roles columns (container/db/models.py):
#   id (auto), email, role, team_id, granted_by,
#   granted_at; UNIQUE(email, role, team_id).
# team_id is NULL for org_admin scope, but the unique
# constraint treats NULLs as distinct in Postgres, so
# we additionally guard with a WHERE NOT EXISTS clause
# to keep this idempotent across re-runs.
SQL="INSERT INTO admin_roles \
(email, role, team_id, granted_by) \
SELECT '${TG_BOOTSTRAP_ADMIN_EMAIL}', \
'org_admin', NULL, 'tg-local-install' \
WHERE NOT EXISTS ( \
  SELECT 1 FROM admin_roles \
  WHERE email='${TG_BOOTSTRAP_ADMIN_EMAIL}' \
    AND role='org_admin' \
    AND team_id IS NULL \
);"

docker compose --env-file "$TG_ENV_FILE" \
  exec -T postgres \
  psql -U tg -d tg -v ON_ERROR_STOP=1 \
  -c "$SQL" >/dev/null
ok "Bootstrap admin seeded: \
$TG_BOOTSTRAP_ADMIN_EMAIL (org_admin)"

# ── 11. Final summary ────────────────────────────────
step "Done"

cat <<EOF

$(printf '\033[1;32m✓ Local install complete.\033[0m')

Compose project: $COMPOSE_PROJECT_NAME
Env-file       : $TG_ENV_FILE

URLs:
  Health   http://localhost:${TG_API_PORT}/api/version
  Docs     http://localhost:${TG_API_PORT}/docs

DB shell:
  docker compose --env-file $TG_ENV_FILE \\
    exec postgres psql -U tg -d tg

Logs:
  docker compose --env-file $TG_ENV_FILE \\
    logs -f api worker

Tear down:
  ${TG_ENV:+TG_ENV=$TG_ENV }scripts/tg-local-destroy.sh
EOF
