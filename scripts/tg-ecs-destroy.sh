#!/usr/bin/env bash
# Clean teardown of the ECS container deployment.
#
# Pairs with scripts/tg-ecs-install.sh. Removes:
#   - ECR repo tg-container (after image purge)
#   - CFN: tg-container-stack (VPC, NAT, RDS, ALB,
#     ECS cluster + services + task defs, Secrets,
#     IAM task roles, CloudWatch log group, etc.)
#   - With --full: also tg-bedrock-role (otherwise
#     preserved so the local install path keeps working).
#     #725: tg-bedrock-logging is retired (no longer created).
#
# OUT OF SCOPE (#568) — separately-managed stacks this
# destroyer never owns or removes, and the clean-slate
# verifier deliberately ignores:
#   - tg-cur-athena   (CUR/Athena; the installer treats it
#                      as a pre-existing dependency. Tear
#                      down with scripts/tg-cur-destroy.sh.)
#   - tg-cognito-pool (Cognito user pool — its own lifecycle)
#   - tg-admin-dist   (tg-admin binary S3 bucket)
#   - tg-BedrockAdmin (legacy untagged role, no CFN stack →
#                      no destroy path; manual: aws iam
#                      delete-role --role-name tg-BedrockAdmin
#                      after detaching policies)
# If you also want these gone, run their own destroy paths.
#
# Re-running on an already-destroyed account is a
# no-op and ends with the same final ✓.
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
#     scripts/tg-ecs-destroy.sh [--full]

set -euo pipefail

cd "$(dirname "$0")/.."

# ── Arg parsing ─────────────────────────────────────
FULL_TEARDOWN=0
for arg in "$@"; do
  case "$arg" in
    --full) FULL_TEARDOWN=1 ;;
    -h|--help)
      sed -n '2,28p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

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

STACK_NAME=tg-container-stack
ECR_REPO=tg-container
CLUSTER=tg-cluster
API_SVC=tg-api-service
WORKER_SVC=tg-worker-service

# ── Warn + 5s pause ─────────────────────────────────
step "Pre-flight"
if [[ "$FULL_TEARDOWN" -eq 1 ]]; then
  EXTRA="  - With --full: also tg-bedrock-role"
else
  EXTRA="  - Preserving tg-bedrock-role (use --full to drop)"
fi
cat <<EOF
⚠ Will destroy:
  - $STACK_NAME CFN (VPC, RDS, ALB, ECS, ECR, etc.)
  - All images in ECR repo $ECR_REPO
$EXTRA
Account: $TG_TARGET_ACCOUNT_ID  Region: $AWS_REGION
Continuing in 5s (Ctrl-C to abort)...
EOF
sleep 5

TOTAL_STEPS=4
[[ "$FULL_TEARDOWN" -eq 1 ]] && TOTAL_STEPS=5

# ── 1. Empty ECR repo so CFN can drop it ────────────
step "1/$TOTAL_STEPS — Empty ECR repo $ECR_REPO"

if aws ecr describe-repositories \
     --repository-names "$ECR_REPO" \
     --region "$AWS_REGION" \
     >/dev/null 2>&1; then
  IMG_IDS=$(
    aws ecr list-images \
      --repository-name "$ECR_REPO" \
      --region "$AWS_REGION" \
      --query 'imageIds[*]' \
      --output json
  )
  IMG_COUNT=$(
    printf '%s' "$IMG_IDS" \
      | python3 -c \
        'import json,sys;print(len(json.load(sys.stdin)))'
  )
  if [[ "$IMG_COUNT" -gt 0 ]]; then
    echo "  Deleting $IMG_COUNT images..."
    aws ecr batch-delete-image \
      --repository-name "$ECR_REPO" \
      --region "$AWS_REGION" \
      --image-ids "$IMG_IDS" \
      >/dev/null \
      || warn "batch-delete-image returned non-zero"
    ok "ECR repo $ECR_REPO emptied"
  else
    ok "ECR repo $ECR_REPO already empty"
  fi
else
  ok "ECR repo $ECR_REPO absent (skip)"
fi

# ── 2. Scale ECS services to 0 ──────────────────────
step "2/$TOTAL_STEPS — Scale ECS services to 0"

