#!/usr/bin/env bash
# tg-ecs-install-preflight-confirm-test.sh — assert the ECS
# installer's pre-flight prints a labeled credential/account
# summary and gates the deploy on an account go/no-go [y/N].
#
# The contract:
#   - Before any resource is created, print: credential source,
#     resolved identity, resolved account, target account.
#   - Wrong-account → hard-fail, NON-overridable (TG_ASSUME_YES
#     does not bypass it).
#   - Accounts match: interactive [y/N]; TG_ASSUME_YES=1 proceeds
#     (incl. headless); non-TTY without it fails fast (no hang).
#   - No secrets printed (only ARN + account id + profile NAME).
#
# Static (grep) + behavioral (replay the go/no-go block with a
# controlled credential source + stdin). No AWS, no deploy —
# mirrors tg-ecs-install-testtrust-test.sh style. The LIVE
# stage-account smoke (prompt fires / n aborts / y proceeds /
# TG_ASSUME_YES headless) is a deploy and runs out-of-band.
# Usage: bash scripts/ci/tg-ecs-install-preflight-confirm-test.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../tg-ecs-install.sh"
fails=0
pass() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1" >&2; fails=$((fails + 1)); }

echo "== ECS installer pre-flight account confirm (#871) =="

# 1. syntax
if bash -n "$SCRIPT"; then pass "bash -n clean"
else fail "bash -n"; fi

# 2. Labeled summary lines are printed in pre-flight.
for label in \
  "Credential source :" \
  "Resolved identity :" \
  "Resolved account  :" \
  "Target account    :"; do
  if grep -qF "$label" "$SCRIPT"; then
    pass "summary line present: ${label% :}"
  else
    fail "missing summary line: $label"
  fi
done

# 3. The go/no-go prompt + the headless escape hatch exist.
if grep -qE 'Proceed\? \[y/N\]' "$SCRIPT"; then
  pass "go/no-go [y/N] prompt present"
else
  fail "go/no-go [y/N] prompt missing"
fi
if grep -qE '\$\{TG_ASSUME_YES:-\}' "$SCRIPT"; then
  pass "TG_ASSUME_YES headless escape present"
else
  fail "TG_ASSUME_YES escape missing"
fi
if grep -qE 'stdin is not a TTY' "$SCRIPT" \
   && grep -qE 'TG_ASSUME_YES=1 to proceed' "$SCRIPT"; then
  pass "non-TTY fail-fast message present (no stdin hang)"
else
  fail "non-TTY fail-fast message missing"
fi

# 4. TG_ASSUME_YES is documented in the header env list.
if grep -qE '^#   TG_ASSUME_YES' "$SCRIPT"; then
  pass "TG_ASSUME_YES documented in header"
else
  fail "TG_ASSUME_YES not documented in header"
fi

# 5. The wrong-account hard-fail is preserved and is NOT gated by
#    TG_ASSUME_YES (the mismatch `fail` must precede / be
#    independent of the assume-yes branch).
if grep -qE 'if \[\[ "\$CALLER_ACCT" != "\$TG_TARGET_ACCOUNT_ID" \]\]' "$SCRIPT"; then
  pass "exact-match hard-fail preserved"
else
  fail "exact-match hard-fail removed/changed (regression)"
fi
# Source-order: the mismatch `fail` must come BEFORE the
# TG_ASSUME_YES branch, so assume-yes can't reach a wrong account.
ln_fail=$(grep -n 'resolve to account' "$SCRIPT" | head -1 | cut -d: -f1)
ln_yes=$(grep -n '"\${TG_ASSUME_YES:-}" == "1"' "$SCRIPT" | head -1 | cut -d: -f1)
if [ -n "$ln_fail" ] && [ -n "$ln_yes" ] && [ "$ln_fail" -lt "$ln_yes" ]; then
  pass "mismatch hard-fail precedes the TG_ASSUME_YES branch"
else
  fail "TG_ASSUME_YES could be reached before the mismatch fail"
fi

# 6. No secret material is printed in the new summary (only ARN /
#    account id / profile NAME). Guard against an accidental
#    access-key / token echo near the summary.
if grep -nE 'Resolved identity|Resolved account|Credential source' "$SCRIPT" \
   | grep -qiE 'secret|access.?key|session.?token|password'; then
  fail "a summary line references a secret-shaped value"
else
  pass "summary prints no secret-shaped values"
fi

echo "== behavioral: replay the go/no-go decision =="

# Replay the EXACT decision logic the installer runs (kept in
# lock-step with the script — if the script's idiom changes this
# helper must change too). Inputs come from env + stdin; output is
# the decision (proceed / abort / fail) without any AWS call.
decide() {
  # mirrors tg-ecs-install.sh pre-flight, accounts-already-match case
  if [[ "${TG_ASSUME_YES:-}" == "1" ]]; then
    echo proceed; return 0
  elif [[ -t 0 ]]; then
    read -rp "Proceed? [y/N] " _ok
    [[ "$_ok" =~ ^[Yy]$ ]] && { echo proceed; return 0; }
    echo abort; return 1
  else
    echo "fail: non-TTY without TG_ASSUME_YES" >&2
    return 3
  fi
}

# B1: TG_ASSUME_YES=1 proceeds even headless (stdin from /dev/null).
got=$(TG_ASSUME_YES=1 bash -c "$(declare -f decide); decide" </dev/null) || true
[ "$got" = "proceed" ] \
  && pass "TG_ASSUME_YES=1 proceeds (headless)" \
  || fail "TG_ASSUME_YES=1 did not proceed (got '$got')"

# B2: non-TTY without TG_ASSUME_YES fails fast (rc 3), no hang.
rc=0
out=$(bash -c "$(declare -f decide); decide" </dev/null 2>&1) || rc=$?
if [ "$rc" -eq 3 ] && grep -q 'non-TTY' <<<"$out"; then
  pass "non-TTY without TG_ASSUME_YES fails fast (rc=3, no hang)"
else
  fail "non-TTY path wrong (rc=$rc out='$out')"
fi

# B3: interactive 'y' proceeds; 'n'/empty aborts. Drive a pseudo-
# interactive read by feeding the answer on stdin AND faking a TTY
# via `[[ -t 0 ]]` is impossible without a real tty — so exercise
# the read+regex directly with each answer.
ans_decide() {  # $1 = the typed answer
  local _ok="$1"
  [[ "$_ok" =~ ^[Yy]$ ]] && echo proceed || echo abort
}
for pair in "y:proceed" "Y:proceed" "n:abort" ":abort" "yes:abort"; do
  a="${pair%%:*}"; want="${pair##*:}"
  got=$(ans_decide "$a")
  [ "$got" = "$want" ] \
    && pass "answer '${a:-<empty>}' → $got" \
    || fail "answer '${a:-<empty>}' → $got (want $want)"
done

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$fails CHECK(S) FAILED" >&2
  exit 1
fi
