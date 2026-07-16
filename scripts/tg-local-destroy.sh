#!/usr/bin/env bash
# Clean teardown of the local container install.
#
# Pairs with scripts/tg-local-install.sh. Removes:
#   - Docker: containers (api, worker, postgres),
#     pgdata volume, built image
#   - CFN: tg-bedrock-role (#725: bedrock-logging retired)
#   - IAM: tg-consumer role; tg-BedrockQuotaDeny,
#     tg-BedrockSharedPolicy managed policies
#     (#591: tg-BedrockAdmin is gone — the desktop client
#     was removed in #574/#576)
#   - CW Logs: /aws/bedrock/invocations is PRESERVED by
#     default (#559: Retain'd + never auto-deleted); the
#     teardown prints the manual cleanup command and only
#     deletes on an explicit interactive y / TG_PURGE_*.
#   - Truncates .env.tg (keeps the file present)
#
# Re-running on an already-destroyed account is a no-op
# and ends with the same final ✓.
#
# Required env vars:
#   AWS_PROFILE          — profile with delete perms
#   TG_TARGET_ACCOUNT_ID    — must match caller's account
#
# Optional:
#   AWS_REGION           — default us-east-1
#
# Usage:
#   AWS_PROFILE=tg-admin \
#     TG_TARGET_ACCOUNT_ID=<12-digit account> \
#     scripts/tg-local-destroy.sh

set -euo pipefail

cd "$(dirname "$0")/.."

# Resolve TG_ENV → COMPOSE_PROJECT_NAME, host ports,
# env-file path. With TG_ENV unset, defaults preserve
# legacy behavior. See scripts/tg-env.sh + #145.
# shellcheck source=tg-env.sh
. "$(pwd)/scripts/tg-env.sh"

# ── Color helpers (match deploy-all.sh) ─────────────
step() {
  printf '\n\033[1;34m========== %s ==========\033[0m\n' \
    "$*"
}
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
fail() {
  printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2
  exit 1
}

# ── Validate env ────────────────────────────────────
: "${AWS_PROFILE:?must export AWS_PROFILE}"
: "${TG_TARGET_ACCOUNT_ID:?must export TG_TARGET_ACCOUNT_ID}"
export AWS_REGION="${AWS_REGION:-us-east-1}"

CALLER_ACCOUNT=$(
  aws sts get-caller-identity \
    --query Account --output text
)
if [[ "$CALLER_ACCOUNT" != "$TG_TARGET_ACCOUNT_ID" ]]; then
  fail "Caller account $CALLER_ACCOUNT != \
TG_TARGET_ACCOUNT_ID $TG_TARGET_ACCOUNT_ID — refusing to run"
fi

# ── Warn + 5s pause ─────────────────────────────────
step "Pre-flight"
cat <<EOF
⚠ Will destroy:
  - Docker containers + pgdata volume
  - CFN stacks: tg-bedrock-role
  - IAM roles: tg-consumer
Account: $TG_TARGET_ACCOUNT_ID  Region: $AWS_REGION
Continuing in 5s (Ctrl-C to abort)...
EOF
sleep 5

# ── 1. Docker teardown ──────────────────────────────
step "1/4 — Docker teardown ($COMPOSE_PROJECT_NAME)"

# We need the env-file present (even if empty) for
# `compose down` to share the same project + port
# bindings as the install. Truncate-then-down so
# any stale creds can't leak into a mistaken `up`.
: > "$TG_ENV_FILE"
ok "$TG_ENV_FILE truncated"

if command -v docker >/dev/null 2>&1; then
  docker compose --env-file "$TG_ENV_FILE" down \
    -v --remove-orphans --rmi local \
    || warn "docker compose down returned non-zero"

  # Verify nothing is running for this project
  RUNNING=$(
    docker compose --env-file "$TG_ENV_FILE" \
      ps -q 2>/dev/null || true
  )
  if [[ -n "$RUNNING" ]]; then
    fail "docker compose still has containers: \
$RUNNING"
  fi
  ok "Docker resources removed"
else
  warn "docker not installed — skipping"
fi

# ── 2. CFN teardown (reverse-dep order) ─────────────
step "2/4 — CFN teardown"

delete_stack() {
  local name="$1"
  if aws cloudformation describe-stacks \
       --stack-name "$name" \
       --region "$AWS_REGION" \
       >/dev/null 2>&1; then
    echo "  Deleting stack: $name"
    aws cloudformation delete-stack \
      --stack-name "$name" \
      --region "$AWS_REGION"
    echo "  Waiting for $name delete..."
    aws cloudformation wait \
      stack-delete-complete \
      --stack-name "$name" \
      --region "$AWS_REGION" \
      || fail "Stack $name failed to delete cleanly"
    ok "Stack $name deleted"
  else
    ok "Stack $name absent (skip)"
  fi
}

# tg-bedrock-role first (depends on nothing here, but
# we delete role-side before logging-side per spec).
#
# These CFN stacks are account-wide singletons (not env-
# scoped). When TG_ENV is set we still drop them — that
# matches the existing behavior of "destroy tears down
# the bedrock-* stacks too", and means a destroy of one
# env will rip them out from under the other. That's
# acceptable because the only reason to run two envs in
# parallel is dev↔stage on different *accounts*; running
# both in the same account isn't a supported workflow.
# #725 (#720 slice 3): tg-bedrock-logging is retired (CUR is the
# sole spend source). No invocation-logging teardown anymore.
# NOTE: an EXISTING tg-bedrock-logging stack from a pre-#725
# install is left in place by this script — its removal is a
# one-shot ops teardown (tg-ops), not a code-path delete.
delete_stack tg-bedrock-role