scale_service() {
  local svc="$1"
  if aws ecs describe-services \
       --cluster "$CLUSTER" \
       --services "$svc" \
       --region "$AWS_REGION" \
       --query 'services[?status==`ACTIVE`].serviceName' \
       --output text 2>/dev/null \
       | grep -q "$svc"; then
    echo "  Scaling $svc to 0..."
    aws ecs update-service \
      --cluster "$CLUSTER" \
      --service "$svc" \
      --desired-count 0 \
      --region "$AWS_REGION" \
      >/dev/null \
      || warn "update-service $svc returned non-zero"
    ok "Service $svc scaled to 0"
  else
    ok "Service $svc absent or inactive (skip)"
  fi
}

if aws ecs describe-clusters \
     --clusters "$CLUSTER" \
     --region "$AWS_REGION" \
     --query 'clusters[?status==`ACTIVE`].clusterName' \
     --output text 2>/dev/null \
     | grep -q "$CLUSTER"; then
  scale_service "$API_SVC"
  scale_service "$WORKER_SVC"
else
  ok "Cluster $CLUSTER absent (skip)"
fi

# ── 3. Delete container stack ───────────────────────
step "3/$TOTAL_STEPS — Removing the application"

empty_ecr() {
  if ! aws ecr describe-repositories \
       --repository-names "$ECR_REPO" \
       --region "$AWS_REGION" \
       >/dev/null 2>&1; then
    return 0
  fi
  local ids
  ids=$(aws ecr list-images \
    --repository-name "$ECR_REPO" \
    --region "$AWS_REGION" \
    --query 'imageIds[*]' --output json)
  local n
  n=$(printf '%s' "$ids" | python3 -c \
    'import json,sys;print(len(json.load(sys.stdin)))')
  if [[ "$n" -gt 0 ]]; then
    aws ecr batch-delete-image \
      --repository-name "$ECR_REPO" \
      --region "$AWS_REGION" \
      --image-ids "$ids" \
      >/dev/null 2>&1 || true
  fi
}

# #406: the protected RDS config (stage/prod default) sets
# DeletionProtection: true, which makes delete-stack fail. Before
# tearing down a stack that owns an RDS instance, disable
# deletion protection on any tg-* DB instance in this stack so an
# INTENTIONAL teardown can proceed. The Snapshot DeletionPolicy
# still captures a final snapshot on delete, so data is preserved
# even though the instance is removed. Idempotent: no-op when the
# stack has no RDS instance (e.g. the disposable config already
# has protection off) or none is found.
disable_db_deletion_protection() {
  local name="$1" db_id
  db_id=$(aws cloudformation describe-stack-resources \
    --stack-name "$name" \
    --region "$AWS_REGION" \
    --query "StackResources[?ResourceType=='AWS::RDS::DBInstance'].PhysicalResourceId | [0]" \
    --output text 2>/dev/null || true)
  [[ -z "$db_id" || "$db_id" == "None" ]] && return 0
  local prot
  prot=$(aws rds describe-db-instances \
    --db-instance-identifier "$db_id" \
    --region "$AWS_REGION" \
    --query "DBInstances[0].DeletionProtection" \
    --output text 2>/dev/null || true)
  if [[ "$prot" == "True" || "$prot" == "true" ]]; then
    echo "  Disabling deletion protection on $db_id"
    aws rds modify-db-instance \
      --db-instance-identifier "$db_id" \
      --region "$AWS_REGION" \
      --no-deletion-protection \
      --apply-immediately >/dev/null \
      || warn "could not disable deletion protection on $db_id"
  fi
}

delete_stack() {
  local name="$1"
  local attempt
  for attempt in 1 2; do
    if ! aws cloudformation describe-stacks \
         --stack-name "$name" \
         --region "$AWS_REGION" \
         >/dev/null 2>&1; then
      ok "Stack $name absent (skip)"
      return 0
    fi
    disable_db_deletion_protection "$name"
    echo "  Deleting stack: $name (attempt $attempt)"
    aws cloudformation delete-stack \
      --stack-name "$name" \
      --region "$AWS_REGION"
    echo "  Waiting for $name delete..."
    if aws cloudformation wait \
         stack-delete-complete \
         --stack-name "$name" \
         --region "$AWS_REGION"; then
      ok "Stack $name deleted"
      return 0
    fi
    # ECR-not-empty race: services may have repulled
    # an image during scale-down. Empty ECR + retry.
    warn "stack-delete failed; emptying ECR + retrying"
    empty_ecr
    sleep 5
  done
  fail "Stack $name failed to delete after retry"
}

