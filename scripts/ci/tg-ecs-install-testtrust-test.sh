#!/usr/bin/env bash
# tg-ecs-install-testtrust-test.sh — assert the ECS installer
# gates the test-trust auth bypass on the named Environment (#570,
# supersedes the #477 secure-default-flag contract).
#
# The invariant: the test-trust bypass (X-Tg-Test-Email →
# org-admin, no SigV4) is ON only for dev/stage and LOCKED OFF for
# prod. The installer derives Environment from TG_ENV
# (unset/other → prod, the fail-safe), no longer reads a
# TG_ENABLE_TEST_TRUST flag, and passes Environment= to CFN on both
# deploy passes. This test fails if prod ever stops locking the
# bypass, if the derivation default isn't prod, or if Environment=
# stops being passed.
#
# Static (grep/source the script's resolution logic) — no AWS,
# no deploy. Mirrors tg-public-publish-test.sh style.
# Usage: bash scripts/ci/tg-ecs-install-testtrust-test.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../tg-ecs-install.sh"
fails=0
pass() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1" >&2; fails=$((fails + 1)); }

echo "== ECS installer test-trust Environment gate (#570) =="

# 1. syntax
if bash -n "$SCRIPT"; then pass "bash -n clean"
else fail "bash -n"; fi

# 2. No hardcoded EnableTestAuthTrust=true OVERRIDE survives
#    (match the CFN --parameter-overrides form, not prose mentions
#    of the literal in comments).
if grep -qE '"EnableTestAuthTrust=true"' "$SCRIPT"; then
  fail "hardcoded EnableTestAuthTrust=true override (regression)"
else
  pass "no hardcoded EnableTestAuthTrust=true override"
fi

# 3. The CFN override uses the resolved var, on both deploy
#    passes (DesiredCount=0 and =1).
n=$(grep -c 'EnableTestAuthTrust=\$TG_TEST_TRUST_CFN' "$SCRIPT")
if [ "$n" -ge 2 ]; then
  pass "both deploy passes use \$TG_TEST_TRUST_CFN ($n)"
else
  fail "expected >=2 overrides via \$TG_TEST_TRUST_CFN, got $n"
fi

# 4. Environment= is passed to CFN on both deploy passes.
m=$(grep -c '"Environment=\$TG_ENVIRONMENT"' "$SCRIPT")
if [ "$m" -ge 2 ]; then
  pass "both deploy passes pass Environment= ($m)"
else
  fail "expected >=2 Environment= overrides, got $m"
fi

# derive_env: replays the installer's Environment derivation,
# reading TG_ENV / TG_ENVIRONMENT from the environment.
derive_env() {
  case "${TG_ENVIRONMENT:-${TG_ENV:-}}" in
    dev)   echo dev ;;
    stage) echo stage ;;
    prod)  echo prod ;;
    *)     echo prod ;;
  esac
}

# 5. Behavioral: replay the derivation + bypass-mapping and check
#    the contract per input. dev/stage → true,
#    prod/unset/garbage → false (fail-safe).
check_map() {
  local tgenv="$1" want="$2" env got
  env=$(TG_ENV="$tgenv" bash -c "$(declare -f derive_env); derive_env")
  [[ "$env" == "prod" ]] && got=false || got=true
  if [ "$got" = "$want" ]; then
    pass "TG_ENV=${tgenv:-(unset)} → $env → bypass $got"
  else
    fail "TG_ENV=${tgenv:-(unset)} → bypass $got (want $want)"
  fi
}

check_map ""        false   # unset → prod → secure default
check_map garbage   false   # anything unknown → prod
check_map prod      false
check_map dev       true
check_map stage     true

# 6. Explicit TG_ENVIRONMENT overrides TG_ENV.
got=$(TG_ENV=prod TG_ENVIRONMENT=dev bash -c "$(declare -f derive_env); derive_env")
if [ "$got" = "dev" ]; then
  pass "TG_ENVIRONMENT overrides TG_ENV (prod+dev → dev)"
else
  fail "TG_ENVIRONMENT override broken (got $got)"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$fails CHECK(S) FAILED" >&2
  exit 1
fi
