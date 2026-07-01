# Day-to-day login commands

Once docs/idc-okta-setup.md is done, these are the commands you run
to get a working claude -> Bedrock session. Blocks below are written
for bulk copy-paste; each fenced block can be selected and pasted as
a unit.


## 1. One-time: AWS profile config

The `~/.aws/config` profile is the single source of truth in
[onboarding-new-user.md](onboarding-new-user.md) §1 — set up the
`tg-developer-<PILOT-ACCOUNT>` profile there (IDC permission set
`tg-Developer`, `sso_role_name`) and come back here for the
day-to-day commands.

The wrappers below default `CCWB_PROFILE` to
`tg-developer-<PILOT-ACCOUNT>` — set it to the exact profile name
you used in the onboarding step.


## 2. One-time: shell wrappers

Paste this whole block into ~/.zshrc (zsh) or ~/.bashrc (bash),
then run `source ~/.zshrc` (or open a new shell). Defines:

    sso <args>          aws CLI, pinned to the CCWB_PROFILE profile
    sso-login           log in (device code, works on headless hosts)
    sso-whoami          print the current caller identity
    sso-logout          clear local SSO token (Okta cookie kept)
    sso-logout-hard     wipe all SSO + assume-role caches
    sso-logout-okta     print the URL to end the Okta-side session
    claude-bedrock      run claude with Bedrock env pinned

```bash
# ---- Claude Code on Bedrock via AWS IDC SSO ----
export CCWB_PROFILE="tg-developer-<PILOT-ACCOUNT>"
export CCWB_REGION="us-east-1"
export CCWB_MODEL="us.anthropic.claude-opus-4-7"
export CCWB_OKTA_SIGNOUT_URL="https://integrator-5499086.okta.com/login/signout"

sso() {
    aws --profile "$CCWB_PROFILE" "$@"
}

sso-login() {
    aws sso login --profile "$CCWB_PROFILE" --use-device-code
}

sso-whoami() {
    aws sts get-caller-identity --profile "$CCWB_PROFILE"
}

sso-logout() {
    aws sso logout --profile "$CCWB_PROFILE"
    echo "Local AWS token cleared. Okta cookie still active."
    echo "To fully sign out of Okta: run 'sso-logout-okta'."
}

sso-logout-hard() {
    aws sso logout --profile "$CCWB_PROFILE" 2>/dev/null
    rm -rf "$HOME/.aws/sso/cache/"* "$HOME/.aws/cli/cache/"* 2>/dev/null
    echo "All local SSO + assume-role caches wiped."
}

sso-logout-okta() {
    echo "Open this URL in the browser where you logged into Okta:"
    echo "    $CCWB_OKTA_SIGNOUT_URL"
    echo
    echo "For a truly fresh login, also clear site data for"
    echo "integrator-5499086.okta.com (Chrome: lock icon ->"
    echo "Site settings -> Clear data)."
}

claude-bedrock() {
    AWS_PROFILE="$CCWB_PROFILE" \
    AWS_REGION="$CCWB_REGION" \
    CLAUDE_CODE_USE_BEDROCK=1 \
    ANTHROPIC_MODEL="$CCWB_MODEL" \
        claude "$@"
}
# ---- end Claude Code on Bedrock ----
```

Why wrappers instead of `export AWS_PROFILE=tg-developer-<PILOT-ACCOUNT>`:
exporting the profile redirects every aws call in the shell, including
unrelated cross-account work. Wrappers scope the override per-command.


## 3. Daily: log in and run claude

SSO session expires after 4h (the permission set's session duration).
When you see a token-expired error, just re-run sso-login.

```bash
sso-login
sso-whoami
claude-bedrock
```

Or one-shot:

```bash
sso-login
claude-bedrock -p "say hi"
```

On a Mac with a local browser you can drop --use-device-code. The
wrapper uses device code by default because it works on both Mac and
headless EC2. It's slightly more clicks on a Mac but never breaks.


## 4. Verifying things

Print the full ARN (should contain AWSReservedSSO_tg-Developer
and end with your Okta username):

```bash
sso-whoami
```

Bedrock-without-claude sanity check. If claude fails but this works,
it's a claude-env problem; if this fails, the SSO session or
permission set is the culprit.

```bash
sso bedrock-runtime converse --region us-east-1 \
    --model-id us.anthropic.claude-opus-4-7 \
    --messages '[{"role":"user","content":[{"text":"hi"}]}]' \
    --query 'output.message.content[0].text' --output text
```

(`sso` is the wrapper from §2 — it pins `--profile $CCWB_PROFILE`.)


## 5. Logging out

Three levels, pick based on what you want:

| Goal                                     | Command            |
|------------------------------------------|--------------------|
| Force a fresh AWS login next time        | sso-logout         |
| Wipe all local token caches (debugging)  | sso-logout-hard    |
| Also end the Okta-side session           | sso-logout-okta    |

Important: `aws sso logout` only clears local AWS tokens. The Okta
browser cookie keeps letting you re-login silently (no password
prompt) until it expires or you hit the signout URL.


## 6. Common errors

Missing required SSO configuration values
    Forgot --profile. Use the sso / sso-login / sso-whoami wrappers.

token expired / session expired
    Run: sso-login

127.0.0.1 refused to connect (after clicking Allow)
    You are on a remote host. Wrapper uses --use-device-code by default.
    If you typed `aws sso login` directly, add --use-device-code.

AccessDeniedException from Bedrock
    The permission set's inline policy does not allow the chosen model.
    See idc-okta-setup.md Phase 6 — extend the Resource list if needed.

ValidationException: on-demand throughput isn't supported
    Use a US CRIS inference-profile ID (us.anthropic.claude-opus-4-7),
    not the bare foundation-model ID.