delete_stack "$STACK_NAME"

# ── 4. Optional --full: bedrock layer ───────────────
if [[ "$FULL_TEARDOWN" -eq 1 ]]; then
  step "4/$TOTAL_STEPS — Delete bedrock-layer stacks"
  # #725 (#720 slice 3): tg-bedrock-logging is retired (CUR is the
  # sole spend source); no invocation-logging teardown anymore.
  # An EXISTING tg-bedrock-logging stack from a pre-#725 install is
  # a one-shot ops teardown (tg-ops), not deleted by this path.
  delete_stack tg-bedrock-role

  # Bedrock invocation-logging capture stacks (analytics). Per-region
  # (same-region singleton), so tear each region in TG_INVLOGS_REGIONS.
  # PRESERVE-BY-DEFAULT of the DATA: the per-region bucket + KMS key are
  # DeletionPolicy:Retain, so deleting the stack removes the logging
  # config wiring but KEEPS the captured objects (invocation-log data is
  # precious — same posture the retired logging stack used). The stack
  # delete runs in the STACK'S region, not $AWS_REGION.
  if [[ -n "${TG_INVLOGS_REGIONS:-}" ]]; then
    _seen=" "
    _oi="$IFS"; IFS=','
    for _r in $TG_INVLOGS_REGIONS; do
      IFS="$_oi"
      _r=$(printf '%s' "$_r" | tr -d '[:space:]')
      [[ -z "$_r" ]] && { IFS=','; continue; }
      case "$_seen" in *" $_r "*) IFS=','; continue;; esac
      _seen="$_seen$_r "
      if aws cloudformation describe-stacks \
           --stack-name tg-bedrock-invocation-logs \
           --region "$_r" >/dev/null 2>&1; then
        echo "  Deleting tg-bedrock-invocation-logs in $_r" \
             "(bucket + KMS key are Retain'd — captured data kept)"
        aws cloudformation delete-stack \
          --stack-name tg-bedrock-invocation-logs --region "$_r"
        aws cloudformation wait stack-delete-complete \
          --stack-name tg-bedrock-invocation-logs --region "$_r" \
          2>/dev/null \
          && ok "invocation-log stack removed in $_r (data retained)" \
          || warn "invocation-log stack delete incomplete in $_r"
      else
        ok "no invocation-log stack in $_r (skip)"
      fi
      IFS=','
    done
    IFS="$_oi"
  fi
fi

# ── Final — Verify clean slate ──────────────────────
step "$TOTAL_STEPS/$TOTAL_STEPS — Verify clean slate"

ERRORS=()

# #568: scope the residue check to what THIS destroyer OWNS.
# tg-ecs-destroy owns tg-container-stack (always) and
# tg-bedrock-role/tg-bedrock-logging (--full only). These other
# tg-* stacks/roles are SEPARATELY MANAGED — each has its own
# install/destroy lifecycle and the installer even treats
# tg-cur-athena as a pre-existing dependency (reads its outputs).
# Flagging them as "residue" made the clean-slate check
# structurally unreachable on any real account (#503 L3 finding).
# They are out of scope here — see the header "out of scope" note.
NOT_OWNED_STACKS_RE='^(tg-cognito-pool|tg-admin-dist|tg-cur-)'
# Roles owned by those separately-managed stacks, plus the legacy
# untagged tg-BedrockAdmin (created pre-installer 2026-05-21, no
# CFN stack tag → no destroy path removes it; documented manual
# cleanup, allow-listed here so it doesn't fail the verifier).
NOT_OWNED_ROLES_RE='^(tg-cur-|tg-BedrockAdmin|tg-cognito)'

# CFN stacks named tg-* (excluding separately-managed stacks
# always, and tg-bedrock-* unless --full).
STK_Q='StackSummaries[?starts_with(StackName,`tg-`)]'
STK_Q="$STK_Q.StackName"
TG_STACKS=$(
  aws cloudformation list-stacks \
    --stack-status-filter CREATE_COMPLETE \
      UPDATE_COMPLETE \
      UPDATE_ROLLBACK_COMPLETE \
      ROLLBACK_COMPLETE \
    --region "$AWS_REGION" \
    --query "$STK_Q" --output text
)
# Always strip the separately-managed stacks (#568).
TG_STACKS=$(
  printf '%s\n' $TG_STACKS \
    | grep -vE "$NOT_OWNED_STACKS_RE" || true
)
if [[ "$FULL_TEARDOWN" -ne 1 ]]; then
  # Strip tg-bedrock-* entries (still expected without --full).
  TG_STACKS=$(
    printf '%s\n' $TG_STACKS \
      | grep -v '^tg-bedrock-' || true
  )
