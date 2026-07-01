#!/usr/bin/env bash
# tg-cur-detect-test.sh — no-AWS, no-deploy unit tests for the CUR 2.0
# detect → classify → decide logic (lib/tg-cur-detect.sh) plus a
# rendered-template assertion that tg-cur-athena.yaml's CreateExport /
# ReuseExternalCur conditions wire the Export resource + Glue Location
# correctly. Mirrors tg-ecs-install-sso-trust-test.sh's two-layer shape.
#
# Usage: bash scripts/ci/tg-cur-detect-test.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LIB="$HERE/../lib/tg-cur-detect.sh"
DEPLOY="$HERE/../tg-cur-deploy.sh"
TPL="$HERE/../../cfn/tg-cur-athena.yaml"
CSTPL="$HERE/../../cfn/tg-container-stack.yaml"
fails=0
pass() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1" >&2; fails=$((fails + 1)); }
eq()   { [[ "$2" == "$3" ]] && pass "$1" || \
         fail "$1 (got '$2' want '$3')"; }

# shellcheck source=/dev/null
. "$LIB"

echo "== A. classify: reuse-worthiness predicate =="
eq "all-good → valid" \
  "$(tg_cur_classify TRUE TRUE TRUE us-east-1 us-east-1)" "valid"
eq "not CUR 2.0 → invalid:not_cur2" \
  "$(tg_cur_classify FALSE TRUE TRUE us-east-1 us-east-1)" \
  "invalid:not_cur2"
eq "no IAM principal → invalid:no_iam_principal" \
  "$(tg_cur_classify TRUE FALSE TRUE us-east-1 us-east-1)" \
  "invalid:no_iam_principal"
eq "no resources → invalid:no_resources" \
  "$(tg_cur_classify TRUE TRUE FALSE us-east-1 us-east-1)" \
  "invalid:no_resources"
eq "blank S3 region → invalid:no_s3" \
  "$(tg_cur_classify TRUE TRUE TRUE None us-east-1)" \
  "invalid:no_s3"
eq "region mismatch → invalid:region_mismatch" \
  "$(tg_cur_classify TRUE TRUE TRUE us-west-2 us-east-1)" \
  "invalid:region_mismatch"
# IAM-principal reason takes precedence over a later resources defect
# (it's the most common + most useful to surface first).
eq "iam-principal checked before resources" \
  "$(tg_cur_classify TRUE FALSE FALSE us-east-1 us-east-1)" \
  "invalid:no_iam_principal"

echo "== B. decide: prompt answer → decision =="
eq "R with a valid candidate → reuse" \
  "$(tg_cur_decide 1 R)" "reuse"
eq "lowercase r → reuse" \
  "$(tg_cur_decide 2 r)" "reuse"
eq "R with NO valid candidate → invalid (can't reuse nothing)" \
  "$(tg_cur_decide 0 R)" "invalid"
eq "C → create" "$(tg_cur_decide 1 C)" "create"
eq "A → abort" "$(tg_cur_decide 0 A)" "abort"
eq "bare Enter → invalid (never silently reuse — owner rule)" \
  "$(tg_cur_decide 1 '')" "invalid"
eq "garbage → invalid" "$(tg_cur_decide 1 xyz)" "invalid"

echo "== C. safety: self-heal delete is tg-owned-name ONLY =="
eq "tg's own name → may delete" \
  "$(tg_cur_should_delete_export tg-bedrock-cur tg-bedrock-cur)" \
  "yes"
eq "a foreign export name → NEVER delete" \
  "$(tg_cur_should_delete_export acme-cur-prod tg-bedrock-cur)" \
  "no"
eq "empty name → no" \
  "$(tg_cur_should_delete_export '' tg-bedrock-cur)" "no"

echo "== D. reason text present for every invalid reason =="
for r in not_cur2 no_iam_principal no_resources region_mismatch \
         no_s3 unknownfuture; do
  t=$(tg_cur_reason_text "$r")
  [[ -n "$t" ]] && pass "reason text for '$r'" \
    || fail "no reason text for '$r'"
done

echo "== E. installer wiring (static) =="
if bash -n "$DEPLOY"; then pass "tg-cur-deploy.sh bash -n clean"
else fail "tg-cur-deploy.sh bash -n"; fi
# The deploy must pass the two new CFN params.
if grep -qE '"CreateExport=\$CREATE_EXPORT"' "$DEPLOY" \
   && grep -qE '"ReusedCurS3Url=\$REUSED_CUR_S3_URL"' "$DEPLOY"; then
  pass "deploy passes CreateExport + ReusedCurS3Url"
else
  fail "deploy must pass CreateExport + ReusedCurS3Url params"
fi
# The destructive delete-export must be guarded by the
# tg_cur_should_delete_export safety helper.
if grep -qE 'tg_cur_should_delete_export' "$DEPLOY"; then
  pass "delete-export gated by the tg-owned-name safety helper"
else
  fail "delete-export must be gated by tg_cur_should_delete_export"
fi

# Regression guard: tg's OWN export must be kept on the create
# path, NEVER offered as a reuse candidate. The reuse path sets
# CreateExport=false, and the CFN CurExport is conditioned on
# CreateExport=true — so reuse-and-drop of tg's own export deletes it,
# leaving the account export-less on an idempotent redeploy.
#   (a) When the discovered export name == tg's own EXPORT_NAME, the loop
#       must `continue` (skip the VALID_*/INVALID_* candidate push). The
#       `_name == EXPORT_NAME` test is followed by a `continue` before the
#       candidate-collection block.
if awk '/_name" == "\$EXPORT_NAME"/{f=1}
        f&&/continue/{print "found";exit}
        f&&/VALID_NAMES\+=/{exit}' "$DEPLOY" \
     | grep -q found; then
  pass "tg's own export skips the reuse-candidate set (continue)"
else
  fail "tg's own export must NOT be added to reuse candidates"
fi
#   (b) When tg already owns an export (TG_OWN_ARN set), force the create
#       path so the conditioned CurExport is never dropped.
if awk '/if \[\[ -n "\$TG_OWN_ARN" \]\]; then/{f=1}
        f&&/CREATE_EXPORT="true"/{print "found";exit}' "$DEPLOY" \
     | grep -q found; then
  pass "tg-owned export forces CREATE_EXPORT=true (keep-managing)"
else
  fail "a tg-owned export must force CREATE_EXPORT=true"
fi

echo "== F. rendered-template: CreateExport / reuse wiring =="
RENDER="$HERE/tg-cur-athena-reuse-render-test.py"
if python3 "$RENDER" "$TPL"; then
  pass "tg-cur-athena reuse rendered-template assertions passed"
else
  fail "tg-cur-athena reuse rendered-template assertions FAILED"
fi

echo "== G. container-stack: ReusedCurBucketArn grant =="
# The app task role's CUR read must extend to the reused bucket only
# when ReusedCurBucketArn is set (HasReusedCurBucket condition).
if grep -qE 'ReusedCurBucketArn' "$CSTPL" \
   && grep -qE 'HasReusedCurBucket' "$CSTPL"; then
  pass "container-stack has ReusedCurBucketArn param + condition"
else
  fail "container-stack must add ReusedCurBucketArn + HasReusedCurBucket"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$fails CHECK(S) FAILED" >&2
  exit 1
fi
