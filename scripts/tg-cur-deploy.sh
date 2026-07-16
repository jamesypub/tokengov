#!/usr/bin/env bash
# tg-cur-deploy.sh
#
# Idempotent deploy of the tg-cur-athena CFN stack:
# CUR 2.0 export, raw + results S3 buckets, Glue
# database + STATIC table (partition projection, no
# crawler — #591), Athena workgroup, and saved queries.
# (The api queries CUR under its own task role tg-app (#590);
# there is no separate admin-role CUR policy after #591/#566D.)
#
# The CUR stack is INDEPENDENT of tg-bedrock-role and
# tg-container-stack — deploy this before or after
# scripts/tg-local-install.sh / tg-ecs-install.sh.
#
# Required env vars:
#   AWS_PROFILE          deploy profile (e.g.
#                        tg-install-<account>)
#   TG_TARGET_ACCOUNT_ID    12-digit account; must match
#                        caller's account
#
# Optional:
#   AWS_REGION           default us-east-1
#                        (CUR 2.0 only operates in
#                        us-east-1 — forced)
#   STACK_NAME           default tg-cur-athena
#   EXPORT_NAME          default tg-bedrock-cur
#   GLUE_DATABASE        default tg_cur
#   ATHENA_WORKGROUP     default tg-cur-analytics
#   CUR_TABLE_NAME       default 'data' (BCM CUR 2.0
#                        leaf folder; rarely overridden)
#
# Re-running after a successful run is a no-op:
#   `aws cloudformation deploy` is idempotent.
#
# Usage:
#   AWS_PROFILE=tg-install-<account> \
#     TG_TARGET_ACCOUNT_ID=<12-digit account> \
#     scripts/tg-cur-deploy.sh

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

# Honor TG_ENV (dev/stage). With TG_ENV unset, defaults
# preserve legacy single-env behavior. See #145.
# shellcheck source=tg-env.sh
. "$REPO_ROOT/scripts/tg-env.sh"

# ── 0. Profile preflight — runs FIRST (#1087) ────────
# AWS_PROFILE is OPTIONAL (the #768 pattern, now consistent with
# tg-ecs-install.sh): pin it when set, else use the default credential
# chain. Resolve + validate the session up front so a stale/absent
# login fails here with clear remediation, not a cryptic mid-deploy
# error. The single read-only get-caller-identity is the universal
# liveness probe for every credential type (SSO / env creds / instance
# role) — #1087 OQ1/OQ2.
step "Resolving AWS credentials"
if [[ -n "${AWS_PROFILE:-}" ]]; then
  PROFILE_ARGS=(--profile "$AWS_PROFILE")
  CRED_SRC="profile $AWS_PROFILE"
else
  PROFILE_ARGS=()
  CRED_SRC="default credential chain (instance role / SSO default \
/ env creds)"
  warn "AWS_PROFILE not set — using the $CRED_SRC. Export \
AWS_PROFILE=tg-install-<account> to pin a named profile."
fi
ok "Using AWS credentials: $CRED_SRC"

# SSO detection (best-effort): an SSO profile carries sso_session or
# sso_start_url. A role-chained profile may reach SSO via
# source_profile and not report here — the liveness probe below still
# catches an expired session; only the SSO-specific message is skipped.
_SSO_HINT=$(aws configure get sso_session "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
  2>/dev/null || aws configure get sso_start_url "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
  2>/dev/null || true)

if ! aws sts get-caller-identity "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
     >/dev/null 2>&1; then
  if [[ -n "$_SSO_HINT" ]]; then
    fail "AWS SSO session is not active for \
${AWS_PROFILE:-the default profile}. Run: \
aws sso login${AWS_PROFILE:+ --profile $AWS_PROFILE}, then re-run."
  else
    fail "AWS credentials for ${AWS_PROFILE:-the default chain} are \
invalid or expired (aws sts get-caller-identity failed)."
  fi
fi

# ── 1. Validate required env vars ────────────────────
step "Validating environment"

: "${TG_TARGET_ACCOUNT_ID:?\
must export TG_TARGET_ACCOUNT_ID=<12-digit account>\
}"

# CUR 2.0 only operates in us-east-1.
REQ_REGION="${AWS_REGION:-us-east-1}"
if [[ "$REQ_REGION" != "us-east-1" ]]; then
  warn "AWS_REGION=$REQ_REGION but CUR 2.0 only \
runs in us-east-1; forcing us-east-1"
fi
export AWS_REGION="us-east-1"

