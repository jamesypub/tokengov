#!/usr/bin/env bash
# tg-ecs-destroy-verifier-scope-test.sh — assert the #568 fix:
# the clean-slate verifier in tg-ecs-destroy.sh scopes its residue
# check to what the destroyer OWNS, and ignores the separately-
# managed stacks/roles (tg-cognito-pool, tg-admin-dist, tg-cur-*,
# tg-BedrockAdmin) — while still catching a genuine leftover.
#
# Static (extract the NOT_OWNED regexes from the script + replay
# the filter) — no AWS, no destroy. Mirrors the other ci/*.sh.
# Usage: bash scripts/ci/tg-ecs-destroy-verifier-scope-test.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../tg-ecs-destroy.sh"
fails=0
pass() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1" >&2; fails=$((fails + 1)); }

echo "== tg-ecs-destroy verifier scope (#568) =="

# 1. syntax
if bash -n "$SCRIPT"; then pass "bash -n clean"
else fail "bash -n"; fi

# 2. Extract the NOT_OWNED regexes the script actually uses, so we
#    test the live values (not a copy that can drift).
STK_RE=$(grep -E "^NOT_OWNED_STACKS_RE=" "$SCRIPT" \
  | head -1 | cut -d"'" -f2)
ROLE_RE=$(grep -E "^NOT_OWNED_ROLES_RE=" "$SCRIPT" \
  | head -1 | cut -d"'" -f2)
[[ -n "$STK_RE" ]] && pass "found NOT_OWNED_STACKS_RE" \
  || fail "NOT_OWNED_STACKS_RE missing"
[[ -n "$ROLE_RE" ]] && pass "found NOT_OWNED_ROLES_RE" \
  || fail "NOT_OWNED_ROLES_RE missing"

# 3. The separately-managed stacks (the #503 L3 residue) must be
#    excluded → a --full destroy that left only these is CLEAN.
res=$(printf 'tg-cognito-pool\ntg-admin-dist\ntg-cur-athena\n' \
  | grep -vE "$STK_RE" || true)
if [[ -z "${res// /}" ]]; then
  pass "separately-managed stacks excluded (clean)"
else
  fail "still flagged as residue: $res"
fi

# 4. Their roles + legacy tg-BedrockAdmin excluded.
res=$(printf 'tg-BedrockAdmin\ntg-cur-athena-CrawlerKickRole-X\ntg-cur-glue-crawler\n' \
  | grep -vE "$ROLE_RE" || true)
if [[ -z "${res// /}" ]]; then
  pass "separately-managed roles + tg-BedrockAdmin excluded"
else
  fail "still flagged as residue: $res"
fi

# 5. A GENUINE owned leftover must STILL be flagged (no false neg).
res=$(printf 'tg-container-stack\ntg-cur-athena\n' \
  | grep -vE "$STK_RE" || true)
if printf '%s\n' "$res" | grep -q '^tg-container-stack$'; then
  pass "real residue (tg-container-stack) still caught"
else
  fail "tg-container-stack NOT caught — false negative!"
fi

# 6. tg-bedrock-role must pass through NOT_OWNED (so the --full
#    logic, not this filter, governs it). #725: tg-bedrock-logging
#    is retired, so tg-bedrock-role is the only bedrock-layer stack.
res=$(printf 'tg-bedrock-role\n' \
  | grep -vE "$STK_RE" || true)
if [[ "$(printf '%s\n' "$res" | grep -c '^tg-bedrock-')" -eq 1 ]]; then
  pass "tg-bedrock-role survives NOT_OWNED (governed by --full)"
else
  fail "tg-bedrock-role wrongly excluded by NOT_OWNED"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$fails CHECK(S) FAILED" >&2
  exit 1
fi
