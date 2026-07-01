#!/usr/bin/env bash
# tg-cur-deploy-crawler-table-test.sh — assert the #591 upgrade-
# path guard: drop_crawler_table_if_orphan
# in tg-cur-deploy.sh drops the crawler-created out-of-band Glue CUR
# table on a crawler-era upgrade so CFN's static AWS::Glue::Table
# create doesn't hit AlreadyExistsException — while leaving a fresh
# install (no table) and a re-run (CFN-managed table) untouched.
#
# It exercises the UPGRADE (table-already-exists) path, which a
# lint-green template masks.
#
# Static-ish: stubs `aws` on PATH, extracts the LIVE function body
# from the script (not a copy that can drift), and asserts whether
# `aws glue delete-table` fires in each scenario. No real AWS.
# Usage: bash scripts/ci/tg-cur-deploy-crawler-table-test.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../tg-cur-deploy.sh"
fails=0
pass() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1" >&2; fails=$((fails + 1)); }

echo "== tg-cur-deploy crawler-table guard (#591) =="

# 1. syntax
if bash -n "$SCRIPT"; then pass "bash -n clean"
else fail "bash -n"; fi

# 2. The guard is wired into the deploy path BEFORE the
#    `aws cloudformation deploy`, not defined-but-unused.
if grep -qE '^drop_crawler_table_if_orphan ' "$SCRIPT" \
   && awk '/^drop_crawler_table_if_orphan "\$GLUE_DATABASE"/{c=NR}
           /^aws cloudformation deploy/{d=NR}
           END{exit !(c && d && c < d)}' "$SCRIPT"; then
  pass "guard invoked before cloudformation deploy"
else
  fail "guard not invoked before deploy (defined-but-unused?)"
fi

# 3. Extract the LIVE function body from the script and eval it,
#    so we test what actually ships (not a transcribed copy).
FUNC_SRC=$(awk '
  /^drop_crawler_table_if_orphan\(\) \{/ {grab=1}
  grab {print}
  grab && /^\}/ {exit}
' "$SCRIPT")
if [[ -z "$FUNC_SRC" ]]; then
  fail "could not extract drop_crawler_table_if_orphan() body"
  echo "$fails CHECK(S) FAILED" >&2; exit 1
fi
pass "extracted live function body"

# Stubs the function depends on.
ok()   { :; }
warn() { :; }
AWS_PROFILE=test-profile
AWS_REGION=us-east-1
eval "$FUNC_SRC"

# A stub `aws` on PATH that:
#   - returns a scripted CreatedBy for `glue get-table`
#   - records a marker file when `glue delete-table` is called
STUBDIR=$(mktemp -d)
trap 'rm -rf "$STUBDIR"' EXIT
DELETED_MARKER="$STUBDIR/deleted"
cat > "$STUBDIR/aws" <<EOF
#!/usr/bin/env bash
# \$AWS_GET_TABLE_RESULT controls the get-table reply.
if [[ "\$1" == "glue" && "\$2" == "get-table" ]]; then
  if [[ "\${AWS_GET_TABLE_RESULT:-}" == "__ABSENT__" ]]; then
    exit 254   # mimic EntityNotFoundException → non-zero
  fi
  printf '%s\n' "\${AWS_GET_TABLE_RESULT:-None}"
  exit 0
fi
if [[ "\$1" == "glue" && "\$2" == "delete-table" ]]; then
  touch "$DELETED_MARKER"
  exit 0
fi
exit 0
EOF
chmod +x "$STUBDIR/aws"
PATH="$STUBDIR:$PATH"

run_case() {
  # \$1 = AWS_GET_TABLE_RESULT value; populates DELETED_MARKER iff
  # delete-table fired.
  rm -f "$DELETED_MARKER"
  AWS_GET_TABLE_RESULT="$1" \
    drop_crawler_table_if_orphan tg_cur data >/dev/null 2>&1 || true
  [[ -f "$DELETED_MARKER" ]] && echo "DROPPED" || echo "KEPT"
}

# 4. crawler-era upgrade: CreatedBy contains AWS-Crawler → DROP
got=$(run_case "arn:aws:glue:...:userDefinedFunction/tg-cur-glue-crawler/AWS-Crawler")
if [[ "$got" == "DROPPED" ]]; then
  pass "crawler-created table → dropped (upgrade path)"
else
  fail "crawler-created table NOT dropped — upgrade still breaks"
fi

# 5. re-run: CFN-managed table (CreatedBy lacks AWS-Crawler) → KEEP
got=$(run_case "arn:aws:cloudformation:us-east-1:123:stack/tg-cur-athena")
if [[ "$got" == "KEPT" ]]; then
  pass "CFN-managed table → kept (idempotent re-run)"
else
  fail "CFN-managed table wrongly dropped — re-run would churn"
fi

# 6. fresh install: no table (get-table non-zero) → no-op, no DROP
got=$(run_case "__ABSENT__")
if [[ "$got" == "KEPT" ]]; then
  pass "absent table → no-op (fresh install)"
else
  fail "absent table triggered a delete — should no-op"
fi

# 7. defensive: a literal 'None' CreatedBy (AWS '--output text'
#    null) is treated as absent, not dropped.
got=$(run_case "None")
if [[ "$got" == "KEPT" ]]; then
  pass "CreatedBy=None → no-op"
else
  fail "CreatedBy=None triggered a delete"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$fails CHECK(S) FAILED" >&2
  exit 1
fi