STACK_NAME="${STACK_NAME:-tg-cur-athena}"
EXPORT_NAME="${EXPORT_NAME:-tg-bedrock-cur}"
GLUE_DATABASE="${GLUE_DATABASE:-tg_cur}"
ATHENA_WORKGROUP="${ATHENA_WORKGROUP:-tg-cur-analytics}"
CUR_TABLE_NAME="${CUR_TABLE_NAME:-data}"

if ! [[ "$TG_TARGET_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  fail "TG_TARGET_ACCOUNT_ID must be 12 digits"
fi

ok "AWS credentials    = $CRED_SRC"
ok "AWS_REGION         = $AWS_REGION"
ok "AWS account        = $TG_TARGET_ACCOUNT_ID"

# ── 2. Pre-flight checks ─────────────────────────────
step "Pre-flight checks"

# aws CLI v2
AWSCLI_VER=$(aws --version 2>&1 | head -1)
if [[ "$AWSCLI_VER" != aws-cli/2.* ]]; then
  fail "aws CLI v2 required (got $AWSCLI_VER)"
fi
ok "$AWSCLI_VER"

# Caller identity must match TG_TARGET_ACCOUNT_ID
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

# Template file present
TPL="cfn/tg-cur-athena.yaml"
[[ -f "$TPL" ]] || fail "template not found: $TPL"
ok "template: $TPL"

# #591 (#566D): no admin-role CUR policy anymore. tg-BedrockAdmin
# is deleted (desktop gone, #574/#576); #590: the api queries CUR
# under its own task role tg-app (perms inline in
# tg-container-stack.yaml). The AdminRoleName param + validation
# are removed.

# ── 3. CUR data lag notice (plain, customer-facing) ──
step "Cost data appears within ~48 hours"

cat <<'EOF'
Your AWS cost data won't show up right away — AWS
publishes it within about 24-48 hours (after Bedrock
usage occurs). It then appears automatically in the
admin Cost Reports page; there's nothing else to do.

To check whether data has arrived yet, run:
  scripts/verify-cur.sh
EOF

# Helper: empty + delete a bucket if it exists. Used to
# clean up Retain-policy buckets that CFN leaves behind
# when the stack is deleted (or that orphan from a
# half-failed prior deploy). NotFound is fine.
drop_bucket_if_orphan() {
  local b="$1"
  if ! aws s3api head-bucket \
         --bucket "$b" \
         "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
         >/dev/null 2>&1; then
    return 0
  fi
  aws s3 rm "s3://$b" --recursive \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    >/dev/null 2>&1 || true
  # Versioned objects + delete markers
  aws s3api list-object-versions \
    --bucket "$b" \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    --output json 2>/dev/null \
    | python3 -c '
import sys, json
d = json.load(sys.stdin)
keys = []
for v in d.get("Versions", []) or []:
    keys.append((v["Key"], v["VersionId"]))
for v in d.get("DeleteMarkers", []) or []:
    keys.append((v["Key"], v["VersionId"]))
for k, vid in keys:
    print(f"{k}\t{vid}")
' 2>/dev/null \
    | while IFS=$'\t' read -r K V; do
        [[ -z "$K" ]] && continue
        aws s3api delete-object \
          --bucket "$b" \
          --key "$K" \
          --version-id "$V" \
          "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
          --region "$AWS_REGION" \
          >/dev/null 2>&1 || true
      done
  aws s3api delete-bucket \
    --bucket "$b" \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    >/dev/null 2>&1 \
    && ok "orphan bucket $b dropped" \
    || warn "could not drop $b"
}

# #591 upgrade-path guard:
# The retired Glue crawler created the CUR table out-of-band
# (CreatedBy .../AWS-Crawler) — it is NOT a CFN-managed
# resource, so CFN can't drop it when we delete the crawler.
# On a crawler-era stack, CFN's CreateTable for the new static
# AWS::Glue::Table (same name) hits AlreadyExistsException →
# UPDATE_ROLLBACK ("Table already exists"). Drop the crawler-
# created table here, before deploy, so CFN creates the static
# one cleanly.
#
# Idempotent + safe (the CreatedBy guard is the discriminator):
#   - fresh install (no table / no db)    → no-op
#   - re-run (CFN-managed table present)  → CreatedBy lacks
#     'AWS-Crawler' → left untouched (deploy is a no-op cs)
#   - crawler-era upgrade                 → dropped
#
# Mirrors the #522 decision to keep Glue lifecycle ops in THIS
# script (a direct CLI call under the operator's creds) rather
# than a custom-resource Lambda — that Lambda wrapper was the
# sole source of the #517/#521 custom-resource rollbacks.
drop_crawler_table_if_orphan() {
  local db="$1" tbl="$2"
  local created_by
  created_by=$(aws glue get-table \
    --database-name "$db" \
    --name "$tbl" \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    --query 'Table.CreatedBy' \
    --output text 2>/dev/null || echo "")
  if [[ -z "$created_by" || "$created_by" == "None" ]]; then
    # No table (fresh install / db not yet created) — nothing
    # to drop; CFN will create both the db and the table.
    return 0
  fi
  if [[ "$created_by" != *AWS-Crawler* ]]; then
    # CFN-managed (or otherwise non-crawler) table — leave it
    # so an idempotent re-run doesn't churn the static table.
    return 0   # not an old leftover table — keep it (silent)
  fi
  warn "Cleaning up an old cost-report table from a previous setup"
  aws glue delete-table \
    --database-name "$db" \
    --name "$tbl" \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    >/dev/null 2>&1 \
    && ok "Old cost-report table cleaned up" \
    || warn "Could not clean up an old cost-report table \
(setup may need a re-run)"
}

CUR_BUCKET_DEFAULT="tg-cur-${TG_TARGET_ACCOUNT_ID}-${AWS_REGION}"
ATH_BUCKET_DEFAULT="tg-athena-results-${TG_TARGET_ACCOUNT_ID}-${AWS_REGION}"

# ── 3b. Detect → validate → reuse-or-create CUR 2.0 ──
# tg's spend + principal-discovery source is a CUR 2.0 export with
# INCLUDE_IAM_PRINCIPAL_DATA (#714/#720). A customer may ALREADY have a
# usable export under a different name; rather than always create a
# second (redundant, billed) export, discover every export in the
# account, validate which are reuse-worthy, and let the operator choose
# reuse-vs-create. A foreign (customer-owned) export is NEVER deleted —
# the self-heal delete is gated to tg's OWN export name only.
#
# The pure classify/decide/safety logic lives in lib/tg-cur-detect.sh
# (AWS-free, unit-tested in scripts/ci/tg-cur-detect-test.sh); this
# block supplies the AWS data and acts on the decision.
. "$(dirname "$0")/lib/tg-cur-detect.sh"

step "Discovering CUR 2.0 exports"

# Decision outputs threaded into the CFN deploy below:
#   CREATE_EXPORT        'true'|'false'  → CreateExport CFN param
#   REUSED_CUR_S3_URL    s3://.../data/  → ReusedCurS3Url (reuse only)
#   REUSED_CUR_BUCKET    bucket name     → app-role grant (reuse only)
CREATE_EXPORT="true"
REUSED_CUR_S3_URL=""
REUSED_CUR_BUCKET=""

# Enumerate every export ARN (ExportName/Name spelling varies by SDK).
# A while-read loop, NOT the Bash-4 array-read builtin — that builtin
# is absent on macOS's stock Bash 3.2, where the install died at this
# step. Process substitution + while-read is Bash-3.2-safe and yields
# the identical _export_arns contents.
_export_arns=()
while IFS= read -r _ln; do
  _export_arns+=("$_ln")
done < <(
  aws bcm-data-exports list-exports \
    --region "$AWS_REGION" "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --query 'Exports[].ExportArn' --output text 2>/dev/null \
    | tr '\t' '\n' | grep -v '^$' || true)

# Per-export: GetExport, classify, collect valid + invalid candidates.
VALID_NAMES=(); VALID_ARNS=(); VALID_URLS=(); VALID_BUCKETS=()
INVALID_NAMES=(); INVALID_REASONS=()
TG_OWN_ARN=""; TG_OWN_HAS_IAM=""
for _arn in "${_export_arns[@]+"${_export_arns[@]}"}"; do
  [[ -z "$_arn" || "$_arn" == "None" ]] && continue
  _ex=$(aws bcm-data-exports get-export \
    --export-arn "$_arn" \
    --region "$AWS_REGION" "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --output json 2>/dev/null || echo '{}')
  _name=$(printf '%s' "$_ex" | python3 -c \
    'import sys,json;e=json.load(sys.stdin).get("Export",{});print(e.get("Name",""))' \
    2>/dev/null || echo "")
  [[ -z "$_name" ]] && continue
  # Pull the classify inputs via one python pass over the GetExport
  # JSON (jq isn't guaranteed on the install host; python3 is).
  read -r _is_cur2 _iam _res _s3region _s3bucket _s3prefix < <(
    printf '%s' "$_ex" | python3 -c '
import sys, json
e = json.load(sys.stdin).get("Export", {})
tc = (e.get("DataQuery", {}).get("TableConfigurations", {})
      .get("COST_AND_USAGE_REPORT", {}))
is_cur2 = "TRUE" if tc else "FALSE"
iam = tc.get("INCLUDE_IAM_PRINCIPAL_DATA", "FALSE")
res = tc.get("INCLUDE_RESOURCES", "FALSE")
s3 = (e.get("DestinationConfigurations", {})
      .get("S3Destination", {}))
print(is_cur2, iam, res,
      s3.get("S3Region", "") or "None",
      s3.get("S3Bucket", "") or "None",
      s3.get("S3Prefix", "") or "None")
' 2>/dev/null || echo "FALSE FALSE FALSE None None None")

  _verdict=$(tg_cur_classify "$_is_cur2" "$_iam" "$_res" \
    "$_s3region" "$AWS_REGION")

  if [[ "$_name" == "$EXPORT_NAME" ]]; then
    # tg's OWN export — track it separately, NEVER as a reuse
    # candidate. The reuse path sets CreateExport=false and the
    # CFN CurExport resource is conditioned on CreateExport=true,
    # so treating tg's own export as reuse-and-drop deletes the very
    # export tg manages, leaving the account export-less on an
    # idempotent redeploy. tg's own export means keep-managing /
    # keep-creating: CreateExport stays true (see below). Only
    # GENUINE foreign exports are reuse candidates.
    TG_OWN_ARN="$_arn"; TG_OWN_HAS_IAM="$_iam"
    continue
  fi

  if [[ "$_verdict" == "valid" ]]; then
    # CUR 2.0 lands parquet under <prefix>/<export>/<export>/data/.
    _url="s3://${_s3bucket}/${_s3prefix}/${_name}/${_name}/data/"
    VALID_NAMES+=("$_name"); VALID_ARNS+=("$_arn")
    VALID_URLS+=("$_url");   VALID_BUCKETS+=("$_s3bucket")
  else
    INVALID_NAMES+=("$_name")
    INVALID_REASONS+=("${_verdict#invalid:}")
  fi
done

# tg already owns an export → keep managing it (keep-creating); do NOT
# offer reuse-and-drop. This is the load-bearing fix for the redeploy
# regression: on an idempotent redeploy of a tg-set-up account, tg's own
# export must stay on the CreateExport=true path so the conditioned CFN
# CurExport resource is never dropped, leaving the account export-less.
# A GENUINE foreign export is still offered for reuse below (tg's own
# export was excluded from the VALID_* candidate set above).
if [[ -n "$TG_OWN_ARN" ]]; then
  CREATE_EXPORT="true"

  # Self-heal (tg's OWN export only — NEVER a foreign one): a
  # tg-bedrock-cur export missing the creation-only IAM-principal flag
  # is deleted so CFN recreates it correctly with the flag set. This is
  # delete-then-recreate, and the recreate is GUARANTEED because we just
  # forced CREATE_EXPORT=true (the CurExport resource will be created) —
  # so the account is never left export-less by this delete. A valid
  # tg-owned export (flag present) is kept as-is: CFN re-asserts it
  # in place, no delete.
  if [[ "$TG_OWN_HAS_IAM" != "TRUE" ]] && \
     [[ "$(tg_cur_should_delete_export "$EXPORT_NAME" \
            "$EXPORT_NAME")" == "yes" ]]; then
    warn "tg-owned export $EXPORT_NAME lacks \
INCLUDE_IAM_PRINCIPAL_DATA — deleting so CFN recreates it."
    aws bcm-data-exports delete-export \
      --export-arn "$TG_OWN_ARN" \
      --region "$AWS_REGION" "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}"
    ok "Old tg-owned export deleted; CFN will recreate it"
    sleep 5
  else
    ok "Keeping tg's own export $EXPORT_NAME (managed by CFN)"
  fi

# No tg-owned export. Decide: prompt when a valid FOREIGN reuse candidate
# exists; else explain an invalid one and offer create-only; else create
# silently (today).
elif [[ "${#VALID_NAMES[@]}" -gt 0 ]]; then
  _chosen=0
  echo "Found an existing CUR 2.0 export usable by tg:"
  printf "  name : %s\n" "${VALID_NAMES[$_chosen]}"
  printf "  s3   : %s\n" "${VALID_URLS[$_chosen]}"
  printf "  iam-principal data : yes\n"
  echo "Reuse it, or create tg's own export?"
  printf "  [R] reuse %s   [C] create %s\n" \
    "${VALID_NAMES[$_chosen]}" "$EXPORT_NAME"
  _decision="invalid"
  while [[ "$_decision" == "invalid" ]]; do
    # #1119: the wizard hoists this decision into its Q&A and passes
    # TG_CUR_DECISION=reuse|create so the deploy runs non-interactively
    # (no mid-install stdin block / invisible-prompt hang). Honor the
    # flag first; only fall back to the interactive prompt / safe
    # create-default for a STANDALONE `bash tg-cur-deploy.sh` run.
    if [[ "${TG_CUR_DECISION:-}" == "reuse" ]]; then
      _ans="R"
    elif [[ "${TG_CUR_DECISION:-}" == "create" ]]; then
      _ans="C"
    elif [[ -t 0 ]]; then
      read -r -p "  choice [R/C]: " _ans || _ans=""
    else
      # Non-interactive with no flag (e.g. a headless standalone run):
      # default to CREATE so it's never blocked and never silently
      # attaches to a customer's export.
      warn "non-interactive — defaulting to create $EXPORT_NAME"
      _ans="C"
    fi
    _decision=$(tg_cur_decide "${#VALID_NAMES[@]}" "$_ans")
    [[ "$_decision" == "invalid" ]] && \
      echo "  please answer R (reuse) or C (create)."
  done
  case "$_decision" in
    reuse)
      CREATE_EXPORT="false"
      REUSED_CUR_S3_URL="${VALID_URLS[$_chosen]}"
      REUSED_CUR_BUCKET="${VALID_BUCKETS[$_chosen]}"
      ok "Reusing export ${VALID_NAMES[$_chosen]}" ;;
    create)
      ok "Creating tg's own export $EXPORT_NAME" ;;
  esac
elif [[ "${#INVALID_NAMES[@]}" -gt 0 ]]; then
  _why=$(tg_cur_reason_text "${INVALID_REASONS[0]}")
  echo "An existing export (${INVALID_NAMES[0]}) can't be reused:"
  echo "  $_why."
  echo "tg will create its own export instead."
  if [[ -t 0 ]]; then
    read -r -p "  [C] create $EXPORT_NAME  [A] abort: " _ans \
      || _ans="A"
  else
    _ans="C"
  fi
  _decision=$(tg_cur_decide 0 "$_ans")
  case "$_decision" in
    abort) fail "aborted by operator" ;;
    *)     ok "Creating tg's own export $EXPORT_NAME" ;;
  esac
else
  ok "No existing export — CFN will create one"
fi

# ── 4. Deploy CFN stack ──────────────────────────────
step "Setting up cost reporting"

# Self-heal a broken stack. CFN can land in a state
# `aws cloudformation deploy` can't recover from when
# resources drift (e.g. someone deletes the Glue DB
# out-of-band, then CFN's UPDATE 404s and rolls back).
# Both buckets have DeletionPolicy: Retain, so deleting
# the stack is non-destructive — but CFN can't recreate
# over Retain'd buckets either, so we drop them too.
STACK_STATUS=$(
  aws cloudformation describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null \
  || echo "ABSENT"
)
case "$STACK_STATUS" in
  ROLLBACK_COMPLETE \
  | UPDATE_ROLLBACK_COMPLETE \
  | UPDATE_ROLLBACK_FAILED \
  | CREATE_FAILED \
  | DELETE_FAILED \
  | REVIEW_IN_PROGRESS)
    warn "stack in $STACK_STATUS — deleting"

    # Pre-clear blockers. AWS::Athena::WorkGroup refuses
    # to delete when it has any query history; the
    # newer template sets RecursiveDeleteOption, but
    # stacks predating that fix don't have it set.
    # Forcibly purge the workgroup ourselves; CFN's
    # delete will then succeed (NotFound is treated
    # as already-deleted by CFN).
    if aws athena get-work-group \
         --work-group "$ATHENA_WORKGROUP" \
         "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
         --region "$AWS_REGION" \
         >/dev/null 2>&1; then
      aws athena delete-work-group \
        --work-group "$ATHENA_WORKGROUP" \
        --recursive-delete-option \
        "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
        --region "$AWS_REGION" \
        >/dev/null 2>&1 \
        && ok "athena workgroup purged" \
        || warn "could not purge workgroup; \
delete may fail"
    fi

    aws cloudformation delete-stack \
      "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
      --region "$AWS_REGION" \
      --stack-name "$STACK_NAME"
    aws cloudformation wait \
      stack-delete-complete \
      "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
      --region "$AWS_REGION" \
      --stack-name "$STACK_NAME" \
      || fail "$STACK_NAME failed to delete"
    ok "stack deleted; recreating fresh"
    drop_bucket_if_orphan "$CUR_BUCKET_DEFAULT"
    drop_bucket_if_orphan "$ATH_BUCKET_DEFAULT"
    ;;
  ABSENT)
    # Stack is gone but Retain'd buckets may still be
    # around from a previous run. CFN's CREATE will 400
    # ("AlreadyExists") if so. Drop them.
    drop_bucket_if_orphan "$CUR_BUCKET_DEFAULT"
    drop_bucket_if_orphan "$ATH_BUCKET_DEFAULT"
    ;;
