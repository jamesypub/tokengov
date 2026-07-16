#!/usr/bin/env bash
# Dev-laptop setup for Claude Code via Bedrock (IDC-federated).
#
# One-time: configures an AWS SSO profile, logs you in once (you'll be asked
# to open a URL in a browser), writes ~/.claude/settings.json so Claude Code
# uses Bedrock, and runs a verification invoke.
#
# Safe to re-run. Idempotent.
#
# Required env vars:
#   SSO_START_URL   IDC portal URL (e.g. https://your-idc-alias.awsapps.com/start)
#
# Optional env vars (sensible defaults):
#   SSO_REGION      (default: us-east-1)
#   BEDROCK_REGION  (default: us-east-1)
#   PROFILE_NAME    (default: bedrock)
#   ANTHROPIC_MODEL (default: us.anthropic.claude-sonnet-4-6)

set -euo pipefail

SSO_START_URL="${SSO_START_URL:?must export SSO_START_URL=https://your-idc-portal.awsapps.com/start}"
SSO_REGION="${SSO_REGION:-us-east-1}"
BEDROCK_REGION="${BEDROCK_REGION:-us-east-1}"
PROFILE_NAME="${PROFILE_NAME:-bedrock}"
ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-us.anthropic.claude-sonnet-4-6}"

AWS_CONFIG="${AWS_CONFIG_FILE:-$HOME/.aws/config}"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

step() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

command -v aws >/dev/null  || fail "aws CLI not found. Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
command -v jq >/dev/null   || fail "jq not found. Install: sudo apt install jq  (or brew install jq)"

step "Writing SSO profile to $AWS_CONFIG"
mkdir -p "$(dirname "$AWS_CONFIG")"
touch "$AWS_CONFIG"

# Remove any existing [profile $PROFILE_NAME] block + its sso-session block, then append fresh
python3 - "$AWS_CONFIG" "$PROFILE_NAME" <<'PY'
import sys, re, pathlib
path, profile = sys.argv[1], sys.argv[2]
p = pathlib.Path(path)
text = p.read_text() if p.exists() else ""
# Drop the profile block
text = re.sub(rf'(?ms)^\[profile {re.escape(profile)}\].*?(?=^\[|\Z)', '', text)
# Drop the matching sso-session block (named after profile)
text = re.sub(rf'(?ms)^\[sso-session {re.escape(profile)}\].*?(?=^\[|\Z)', '', text)
p.write_text(text.rstrip() + "\n" if text.strip() else "")
PY

cat >> "$AWS_CONFIG" <<EOF

[sso-session ${PROFILE_NAME}]
sso_start_url = ${SSO_START_URL}
sso_region = ${SSO_REGION}
sso_registration_scopes = sso:account:access

[profile ${PROFILE_NAME}]
sso_session = ${PROFILE_NAME}
sso_account_id = 123456789012
# NOT the IAM role renamed in #349. This is the IDC/SSO
# PERMISSION-SET name (sso_role_name → AWSReservedSSO_<name>).
# This profile uses the PRIMARY (direct) governance model: it has
# no role_arn/source_profile, so the dev invokes Bedrock AS this
# SSO role (the permission set carries bedrock:InvokeModel and the
# deny reconciler attaches tg-BedrockQuotaDeny to it). The
# secondary/optional model — chaining sts:AssumeRole into the
# tg-consumer role — is for locked-down IDC where the permission
# set can't carry Bedrock; it is NOT what this profile does. Leave
# as BedrockDeveloper. (#472)
sso_role_name = BedrockDeveloper
region = ${BEDROCK_REGION}
output = json
EOF
ok "Profile [${PROFILE_NAME}] written"

step "Logging in via SSO (a short URL + code will appear — open them in any browser)"
aws sso login --profile "$PROFILE_NAME" --use-device-code || fail "aws sso login failed"
ok "SSO login succeeded"

step "Verifying identity"
CALLER_ARN=$(aws sts get-caller-identity --profile "$PROFILE_NAME" --query Arn --output text)
echo "  $CALLER_ARN"
[[ "$CALLER_ARN" == *"AWSReservedSSO_BedrockDeveloper"* ]] \
  || fail "Expected ARN to include 'AWSReservedSSO_BedrockDeveloper'; got: $CALLER_ARN"
ok "Federated as BedrockDeveloper"

step "Writing ~/.claude/settings.json"
mkdir -p "$(dirname "$CLAUDE_SETTINGS")"
# Merge into existing settings.json (preserve other keys) if it exists
if [[ -s "$CLAUDE_SETTINGS" ]]; then
  jq --arg profile "$PROFILE_NAME" \
     --arg region "$BEDROCK_REGION" \
     --arg model "$ANTHROPIC_MODEL" \
     '.env = (.env // {}) + {
        CLAUDE_CODE_USE_BEDROCK: "1",
        AWS_PROFILE: $profile,
        AWS_REGION: $region,
        ANTHROPIC_MODEL: $model
      }' "$CLAUDE_SETTINGS" > "$CLAUDE_SETTINGS.tmp" && mv "$CLAUDE_SETTINGS.tmp" "$CLAUDE_SETTINGS"
else
  cat > "$CLAUDE_SETTINGS" <<EOF
{
  "env": {
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "AWS_PROFILE": "${PROFILE_NAME}",
    "AWS_REGION": "${BEDROCK_REGION}",
    "ANTHROPIC_MODEL": "${ANTHROPIC_MODEL}"
  }
}
EOF
fi
ok "Claude Code settings written"

step "Verifying Bedrock invoke works"
OUT=$(mktemp)
aws bedrock-runtime invoke-model \
  --profile "$PROFILE_NAME" --region "$BEDROCK_REGION" \
  --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":20,"messages":[{"role":"user","content":"say ok"}]}' \
  --cli-binary-format raw-in-base64-out \
  "$OUT" >/dev/null 2>&1 \
  && jq -r '.content[0].text' "$OUT" \
  || fail "Bedrock invoke failed. Check that your IDC user is in the BedrockPilot group."
rm -f "$OUT"
ok "Bedrock invoke succeeded"

cat <<EOF

$(printf '\033[1;32m✓ Setup complete.\033[0m') Run \`claude\` to start Claude Code against Bedrock.

To switch models, edit $CLAUDE_SETTINGS (ANTHROPIC_MODEL field):
  us.anthropic.claude-sonnet-4-6                    (default)
  us.anthropic.claude-haiku-4-5-20251001-v1:0       (cheap/fast)
  us.anthropic.claude-opus-4-7                      (expensive; under tighter quota)

To refresh SSO credentials (expire every ~8h):
  aws sso login --profile ${PROFILE_NAME}
EOF