# ── 3. Verify clean slate ───────────────────────────
step "3/4 — Verify clean slate"

ERRORS=()

# IAM roles starting with tg- that THIS script owns.
# Exclude:
#   - tg-install-*    (platform/SSO layer)
#   - tg-cur-*        (separate tg-cur-athena stack;
#                      use tg-cur-destroy.sh)
#   - tg-BedrockAdmin (legacy orphan from the removed
#                      desktop client #574/#576; templates
#                      no longer create it, but a
#                      previously-deployed role may linger
#                      until the #591 ops:chore teardown —
#                      tolerate it here, don't false-alarm)
ROLE_Q='Roles[?starts_with(RoleName,`tg-`)'
ROLE_Q="$ROLE_Q && !starts_with(RoleName,"
ROLE_Q="$ROLE_Q\`tg-install-\`)"
ROLE_Q="$ROLE_Q && !starts_with(RoleName,"
ROLE_Q="$ROLE_Q\`tg-cur-\`)"
ROLE_Q="$ROLE_Q && RoleName!="
ROLE_Q="$ROLE_Q\`tg-BedrockAdmin\`].RoleName"
TG_ROLES=$(
  aws iam list-roles \
    --query "$ROLE_Q" --output text
)
if [[ -n "$TG_ROLES" ]]; then
  ERRORS+=("residual IAM roles: $TG_ROLES")
else
  ok "No tg-* IAM roles"
fi

# IAM customer-managed policies starting with tg-
# (excludes tg-cur-* — owned by tg-cur-athena stack)
POL_Q='Policies[?starts_with(PolicyName,`tg-`)'
POL_Q="$POL_Q && !starts_with(PolicyName,"
POL_Q="$POL_Q\`tg-cur-\`)].PolicyName"
TG_POLS=$(
  aws iam list-policies --scope Local \
    --query "$POL_Q" --output text
)
if [[ -n "$TG_POLS" ]]; then
  ERRORS+=("residual IAM policies: $TG_POLS")
else
  ok "No tg-* IAM policies"
fi

# CFN stacks starting with tg-
# Excludes:
#   - tg-cur-*       (owned by tg-cur-deploy.sh)
#   - tg-admin-dist  (binary distribution; persists
#                     across local-destroy by design)
STK_Q='StackSummaries[?starts_with(StackName,`tg-`)'
STK_Q="$STK_Q && !starts_with(StackName,"
STK_Q="$STK_Q\`tg-cur-\`)"
STK_Q="$STK_Q && StackName!="
STK_Q="$STK_Q\`tg-admin-dist\`].StackName"
TG_STACKS=$(
  aws cloudformation list-stacks \
    --stack-status-filter CREATE_COMPLETE \
      UPDATE_COMPLETE \
    --query "$STK_Q" --output text
)
if [[ -n "$TG_STACKS" ]]; then
  ERRORS+=("residual CFN stacks: $TG_STACKS")
else
  ok "No tg-* CFN stacks"
fi

# CW Logs (informational — stack manages this)
BEDROCK_LOGS=$(
  aws logs describe-log-groups \
    --log-group-name-prefix /aws/bedrock \
    --query 'logGroups[].logGroupName' \
    --output text
)
if [[ -n "$BEDROCK_LOGS" ]]; then
  warn "Bedrock log groups still present: \
$BEDROCK_LOGS"
else
  ok "No /aws/bedrock log groups"
fi

# Docker containers + volumes for this project
if command -v docker >/dev/null 2>&1; then
  # Use the active compose project name for filtering
  # so dev/stage teardowns don't false-alarm on each
  # other's resources.
  DKR_CONT=$(
    docker ps --filter "name=$COMPOSE_PROJECT_NAME" \
      --format '{{.Names}}'
  )
  if [[ -n "$DKR_CONT" ]]; then
    ERRORS+=("docker containers: $DKR_CONT")
  else
    ok "No $COMPOSE_PROJECT_NAME docker containers"
  fi

  DKR_VOL=$(
    docker volume ls --filter "name=$COMPOSE_PROJECT_NAME" \
      --format '{{.Name}}'
  )
  if [[ -n "$DKR_VOL" ]]; then
    ERRORS+=("docker volumes: $DKR_VOL")
  else
    ok "No $COMPOSE_PROJECT_NAME docker volumes"
  fi
fi

if [[ ${#ERRORS[@]} -gt 0 ]]; then
  printf '\033[1;31m✗\033[0m residue:\n' >&2
  for e in "${ERRORS[@]}"; do
    printf '    - %s\n' "$e" >&2
  done
  fail "destroy not clean — \
manual cleanup needed: ${ERRORS[*]}"
fi

# ── 4. Truncate env-file ────────────────────────────
step "4/4 — Truncate $TG_ENV_FILE"
: > "$TG_ENV_FILE"
ok "$TG_ENV_FILE truncated"

# ── Done ────────────────────────────────────────────
step "Done"
ok "Account $TG_TARGET_ACCOUNT_ID is back to clean slate."
echo "Re-run scripts/tg-local-install.sh to redeploy."
