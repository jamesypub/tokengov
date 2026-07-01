#!/bin/bash
#
# Run Claude Code against Bedrock under an existing AWS profile.
# Sets the env vars and execs `claude` — does not modify ~/.claude/settings.json.
#
# Make sure your AWS SSO session is active first:
#   aws sso login --profile <source-profile>                       (local: opens a browser)
#   aws sso login --profile <source-profile> --use-device-code     (remote/EC2: prints URL + code)
#
# Usage:
#   scripts/claude-use.sh <aws-profile> [-- claude-args...]
#
# Examples:
#   scripts/claude-use.sh tg-consumer-<YOUR_ACCOUNT_ID>
#   scripts/claude-use.sh tg-consumer-<YOUR_ACCOUNT_ID> -- -p "say hi"

set -e

# Color helpers (only when stdout is a TTY)
if [[ -t 2 ]]; then
  BOLD=$'\033[1m'; YELLOW=$'\033[1;33m'; CYAN=$'\033[1;36m'; RESET=$'\033[0m'
else
  BOLD=''; YELLOW=''; CYAN=''; RESET=''
fi

# Best-effort: walk source_profile chain to find the SSO entry profile.
# That's the right one to pass to `aws sso login`.
sso_source_profile() {
  python3 - "$HOME/.aws/config" "$1" 2>/dev/null <<'PY'
import sys, configparser, pathlib
path, want = sys.argv[1], sys.argv[2]
text = pathlib.Path(path).read_text() if pathlib.Path(path).exists() else ""
cp = configparser.ConfigParser()
cp.read_string(text)
def chain(name):
    sec = f"profile {name}"
    if not cp.has_section(sec):
        return None
    if cp.has_option(sec, "sso_session"):
        return name
    src = cp.get(sec, "source_profile", fallback=None)
    return chain(src) if src else None
print(chain(want) or want)
PY
}

PROFILE="${1:-}"
if [[ -z "$PROFILE" ]]; then
  echo "usage: $0 <aws-profile> [-- claude-args...]" >&2
  echo >&2
  echo "Available profiles in ~/.aws/config:" >&2
  grep -E '^\[profile ' ~/.aws/config 2>/dev/null | sed 's/^\[profile /  /;s/\]$//' >&2
  echo >&2
  echo "${YELLOW}Reminder: make sure 'aws sso login --profile <source>' is current.${RESET}" >&2
  exit 1
fi

if ! grep -qE "^\[profile ${PROFILE}\]\$" ~/.aws/config; then
  echo "✗ Profile '$PROFILE' not found in ~/.aws/config" >&2
  exit 1
fi

shift
[[ "${1:-}" == "--" ]] && shift

SOURCE=$(sso_source_profile "$PROFILE")

cat >&2 <<EOF
${YELLOW}${BOLD}→ Reminder: make sure your SSO session is active.${RESET}

  ${BOLD}Local (browser):${RESET}
    ${CYAN}aws sso login --profile ${SOURCE}${RESET}

  ${BOLD}Remote / EC2 (no browser):${RESET}
    ${CYAN}aws sso login --profile ${SOURCE} --use-device-code${RESET}

→ Launching claude with AWS_PROFILE=${PROFILE}

EOF

exec env \
  CLAUDE_CODE_USE_BEDROCK=1 \
  AWS_PROFILE="$PROFILE" \
  AWS_REGION=us-east-1 \
  ANTHROPIC_MODEL=us.anthropic.claude-sonnet-4-6 \
  ANTHROPIC_SMALL_FAST_MODEL=us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  claude "$@"
