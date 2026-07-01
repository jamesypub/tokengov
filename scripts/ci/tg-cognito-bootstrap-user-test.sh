#!/usr/bin/env bash
# tg-cognito-bootstrap-user-test.sh — assert the #937 fix:
# tg_ensure_bootstrap_admin_user in tg-cognito-bootstrap-pw.sh creates
# the bootstrap admin idempotently, replacing the create-only
# AWS::Cognito::UserPoolUser CFN resource that rolled the pool stack
# back with `User already exists` on every callback-URL reconcile
# update (the #935 self-heal blocker).
#
# Exercises the UPGRADE path (user already exists) that a lint-green
# template masks — the acceptance criterion is that `tg install`
# against a pool whose bootstrap user already exists SUCCEEDS and
# leaves the user intact.
#
# Static: stubs `aws` on PATH, extracts the LIVE function body from
# the script (not a copy that can drift), and asserts whether
# `admin-create-user` fires in each scenario. No real AWS.
# Usage: bash scripts/ci/tg-cognito-bootstrap-user-test.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../tg-cognito-bootstrap-pw.sh"
fails=0
pass() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1" >&2; fails=$((fails + 1)); }

echo "== tg-cognito bootstrap-user idempotency (#937) =="

# 1. syntax
if bash -n "$SCRIPT"; then pass "bash -n clean"
else fail "bash -n"; fi

# 2. The installers wire the ensure-user call BEFORE the password
#    step (the user must exist before it can be set CONFIRMED), and
#    on BOTH install paths.
for inst in tg-ecs-install.sh tg-local-install.sh; do
  f="$HERE/../$inst"
  if awk '
        /tg_ensure_bootstrap_admin_user/ {e=NR}
        /tg_set_bootstrap_admin_password/ {p=NR}
        END{exit !(e && p && e < p)}' "$f"; then
    pass "$inst: ensure-user invoked before set-password"
  else
    fail "$inst: ensure-user missing or not before set-password"
  fi
done

# 3. The create-only CFN resource is gone from the template (the root
#    cause). Its absence is what stops the UPDATE_ROLLBACK.
TPL="$HERE/../../cfn/tg-cognito-pool.yaml"
if grep -qE '^\s*BootstrapUser:\s*$' "$TPL" \
   || grep -q 'Type:\s*AWS::Cognito::UserPoolUser' "$TPL"; then
  fail "AWS::Cognito::UserPoolUser still in tg-cognito-pool.yaml"
else
  pass "BootstrapUser CFN resource removed from template"
fi

# 4. Extract the LIVE function body and eval it, so we test what
#    actually ships (not a transcribed copy).
FUNC_SRC=$(awk '
  /^tg_ensure_bootstrap_admin_user\(\) \{/ {grab=1}
  grab {print}
  grab && /^\}/ {exit}
' "$SCRIPT")
if [[ -z "$FUNC_SRC" ]]; then
  fail "could not extract tg_ensure_bootstrap_admin_user() body"
  echo "$fails CHECK(S) FAILED" >&2; exit 1
fi
pass "extracted live function body"

# Stubs the function depends on. NOTE: the function under test calls
# `fail` on error, but `fail` is also this harness's failure reporter.
# We must NOT clobber the reporter — so eval the function here, and in
# run_case() invoke it inside a subshell that locally redefines `fail`
# to a non-fatal abort. The parent's reporter stays intact.
ok()   { :; }
warn() { :; }
PROFILE_ARGS=()
eval "$FUNC_SRC"

# A stub `aws` on PATH that:
#   - returns a scripted UserStatus for `cognito-idp admin-get-user`
#   - records a marker file when `cognito-idp admin-create-user` fires
#   - can be told to fail the create (CREATE_RESULT=fail)
STUBDIR=$(mktemp -d)
trap 'rm -rf "$STUBDIR"' EXIT
CREATED_MARKER="$STUBDIR/created"
cat > "$STUBDIR/aws" <<EOF
#!/usr/bin/env bash
# \$AWS_GET_USER_RESULT controls the admin-get-user reply:
#   __ABSENT__ → non-zero (UserNotFoundException)
#   else       → printed as UserStatus
# \$AWS_CREATE_RESULT=fail → admin-create-user exits non-zero
if [[ "\$2" == "admin-get-user" ]]; then
  if [[ "\${AWS_GET_USER_RESULT:-}" == "__ABSENT__" ]]; then
    exit 254
  fi
  printf '%s\n' "\${AWS_GET_USER_RESULT:-None}"
  exit 0
fi
if [[ "\$2" == "admin-create-user" ]]; then
  if [[ "\${AWS_CREATE_RESULT:-}" == "fail" ]]; then exit 1; fi
  touch "$CREATED_MARKER"
  exit 0
fi
exit 0
EOF
chmod +x "$STUBDIR/aws"
PATH="$STUBDIR:$PATH"

run_case() {
  # $1 = AWS_GET_USER_RESULT ; $2 = AWS_CREATE_RESULT (optional)
  # echoes CREATED iff admin-create-user fired, else SKIPPED;
  # prefixes FAILED- if the function failed (returned non-zero).
  # The subshell locally redefines `fail` to a non-fatal return so a
  # function-under-test failure doesn't kill the harness or touch the
  # parent's `fail` reporter.
  rm -f "$CREATED_MARKER"
  local rc=0
  (
    fail() { return 1; }
    AWS_GET_USER_RESULT="$1" AWS_CREATE_RESULT="${2:-}" \
      tg_ensure_bootstrap_admin_user pool-1 \
        admin@example.com us-east-1 >/dev/null 2>&1
  ) || rc=$?
  local out
  [[ -f "$CREATED_MARKER" ]] && out="CREATED" || out="SKIPPED"
  [[ "$rc" -ne 0 ]] && out="FAILED-$out"
  echo "$out"
}

# 5. existing CONFIRMED user → no create, success (the #937 case:
#    the reconcile-update path that used to roll back).
got=$(run_case "CONFIRMED")
if [[ "$got" == "SKIPPED" ]]; then
  pass "existing user → create skipped (idempotent, the #937 fix)"
else
  fail "existing user produced '$got' — expected SKIPPED"
fi

# 6. existing FORCE_CHANGE_PASSWORD user → also no create (a prior
#    install left it; the password step will CONFIRM it).
got=$(run_case "FORCE_CHANGE_PASSWORD")
if [[ "$got" == "SKIPPED" ]]; then
  pass "FORCE_CHANGE_PASSWORD user → create skipped"
else
  fail "FORCE_CHANGE_PASSWORD produced '$got' — expected SKIPPED"
fi

# 7. fresh install: user absent → created.
got=$(run_case "__ABSENT__")
if [[ "$got" == "CREATED" ]]; then
  pass "absent user → created (fresh install)"
else
  fail "absent user produced '$got' — expected CREATED"
fi

# 8. defensive: a literal 'None' UserStatus (AWS '--output text'
#    null) must NOT be treated as present — it means absent → create.
got=$(run_case "None")
if [[ "$got" == "CREATED" ]]; then
  pass "UserStatus=None → treated as absent → created"
else
  fail "UserStatus=None produced '$got' — expected CREATED"
fi

# 9. fail-loud: absent user + create error → function fails (never
#    silently proceeds to the password step on a missing user).
got=$(run_case "__ABSENT__" "fail")
if [[ "$got" == FAILED-* ]]; then
  pass "create error → fails loud"
else
  fail "create error produced '$got' — expected FAILED-*"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$fails CHECK(S) FAILED" >&2
  exit 1
fi