fi
if [[ -n "${TG_STACKS// /}" ]]; then
  ERRORS+=("residual CFN stacks: $TG_STACKS")
else
  ok "Application fully removed"
fi

# IAM roles named tg-* (excluding tg-install-* always, the
# separately-managed roles always, and tg-bedrock-*/tg-Bedrock*
# unless --full).
ROLE_Q='Roles[?starts_with(RoleName,`tg-`)'
ROLE_Q="$ROLE_Q && !starts_with(RoleName,"
ROLE_Q="$ROLE_Q\`tg-install-\`)].RoleName"
TG_ROLES=$(
  aws iam list-roles \
    --query "$ROLE_Q" --output text
)
# Always strip the separately-managed roles (#568).
TG_ROLES=$(
  printf '%s\n' $TG_ROLES \
    | grep -vE "$NOT_OWNED_ROLES_RE" || true
)
if [[ "$FULL_TEARDOWN" -ne 1 ]]; then
  TG_ROLES=$(
    printf '%s\n' $TG_ROLES \
      | grep -viE '^tg-bedrock' || true
  )
fi
if [[ -n "${TG_ROLES// /}" ]]; then
  ERRORS+=("residual IAM roles: $TG_ROLES")
else
  ok "No owned tg-* IAM roles (excl separately-managed)"
fi

# ECR repos named tg-*.
ECR_Q='repositories[?starts_with(repositoryName,`tg-`)]'
ECR_Q="$ECR_Q.repositoryName"
TG_ECR=$(
  aws ecr describe-repositories \
    --region "$AWS_REGION" \
    --query "$ECR_Q" --output text 2>/dev/null \
    || true
)
if [[ -n "${TG_ECR// /}" ]]; then
  ERRORS+=("residual ECR repos: $TG_ECR")
else
  ok "No tg-* ECR repos"
fi

# VPCs tagged Name=tg-* — i.e. ONLY the VPCs tg created on the
# create-new path. #774: a bring-your-own (ExistingVpcId) VPC is
# never tg-tagged and is not a stack resource (the CreateVpc
# condition gates the VPC/IGW/subnets out), so it is invisible here
# and a stack delete never touches it. Never delete a VPC tg didn't
# create.
VPC_IDS=$(
  aws ec2 describe-vpcs \
    --region "$AWS_REGION" \
    --filters 'Name=tag:Name,Values=tg-*' \
    --query 'Vpcs[].VpcId' \
    --output text 2>/dev/null \
    || true
)
if [[ -n "${VPC_IDS// /}" ]]; then
  ERRORS+=("residual VPCs: $VPC_IDS")
else
  ok "No tg-* VPCs"
fi

# ALBs named tg-*.
ALB_Q='LoadBalancers[?starts_with('
ALB_Q="${ALB_Q}LoadBalancerName,\`tg-\`)]"
ALB_Q="${ALB_Q}.LoadBalancerName"
ALB_NAMES=$(
  aws elbv2 describe-load-balancers \
    --region "$AWS_REGION" \
    --query "$ALB_Q" \
    --output text 2>/dev/null \
    || true
)
if [[ -n "${ALB_NAMES// /}" ]]; then
  ERRORS+=("residual ALBs: $ALB_NAMES")
else
  ok "No tg-* ALBs"
fi

if [[ ${#ERRORS[@]} -gt 0 ]]; then
  printf '\033[1;31m✗\033[0m residue:\n' >&2
  for e in "${ERRORS[@]}"; do
    printf '    - %s\n' "$e" >&2
  done
  fail "destroy not clean — \
manual cleanup needed: ${ERRORS[*]}"
fi

# ── Done ────────────────────────────────────────────
step "Done"
ok "Account $TG_TARGET_ACCOUNT_ID is back to clean slate."
echo "Re-run scripts/tg-ecs-install.sh to redeploy."