esac

# #591 upgrade-path guard: drop the crawler-created CUR table
# (if any) so CFN's static AWS::Glue::Table create doesn't hit
# AlreadyExistsException on a crawler-era stack. No-op on a
# fresh install or a CFN-managed-table re-run.
drop_crawler_table_if_orphan "$GLUE_DATABASE" "$CUR_TABLE_NAME"

aws cloudformation deploy \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --template-file "$TPL" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "ExportName=$EXPORT_NAME" \
    "GlueDatabaseName=$GLUE_DATABASE" \
    "AthenaWorkgroupName=$ATHENA_WORKGROUP" \
    "CurTableName=$CUR_TABLE_NAME" \
    "CreateExport=$CREATE_EXPORT" \
    "ReusedCurS3Url=$REUSED_CUR_S3_URL"
ok "$STACK_NAME deployed"

# Reuse path: the CUR data lives in a customer-owned bucket, so the
# app task role (tg-app, tg-container-stack) needs read on it. Surface
# the bucket ARN for the container deploy's ReusedCurBucketArn param;
# tg-ecs-install.sh / tg-local-install.sh pass it through. (Recorded
# here so the install orchestrator can thread it; empty on create.)
if [[ "$CREATE_EXPORT" == "false" && -n "$REUSED_CUR_BUCKET" ]]; then
  REUSED_CUR_BUCKET_ARN="arn:aws:s3:::${REUSED_CUR_BUCKET}"
  export TG_REUSED_CUR_BUCKET_ARN="$REUSED_CUR_BUCKET_ARN"
  ok "Reuse: app task role must read $REUSED_CUR_BUCKET_ARN"
  ok "(pass ReusedCurBucketArn=$REUSED_CUR_BUCKET_ARN to \
tg-container-stack)"
fi

