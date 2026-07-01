#!/usr/bin/env bash
# tg-cur-destroy.sh
#
# Clean teardown of the tg-cur-athena CFN stack.
#
# Pairs with scripts/tg-cur-deploy.sh. Removes:
#   - CFN stack tg-cur-athena (CUR export, Glue
#     database + static table, Athena workgroup,
#     saved queries) — #591: no crawler, no
#     tg-BedrockAdmin policy
#   - S3 buckets (DeletionPolicy: Retain on the
#     template, so we empty + delete them by hand):
#       tg-cur-<acct>-<region>
#       tg-athena-results-<acct>-<region>
#     ALL CUR DATA + ATHENA RESULTS ARE LOST.
#
# Re-running on an already-destroyed account is a
# no-op and ends with the same final ✓.
#
# Required env vars:
#   AWS_PROFILE          deploy profile with delete
#                        perms
#   TG_TARGET_ACCOUNT_ID    12-digit account; must match
#                        caller's account
#
# Optional:
#   AWS_REGION           default us-east-1
#   STACK_NAME           default tg-cur-athena
#
# Usage:
#   AWS_PROFILE=tg-install-<account> \
#     TG_TARGET_ACCOUNT_ID=<12-digit account> \
#     scripts/tg-cur-destroy.sh

set -euo pipefail

cd "$(dirname "$0")/.."

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
STACK_NAME="${STACK_NAME:-tg-cur-athena}"

