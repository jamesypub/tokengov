#!/usr/bin/env bash
# tg-shell-parse-test.sh — parse gate on the shipped shell scripts, so a
# Bash-version-specific break (unbalanced quote, stray heredoc backslash,
# a Bash-4-only builtin) can't reach a customer install.
#
# Filed after #1067 (a bare end-of-line `\` inside the install summary
# heredoc escaped the newline and corrupted the script's quoting). The
# original gate ran `bash -n` under the HOST bash only. But the product
# must run on a stock Mac, which ships **Bash 3.2.57** — and two
# customer-blocking installs shipped anyway because CI's host bash is 5.x:
#   - #1105: `mapfile` (Bash 4+) in tg-cur-deploy.sh → install dies.
#   - #1112: a heredoc-in-$() with an apostrophe in tg-ecs-install.sh →
#            `unexpected EOF` on 3.2; clean on 5.x.
# So this gate now ALSO parses every script under Bash 3.2 (the official
# `bash:3.2` image == 3.2.57, the macOS version) and greps for the common
# Bash-4-isms the parser can't catch at `-n` time (runtime builtins).
#
# Three passes:
#   1. host `bash -n`         — fast, always runs.
#   2. Bash-4-ism grep        — cheap, no Docker; catches mapfile /
#                               readarray / declare -A / local -A /
#                               ${x^^} / ${x,,} (would have caught #1105).
#   3. `bash:3.2` `bash -n`   — the 3.2 lexer; catches the #1112
#                               quote/heredoc class the grep can't. Needs
#                               Docker. REQUIRED in CI (TG_CI=1); skipped
#                               with a loud warning when Docker is absent
#                               locally (#1114 OQ1).
#
# Usage:   bash scripts/ci/tg-shell-parse-test.sh
# CI:      TG_CI=1 bash scripts/ci/tg-shell-parse-test.sh
#          (TG_CI=1 makes the missing-Docker case a HARD FAILURE — the
#          3.2 pass is the merge gate, it must actually run.)

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
TG_CI="${TG_CI:-0}"
BASH32_IMAGE="bash:3.2"
fails=0
pass() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1" >&2; fails=$((fails + 1)); }

# The shipped scripts: every tracked .sh under scripts/ (the installer +
# helpers ship and are exec'd by the CLI). Skip nothing — a parse error
# anywhere is a release blocker. Collect once (Bash 3.2-safe: a while-read
# loop, NOT mapfile — this gate's own portability matters too).
SCRIPTS=()
while IFS= read -r f; do
  SCRIPTS+=("$f")
done < <(find "$ROOT/scripts" -name '*.sh' -type f | sort)

# ── Pass 1: host bash -n ─────────────────────────────────────────────
echo "== Pass 1: host bash -n ($(bash --version | head -1)) =="
for f in "${SCRIPTS[@]}"; do
  if bash -n "$f" 2>/tmp/parse-err; then
    pass "$(basename "$f")"
  else
    fail "$(basename "$f") — $(tr '\n' ' ' </tmp/parse-err)"
  fi
done

# ── Pass 2: Bash-4-ism grep (no Docker) ──────────────────────────────
# Catches the runtime Bash-4 builtins/expansions a `bash -n` parse won't
# flag (mapfile/readarray are valid syntax to a parser; they just don't
# EXIST on 3.2). Excludes scripts/ci/ so this gate's own pattern string
# doesn't match itself.
echo
echo "== Pass 2: Bash-4-ism grep (3.2-incompatible builtins/expansions) =="
B4_RE='mapfile|readarray|declare -A|local -A|\$\{[A-Za-z_][A-Za-z0-9_]*(\^\^?|,,?)\}'

# The Pass-2 hit filter, factored out so the self-test below exercises the
# EXACT pipeline the real scan uses (no drift-prone copy). Greps a
# directory tree for B4_RE, then STRIPS COMMENT lines (same filter Pass 5a
# uses, line ~168): a comment DOCUMENTING a builtin's deliberate absence
# (e.g. "# Split … WITHOUT mapfile/readarray (Bash 3.2)." in
# tg-ecs-install.sh) must NOT false-red the guard — only a real invocation
# on a CODE line is a Bash-4-ism. grep output is `path:lineno:content`; the
# -vE drops any hit whose content (after the path:lineno: prefix) is a `#`
# comment. `|| true` keeps `set -e` from aborting when a stage empties.
b4_scan() {
  grep -rnE "$B4_RE" "$1" --include='*.sh' --exclude-dir=ci 2>/dev/null \
    | grep -vE ':[[:space:]]*#' || true
}