# ── 5. Read stack outputs ────────────────────────────
step "Finishing cost-reporting setup"

read_output() {
  local key="$1"
  aws cloudformation describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --query \
    "Stacks[0].Outputs[?OutputKey=='${key}']\
.OutputValue | [0]" \
    --output text
}

CUR_BUCKET=$(read_output CurBucketName)
ATHENA_RESULTS=$(read_output AthenaResultsBucketName)
WORKGROUP=$(read_output AthenaWorkgroupName)
DATABASE=$(read_output GlueDatabaseName)

[[ -n "$CUR_BUCKET" && "$CUR_BUCKET" != "None" ]] \
  || fail "CurBucketName output missing"
[[ -n "$ATHENA_RESULTS" \
  && "$ATHENA_RESULTS" != "None" ]] \
  || fail "AthenaResultsBucketName output missing"

ok "Cost-data bucket : $CUR_BUCKET"
ok "Cost reporting configured"

# #591 (#566E): no crawler to kick. The CUR data is a STATIC Glue
# table with partition projection (cfn/tg-cur-athena.yaml) — new
# billing months resolve at query time, so there's nothing to
# discover post-deploy.

# ── 6. Write env vars file ───────────────────────────
step "Writing .tg-admin.env"

ENV_FILE="$REPO_ROOT/.tg-admin.env"
cat > "$ENV_FILE" <<EOF
# Source before launching tg-admin to enable CUR
# + Athena features:
#   source $ENV_FILE
#   AWS_PROFILE=tg-admin tg-admin ui
export CC_CUR_S3_BUCKET=$CUR_BUCKET
export CC_ATHENA_WORKGROUP=$WORKGROUP
export CC_ATHENA_DATABASE=$DATABASE
EOF
ok "Wrote env vars to $ENV_FILE"