if ! [[ "$TG_TARGET_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  fail "TG_TARGET_ACCOUNT_ID must be 12 digits"
fi

CALLER_ACCOUNT=$(
  aws sts get-caller-identity \
    --profile "$AWS_PROFILE" \
    --query Account --output text
) || fail "aws sts get-caller-identity failed"

if [[ "$CALLER_ACCOUNT" != "$TG_TARGET_ACCOUNT_ID" ]]; then
  fail "Caller account $CALLER_ACCOUNT != \
TG_TARGET_ACCOUNT_ID $TG_TARGET_ACCOUNT_ID — refusing"
fi

CUR_BUCKET_DEFAULT=\
"tg-cur-${TG_TARGET_ACCOUNT_ID}-${AWS_REGION}"
ATHENA_BUCKET_DEFAULT=\
"tg-athena-results-${TG_TARGET_ACCOUNT_ID}-${AWS_REGION}"

# ── Warn + 5s pause ─────────────────────────────────
step "Pre-flight"
cat <<EOF
⚠ Will destroy:
  - CFN stack: $STACK_NAME
  - CUR 2.0 export, Glue db + static table,
    Athena workgroup, saved queries
  - S3 buckets (AND ALL DATA):
      $CUR_BUCKET_DEFAULT
      $ATHENA_BUCKET_DEFAULT
Account: $TG_TARGET_ACCOUNT_ID  Region: $AWS_REGION
Continuing in 5s (Ctrl-C to abort)...
EOF
sleep 5

# ── 1. Resolve actual bucket names from outputs ─────
step "1/4 — Resolving bucket names"

# Prefer stack outputs (handles non-default names);
# fall back to the default naming pattern if the
# stack is already gone.
CUR_BUCKET=""
ATHENA_BUCKET=""

if aws cloudformation describe-stacks \
     --stack-name "$STACK_NAME" \
     --profile "$AWS_PROFILE" \
     --region "$AWS_REGION" \
     >/dev/null 2>&1; then
  read_output() {
    local key="$1"
    aws cloudformation describe-stacks \
      --profile "$AWS_PROFILE" \
      --region "$AWS_REGION" \
      --stack-name "$STACK_NAME" \
      --query \
      "Stacks[0].Outputs[?OutputKey=='${key}']\
.OutputValue | [0]" \
      --output text 2>/dev/null
  }
  CUR_BUCKET=$(read_output CurBucketName || true)
  ATHENA_BUCKET=$(
    read_output AthenaResultsBucketName || true
  )
fi

# Sanitize "None" / empty → fall back to defaults.
if [[ -z "$CUR_BUCKET" || "$CUR_BUCKET" == "None" ]]
then
  CUR_BUCKET="$CUR_BUCKET_DEFAULT"
fi
if [[ -z "$ATHENA_BUCKET" \
      || "$ATHENA_BUCKET" == "None" ]]; then
  ATHENA_BUCKET="$ATHENA_BUCKET_DEFAULT"
fi

ok "CUR bucket    : $CUR_BUCKET"
ok "Athena bucket : $ATHENA_BUCKET"

# ── 2. Empty both buckets ───────────────────────────
step "2/4 — Empty S3 buckets (lose all data)"

empty_bucket() {
  local b="$1"
  if ! aws s3api head-bucket \
         --bucket "$b" \
         --profile "$AWS_PROFILE" \
         --region "$AWS_REGION" \
         >/dev/null 2>&1; then
    ok "Bucket $b absent (skip)"
    return 0
  fi
  echo "  Emptying s3://$b ..."
  aws s3 rm "s3://$b" --recursive \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    >/dev/null \
    || warn "rm --recursive on $b non-zero"

  # CFN bucket has versioning off + a 30-day
  # noncurrent-version expiration, but be defensive
  # in case versions exist anyway.
  VERSIONS_JSON=$(
    aws s3api list-object-versions \
      --bucket "$b" \
      --profile "$AWS_PROFILE" \
      --region "$AWS_REGION" \
      --output json 2>/dev/null \
      || echo '{}'
  )
  TO_DELETE=$(printf '%s' "$VERSIONS_JSON" \
    | python3 -c '
import json, sys
d = json.load(sys.stdin)
items = []
for v in d.get("Versions", []) or []:
  items.append({"Key": v["Key"],
                "VersionId": v["VersionId"]})
for v in d.get("DeleteMarkers", []) or []:
  items.append({"Key": v["Key"],
                "VersionId": v["VersionId"]})
print(json.dumps({"Objects": items,
                  "Quiet": True}))')
  N=$(printf '%s' "$TO_DELETE" | python3 -c \
    'import json,sys;\
print(len(json.load(sys.stdin)["Objects"]))')
  if [[ "$N" -gt 0 ]]; then
    echo "  Deleting $N versioned objects..."
    aws s3api delete-objects \
      --bucket "$b" \
      --profile "$AWS_PROFILE" \
      --region "$AWS_REGION" \
      --delete "$TO_DELETE" \
      >/dev/null \
      || warn "delete-objects on $b non-zero"
  fi
  ok "Bucket $b emptied"
}

empty_bucket "$CUR_BUCKET"
empty_bucket "$ATHENA_BUCKET"

# ── 3. Delete CFN stack + retained buckets ──────────
step "3/4 — Removing cost reporting"

if aws cloudformation describe-stacks \
     --stack-name "$STACK_NAME" \
     --profile "$AWS_PROFILE" \
     --region "$AWS_REGION" \
     >/dev/null 2>&1; then
  # Pre-purge Athena workgroup (older stacks lack
  # RecursiveDeleteOption; CFN's delete refuses with
  # "WorkGroup ... is not empty" once any query has
  # run). NotFound here is fine — CFN treats already-
  # deleted resources as success.
  WG="${ATHENA_WORKGROUP:-tg-cur-analytics}"
  if aws athena get-work-group \
       --work-group "$WG" \
       --profile "$AWS_PROFILE" \
       --region "$AWS_REGION" \
       >/dev/null 2>&1; then
    aws athena delete-work-group \
      --work-group "$WG" \
      --recursive-delete-option \
      --profile "$AWS_PROFILE" \
      --region "$AWS_REGION" \
      >/dev/null 2>&1 \
      && ok "Athena workgroup $WG purged" \
      || warn "could not purge workgroup $WG"
  fi
  echo "  Deleting stack: $STACK_NAME"
  aws cloudformation delete-stack \
    --stack-name "$STACK_NAME" \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION"
  echo "  Waiting for $STACK_NAME delete..."
  aws cloudformation wait \
    stack-delete-complete \
    --stack-name "$STACK_NAME" \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    || fail "Stack $STACK_NAME did not delete"
  ok "Stack $STACK_NAME deleted"
else
  ok "Stack $STACK_NAME absent (skip)"
fi

# Both S3 buckets have DeletionPolicy: Retain on
# the template, so CFN leaves them behind even on
# stack delete. Drop them now (they're empty).
delete_bucket() {
  local b="$1"
  if ! aws s3api head-bucket \
         --bucket "$b" \
         --profile "$AWS_PROFILE" \
         --region "$AWS_REGION" \
         >/dev/null 2>&1; then
    ok "Bucket $b already gone (skip)"
    return 0
  fi
  aws s3api delete-bucket \
    --bucket "$b" \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    >/dev/null \
    || warn "delete-bucket $b non-zero"
  ok "Bucket $b deleted"
}

delete_bucket "$CUR_BUCKET"
delete_bucket "$ATHENA_BUCKET"

# ── 4. Verify clean slate ───────────────────────────
step "4/4 — Verify clean slate"

ERRORS=()

# CFN stack must be gone.
if aws cloudformation describe-stacks \
     --stack-name "$STACK_NAME" \
     --profile "$AWS_PROFILE" \
     --region "$AWS_REGION" \
     >/dev/null 2>&1; then
  ERRORS+=("residual CFN stack: $STACK_NAME")
else
  ok "Stack $STACK_NAME gone"
fi

# S3 buckets matching tg-cur-* and tg-athena-results-*
S3_LIST=$(
  aws s3api list-buckets \
    --profile "$AWS_PROFILE" \
    --query 'Buckets[].Name' \
    --output text 2>/dev/null \
    || true
)
RESIDUAL_S3=""
for b in $S3_LIST; do
  case "$b" in
    tg-cur-*|tg-athena-results-*)
      RESIDUAL_S3+="$b "
      ;;
  esac
done
if [[ -n "${RESIDUAL_S3// /}" ]]; then
  ERRORS+=("residual S3 buckets: $RESIDUAL_S3")
else
  ok "No tg-cur-* / tg-athena-results-* buckets"
fi

# Athena workgroup
WG_HIT=$(
  aws athena list-work-groups \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --query \
    "WorkGroups[?Name=='tg-cur-analytics'].Name" \
    --output text 2>/dev/null \
    || true
)
if [[ -n "${WG_HIT// /}" ]]; then
  ERRORS+=("residual cost-report query workgroup: $WG_HIT")
else
  ok "Cost-report query setup removed"
fi

# Glue crawler — #591 stopped creating it (static table now),
# but keep this residue check so tearing down an OLDER stack
# that still has tg-cur-crawler confirms it's gone post-delete.
CR_HIT=$(
  aws glue get-crawler \
    --name tg-cur-crawler \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --query 'Crawler.Name' \
    --output text 2>/dev/null \
    || true
)
if [[ -n "${CR_HIT// /}" \
      && "$CR_HIT" != "None" ]]; then
  ERRORS+=("residual old cost-report table scanner: $CR_HIT")
else
  ok "No old cost-report table scanner left"
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
ok "Cost reporting fully removed from \
account $TG_TARGET_ACCOUNT_ID."
echo "Re-run scripts/tg-cur-deploy.sh to redeploy."
