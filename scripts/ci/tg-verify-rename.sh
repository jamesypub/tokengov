#!/usr/bin/env bash
# tg-verify-rename.sh — CI guard for the #349 role rename.
# Greps the tracked tree for the retired role name and exits
# non-zero if any reference remains outside the explicit
# allowlist of intentional historical mentions (the rename
# log entry in design-rationale).
#
# #402: the transitional alias role was DROPPED entirely
# (the app never shipped, so there was no name to preserve)
# — the CFN template no longer defines the legacy role, so it
# is removed from the allowlist. Any remaining hit in a
# functional path is now a regression.
#
# The retired name is assembled at runtime (NEEDLE below) so
# this guard file does not itself contain the literal token —
# that keeps `grep -rl <name> scripts/ container/ cfn/`
# genuinely empty once the rename is complete, instead of the
# guard matching its own source.

set -euo pipefail

# Retired role name, split so the literal isn't in this file.
NEEDLE="tg-Bedrock""Developer"

ALLOWED_PATHS=(
  scripts/tg-verify-rename.sh
  docs/design-rationale.md
)

is_allowed() {
  local p="$1" a
  for a in "${ALLOWED_PATHS[@]}"; do
    [ "$p" = "$a" ] && return 0
  done
  return 1
}

HITS=$(
  git ls-files \
    | xargs grep -l "$NEEDLE" 2>/dev/null \
    || true
)

UNEXPECTED=()
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if ! is_allowed "$f"; then
    UNEXPECTED+=("$f")
  fi
done <<< "$HITS"

if [ "${#UNEXPECTED[@]}" -gt 0 ]; then
  echo "ERROR: retired role name '$NEEDLE' found outside" >&2
  echo "       the allowlist (#349 regression):" >&2
  printf '  - %s\n' "${UNEXPECTED[@]}" >&2
  exit 1
fi

echo "OK: no unexpected '$NEEDLE' references"