# ── 6b. Heal the docker stack's .env.tg ──────────────
# If tg-local-install.sh ran BEFORE this script
# (the common BF2-then-BF5 ordering in tg-test.sh
# --browser-full), the api/worker containers were
# started with ATHENA_WORKGROUP=None / ATHENA_DATABASE=
# None / ATHENA_RESULTS_BUCKET=s3://None/. Patch
# .env.tg in place + recreate api+worker so the
# running stack picks up the real Athena values.
# No-op when .env.tg is absent (standalone CUR
# deploy) or when compose isn't running.
DOCKER_ENV="$REPO_ROOT/$TG_ENV_FILE"
if [[ -s "$DOCKER_ENV" ]] \
   && grep -q '^ATHENA_WORKGROUP=' "$DOCKER_ENV"; then
  step "Connecting the app to cost reporting"
  python3 - "$DOCKER_ENV" \
    "$CUR_BUCKET" "$WORKGROUP" "$DATABASE" \
    "$ATHENA_RESULTS" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
cur, wg, db, ath = sys.argv[2:6]
new = {
  "ATHENA_RESULTS_BUCKET": f"s3://{ath}/",
  "ATHENA_DATABASE":       db,
  "ATHENA_WORKGROUP":      wg,
}
out = []
seen = set()
for line in p.read_text().splitlines():
  k = line.split("=", 1)[0] if "=" in line else ""
  if k in new:
    out.append(f"{k}={new[k]}")
    seen.add(k)
  else:
    out.append(line)