b4_hits=$(b4_scan "$ROOT/scripts")
if [ -n "$b4_hits" ]; then
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    fail "Bash-4-ism: $line"
  done <<< "$b4_hits"
else
  pass "no Bash-4-isms (mapfile/readarray/declare -A/local -A/case-mod)"
fi

# ── Pass 2b: SELF-TEST — the comment-strip is correct in BOTH directions ─
# Guards against a future edit that weakens Pass 2 back into the
# comment-blind false-red (the reason it stood red on main) OR that
# over-strips and lets a real invocation through. Hermetic: a temp fixture
# tree, no Docker/AWS/creds. Bash-3.2-safe (mktemp -d + here-strings only).
echo
echo "== Pass 2b: self-test — Pass-2 filter ignores comments, catches code =="
b4_fixture_dir=$(mktemp -d 2>/dev/null || mktemp -d -t tgb4)
# NEGATIVE fixture: a builtin named only in a comment → must NOT be a hit.
{
  echo '#!/usr/bin/env bash'
  echo '# no mapfile/readarray here — declare -A only in this comment'
  echo 'echo ok'
} > "$b4_fixture_dir/comment_only.sh"
# POSITIVE fixture: a real invocation on a code line → must be a hit.
{
  echo '#!/usr/bin/env bash'
  echo 'mapfile -t x < f'
} > "$b4_fixture_dir/real_invocation.sh"
b4_selftest=$(b4_scan "$b4_fixture_dir")
if printf '%s\n' "$b4_selftest" | grep -q 'real_invocation.sh'; then
  pass "real 'mapfile -t x < f' on a code line still fails Pass 2"
else
  fail "self-test: a REAL Bash-4-ism on a code line was NOT caught"
fi
if printf '%s\n' "$b4_selftest" | grep -q 'comment_only.sh'; then
  fail "self-test: a builtin named only in a comment false-red Pass 2"
else
  pass "builtin named only in a '#' comment does NOT false-red Pass 2"
fi
rm -rf "$b4_fixture_dir"

# ── Pass 3: bash:3.2 parse (Docker) ──────────────────────────────────
echo
echo "== Pass 3: bash 3.2 parse (Docker $BASH32_IMAGE == 3.2.57, macOS) =="
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  for f in "${SCRIPTS[@]}"; do
    if docker run --rm -v "$f":/s.sh:ro "$BASH32_IMAGE" \
         bash -n /s.sh 2>/tmp/parse32-err; then
      pass "3.2: $(basename "$f")"
    else
      fail "3.2: $(basename "$f") — $(tr '\n' ' ' </tmp/parse32-err)"
    fi
  done
else
  if [ "$TG_CI" = "1" ]; then
    fail "Docker unavailable but TG_CI=1 — the bash 3.2 parse gate is \
REQUIRED in CI and could not run (this is the macOS-break merge gate)."
  else
    echo "  ! WARNING: Docker unavailable — SKIPPING the bash 3.2 parse" >&2
    echo "  ! pass locally. The host bash -n + Bash-4-ism grep still ran." >&2
    echo "  ! CI runs this pass (Docker present); set TG_CI=1 to require it." >&2
  fi
fi

# ── Pass 4: shellcheck (Layer A — static, no exec, no AWS) ───────────
# #1133: catches the UNQUOTED array-expansion hazard (SC2068) + other
# error-level 3.2 footguns the parse gate can't see. NOT the catch for
# the QUOTED-empty-array set -u trap (#1132) — that's Pass 5; shellcheck
# is verified NOT to flag the quoted form. We gate on --severity=error
# (SC2068 is an error) so the install-breaking class fails the build
# while the pre-existing info/warning style noise (SC2015/SC1091) does
# not (#1133 OQ1=b: gate the high-signal class now, tighten later).
echo
echo "== Pass 4: shellcheck (Layer A — SC2068 + error-level hazards) =="
SHELLCHECK_IMAGE="koalaman/shellcheck:stable"
if command -v shellcheck >/dev/null 2>&1; then
  _sc() { shellcheck --shell=bash --severity=error "$@"; }
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  _sc() { docker run --rm -v "$ROOT":/mnt:ro -w /mnt "$SHELLCHECK_IMAGE" \
            --shell=bash --severity=error "$@"; }
else
  _sc=""
fi
if [ -z "${_sc:-_}" ] && ! command -v shellcheck >/dev/null 2>&1 \
   && ! { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }
then
  if [ "$TG_CI" = "1" ]; then
    fail "shellcheck unavailable but TG_CI=1 — Layer A is REQUIRED in CI."
  else
    echo "  ! WARNING: no shellcheck + no Docker — SKIPPING Layer A locally." >&2
  fi
else
  for f in "${SCRIPTS[@]}"; do
    # Pass a repo-relative path (read under the mounted root).
    rel="${f#"$ROOT"/}"
    if ( cd "$ROOT" && _sc "$rel" ) >/tmp/sc-err 2>&1; then
      pass "shellcheck: $(basename "$f")"
    else
      fail "shellcheck: $(basename "$f") — $(tr '\n' ' ' </tmp/sc-err | cut -c1-300)"
    fi
  done
fi

# ── Pass 5: empty-array under set -u (the #1132 class) ───────────────
# The PRIMARY catch for the quoted-"${arr[@]}"-on-an-empty-array set -u
# trap that BOTH the parse gate (Pass 1/3) and shellcheck (Pass 4) miss
# (shellcheck flags only the UNQUOTED form; #1132 was QUOTED). Two parts:
#
#   5a STATIC — fail on any BARE "${ARR[@]}" of an empty-able array on a
#      code line that is NOT the `+`-guarded form "${ARR[@]+"${ARR[@]}"}".
#      Deterministic, no Docker; this is what reliably catches a reverted
#      #1132 fix (the guarded form has nested quotes that defeat naive
#      expression-extraction, so we detect the BARE form by absence of
#      the guard rather than re-running extracted expressions).
#   5b RUNTIME PROOF (Docker) — a fixed canonical snippet on bash:3.2
#      demonstrating WHY: a bare empty-array expansion throws `unbound
#      variable` under set -u while the `+`-guard is clean. Anchors the
#      static rule to the real 3.2 behavior; not data-dependent.
#
# Empty-able arrays = those declared `=()` (e.g. PROFILE_ARGS when
# AWS_PROFILE is unset; _export_arns when zero exports; VALID_*/INVALID_*
# before any match). Hermetic: no AWS, no install, no creds.
echo
echo "== Pass 5a: STATIC — bare empty-able \"\${arr[@]}\" under set -u (#1132) =="
EMPTY_ARRAYS='PROFILE_ARGS|_export_arns|VALID_[A-Z]+|INVALID_[A-Z]+'
# Bare = "${ARR[@]}" with NO `+` immediately after `[@]` (the guard adds
# `[@]+`). Code lines only (skip comments). Search every shipped *.sh.
if bare=$(grep -rnE "\"\\\$\{($EMPTY_ARRAYS)\[@\]\}\"" \
      "$ROOT/scripts" --include='*.sh' 2>/dev/null \
      | grep -vE ':[[:space:]]*#' \
      | grep -vE '\[@\]\+'); then
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    fail "bare empty-able array (use \"\${A[@]+\"\${A[@]}\"}\"): $line"
  done <<< "$bare"
else
  pass "no bare empty-able \"\${arr[@]}\" — all guarded (#1132)"
fi

echo
echo "== Pass 5b: bash 3.2 RUNTIME proof — bare errors, guard is clean =="
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  bare_out=$(printf '%s\n' 'set -u' 'a=()' 'for x in "${a[@]}"; do :; done' \
             'echo REACHED' | docker run --rm -i "$BASH32_IMAGE" bash 2>&1) \
             || true
  guard_out=$(printf '%s\n' 'set -u' 'a=()' \
              'for x in "${a[@]+"${a[@]}"}"; do :; done' 'echo GUARD_OK' \
              | docker run --rm -i "$BASH32_IMAGE" bash 2>&1) || true
  if printf '%s' "$bare_out" | grep -q 'unbound variable' \
     && printf '%s' "$guard_out" | grep -q 'GUARD_OK'; then
    pass "3.2: bare empty-array errors (unbound), the +-guard runs clean"
  else
    fail "3.2 runtime proof unexpected — bare:[$(printf '%s' "$bare_out" \
| tr '\n' ' ')] guard:[$(printf '%s' "$guard_out" | tr '\n' ' ')]"
  fi
else
  if [ "$TG_CI" = "1" ]; then
    fail "Docker unavailable but TG_CI=1 — Pass 5b (3.2 runtime proof) \
is REQUIRED in CI."
  else
    echo "  ! WARNING: Docker unavailable — SKIPPING the 3.2 runtime" >&2
    echo "  ! proof locally (the static 5a still ran). CI runs it." >&2
  fi
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$fails CHECK(S) FAILED" >&2
  exit 1
fi