for k, v in new.items():
  if k not in seen:
    out.append(f"{k}={v}")
p.write_text("\n".join(out) + "\n")
PY
  ok ".env.tg ATHENA_* refreshed"

  # Bounce api + worker if compose is up so they
  # re-read the new env. force-recreate ensures the
  # env propagates (compose only re-reads .env at
  # container create, not start).
  if command -v docker >/dev/null 2>&1 \
     && docker compose ps --status running \
          --services 2>/dev/null \
          | grep -qE '^(api|worker)$'; then
    if (cd "$REPO_ROOT" && docker compose \
         --env-file "$DOCKER_ENV" \
         up -d --force-recreate --no-deps api worker \
         >/dev/null 2>&1); then
      ok "api + worker recreated with new env"
      # Wait for api to be ready again — caller scripts
      # (e.g. tg-test.sh's BF6 browser run) can hit
      # /api/* immediately after this returns and get
      # ConnectionReset if the container hasn't started.
      for i in 1 2 3 4 5 6 7 8 9 10 11 12 \
               13 14 15 16 17 18 19 20; do
        if curl -fsS \
             http://localhost:8000/api/version \
             >/dev/null 2>&1; then
          ok "api healthy (took ${i}s)"
          break
        fi
        sleep 1
        if [[ "$i" == "20" ]]; then
          warn "api not healthy after 20s — \
downstream tests may flake"
        fi
      done
    else
      warn "compose recreate non-zero — \
restart api/worker manually"
    fi
  else
    ok "compose not running — no restart needed"
  fi
fi

# ── 6b2. Heal the ECS api task definition (#195) ─────
# tg-ecs-install.sh deploys tg-container-stack with the
# four CUR/Athena env vars defaulting to empty/sentinel,
# so the api returns 503 on the Cost Reports page until
# this script reruns the container-stack deploy with the
# real Athena outputs as parameter overrides. No-op if
# the stack isn't deployed (local-compose installs).
TG_ECS_STACK="${TG_ECS_STACK:-tg-container-stack}"
if aws cloudformation describe-stacks \
     "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
     --stack-name "$TG_ECS_STACK" >/dev/null 2>&1; then
  step "Connecting the app to cost reporting"

  # Pull the stack's existing parameters, override the
  # four CUR/Athena ones, leave the rest alone via
  # UsePreviousValue=true.
  existing_params=$(aws cloudformation describe-stacks \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
    --stack-name "$TG_ECS_STACK" \
    --query 'Stacks[0].Parameters[].ParameterKey' \
    --output text)

  cur_overrides=(
    "AthenaResultsBucket=s3://${ATHENA_RESULTS}/"
    "AthenaWorkgroup=${WORKGROUP}"
    "AthenaDatabase=${DATABASE}"
    "CurTableName=${CUR_TABLE_NAME}"
  )

  prev_args=()
  for key in $existing_params; do
    case "$key" in
      AthenaResultsBucket|AthenaWorkgroup|AthenaDatabase|CurTableName)
        : # overridden below
        ;;
      *)
        prev_args+=("ParameterKey=${key},UsePreviousValue=true")
        ;;
    esac
  done

  override_args=()
  for kv in "${cur_overrides[@]}"; do
    k="${kv%%=*}"; v="${kv#*=}"
    override_args+=("ParameterKey=${k},ParameterValue=${v}")
  done

  if aws cloudformation update-stack \
       "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
       --stack-name "$TG_ECS_STACK" \
       --use-previous-template \
       --capabilities CAPABILITY_NAMED_IAM \
       --parameters \
         "${prev_args[@]}" "${override_args[@]}" \
       2>/tmp/tg-cur-update.err >/dev/null; then
    ok "Update kicked off — waiting (max 10 min)"
    if aws cloudformation wait stack-update-complete \
         "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
         --region "$AWS_REGION" \
         --stack-name "$TG_ECS_STACK" 2>/dev/null; then
      ok "App connected to cost reporting"
    else
      warn "Stack update did not complete cleanly — \
verify in console"
    fi

    # Force a new deployment so running tasks pick up the
    # updated task definition immediately.
    for svc in tg-api-service tg-worker-service; do
      aws ecs update-service \
        "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$AWS_REGION" \
        --cluster tg-cluster \
        --service "$svc" \
        --force-new-deployment >/dev/null 2>&1 \
        && ok "Forced new deployment: $svc" \
        || warn "force-new-deployment failed: $svc"
    done
  else
    if grep -q "No updates" /tmp/tg-cur-update.err; then
      ok "$TG_ECS_STACK already has correct env vars"
    else
      warn "Stack update failed:"
      cat /tmp/tg-cur-update.err >&2
    fi
  fi
else
  ok "$TG_ECS_STACK not deployed — skipping ECS patch"
fi

# ── 6c. Verify the static Glue table exists ──────────
# #591 (#566E): no crawler. The static table
# (cfn/tg-cur-athena.yaml) is created by the stack itself, so it
# exists the moment this deploy completes — verify it directly
# (no crawl wait). Rows appear once AWS Billing delivers the
# first CUR file (24-48h); partition projection then resolves
# each month at query time with no further action.
step "Verifying cost-reporting setup"
tables=$(aws glue get-tables \
  --database-name "$DATABASE" \
  --region "$AWS_REGION" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
  --query 'TableList[].Name' \
  --output text 2>/dev/null || echo "")
if echo "$tables" | grep -qw "$CUR_TABLE_NAME"; then
  ok "Cost reporting ready"
else
  warn "Cost reporting isn't fully ready yet — re-run"
  warn "scripts/tg-cur-deploy.sh, or check the cost-reporting"
  warn "setup in your AWS account if this persists."
fi

# ── 7. Final summary ─────────────────────────────────
step "Done"

cat <<'EOF'

✓ Cost reporting is set up.

What happens next:
  1. AWS publishes the first billing data within
     24-48 hours (after Bedrock usage occurs).
  2. Cost data then appears automatically in the
     admin Cost Reports page — no further action.
  3. To confirm data has landed, run:
       scripts/verify-cur.sh

Tear down:
  scripts/tg-cur-destroy.sh
EOF
