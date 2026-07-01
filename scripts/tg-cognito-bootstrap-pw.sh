#!/usr/bin/env bash
# tg-cognito-bootstrap-pw.sh — set the bootstrap admin's Cognito
# password to a PERMANENT one so the user lands in CONFIRMED (not
# FORCE_CHANGE_PASSWORD). This is what makes forgot-password work:
# Cognito ForgotPassword requires CONFIRMED, and the temp-password
# invite both (a) may never arrive (COGNITO_DEFAULT sender gets
# spam-filtered) and (b) leaves the user FORCE_CHANGE_PASSWORD —
# locked out both ways. (#921; supersedes the #915 temp-password
# print, which kept FORCE_CHANGE_PASSWORD.)
#
# Two operator choices, BOTH ending CONFIRMED:
#   Option B — operator-provided: TG_BOOTSTRAP_ADMIN_PASSWORD set
#     (or typed at an interactive prompt). Logs in with it directly.
#   Option A — random throwaway (default/headless): a strong random
#     password is generated, set --permanent, and DISCARDED (never
#     printed, logged, or stored). Operator logs in via Forgot
#     password → reset code → their own password. The throwaway
#     exists only to flip the user to CONFIRMED.
#
# Selection: env var set → B; else interactive (TTY) → prompt (Enter
# = A); else headless → A automatically (never stalls).
#
# Sourced by tg-ecs-install.sh and tg-local-install.sh. Relies on the
# caller's helpers: ok()/warn()/fail() and the PROFILE_ARGS array.
# The secret is passed to AWS as an arg in a subshell with `set +x`
# so it can never leak via xtrace; it is never echoed.

# tg_cognito_pw_policy_ok <password> — true if it satisfies the
# tg-cognito-pool policy (MinimumLength 12; lower+upper+number;
# RequireSymbols false). Mirrors cfn/tg-cognito-pool.yaml.
tg_cognito_pw_policy_ok() {
  local p="$1"
  [[ ${#p} -ge 12 ]]        || return 1
  [[ "$p" == *[a-z]* ]]     || return 1
  [[ "$p" == *[A-Z]* ]]     || return 1
  [[ "$p" == *[0-9]* ]]     || return 1
  return 0
}

# tg_cognito_gen_pw — emit a strong random policy-compliant password.
# 24 random alnum bytes carry the entropy; the aA1 suffix guarantees
# every required class is present regardless of the random draw.
tg_cognito_gen_pw() {
  local rand
  rand=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)
  printf '%saA1' "$rand"
}

# tg_ensure_bootstrap_admin_user <pool_id> <email> <region>
# Idempotently create the bootstrap admin in the pool. (#937 — this
# replaces the create-only `AWS::Cognito::UserPoolUser` CFN resource,
# which has NO AlreadyExists tolerance: a pool UPDATE that merely
# reconciles the ALB callback URL re-evaluates that resource and fails
# `User ... already exists` → UPDATE_ROLLBACK, install exits 255. It
# bit the #935 self-heal every time tg-container-stack was recreated
# while tg-cognito-pool survived — the common case. MessageAction does
# NOT help: SUPPRESS only silences the invite email, the create still
# throws on an existing user; and a CFN-managed user would be DELETED
# if simply dropped from the template on the update — confirmed
# against the AWS CFN AWS::Cognito::UserPoolUser + DeletionPolicy docs.
# Owning it script-side (decouple, the ticket's Option 2) mirrors the
# tg_set_bootstrap_admin_password idiom right below: gate on
# admin-get-user, no-op when present.)
#
# AdminCreateUser with MessageAction=SUPPRESS — we never want the
# COGNITO_DEFAULT invite email (#921: it's spam-filtered AND leaves
# FORCE_CHANGE_PASSWORD; the password step below puts the user
# CONFIRMED). email_verified=true so forgot-password can reach them.
tg_ensure_bootstrap_admin_user() {
  local pool_id="$1" email="$2" region="$3" status

  status=$(aws cognito-idp admin-get-user \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$region" \
    --user-pool-id "$pool_id" --username "$email" \
    --query 'UserStatus' --output text 2>/dev/null || echo "")

  if [[ -n "$status" && "$status" != "None" ]]; then
    ok "Bootstrap admin $email already in pool — create skipped"
    return 0
  fi

  if aws cognito-idp admin-create-user \
       "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$region" \
       --user-pool-id "$pool_id" --username "$email" \
       --message-action SUPPRESS \
       --user-attributes \
         "Name=email,Value=$email" \
         "Name=email_verified,Value=true" \
       >/dev/null 2>&1
  then
    ok "Bootstrap admin $email created in pool"
  else
    fail "admin-create-user failed for $email — check the installer \
role has cognito-idp:AdminCreateUser on the pool, then re-run."
  fi
}

# tg_set_bootstrap_admin_password <pool_id> <email> <region>
# Sets a PERMANENT password (Option A or B) so the user is CONFIRMED.
# Idempotent: if the user is already CONFIRMED (e.g. an upgrade where
# the admin set their own password), leave it untouched — never
# clobber a working credential.
tg_set_bootstrap_admin_password() {
  local pool_id="$1" email="$2" region="$3"
  local status pw source

  status=$(aws cognito-idp admin-get-user \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$region" \
    --user-pool-id "$pool_id" --username "$email" \
    --query 'UserStatus' --output text 2>/dev/null || echo "")

  if [[ "$status" == "CONFIRMED" ]]; then
    ok "Bootstrap admin already CONFIRMED — password left untouched"
    return 0
  fi

  # ── choose the password + source ──
  if [[ -n "${TG_BOOTSTRAP_ADMIN_PASSWORD:-}" ]]; then
    # Option B — operator-provided via env.
    pw="$TG_BOOTSTRAP_ADMIN_PASSWORD"
    source="provided"
    if ! tg_cognito_pw_policy_ok "$pw"; then
      fail "TG_BOOTSTRAP_ADMIN_PASSWORD violates the password \
policy (need ≥12 chars incl. lower, upper, and a number). \
Choose a compliant password and re-run."
    fi
  elif [[ -t 0 ]]; then
    # Interactive — offer B, Enter = A. Read silently (no echo); the
    # value never hits the terminal or logs.
    local pw1 pw2
    echo "Set an admin password now, or press Enter to generate a" >&2
    echo "random one and use 'Forgot password' to set yours." >&2
    read -rsp "Admin password (Enter = random): " pw1 </dev/tty; echo >&2
    if [[ -z "$pw1" ]]; then
      source="random"
    else
      read -rsp "Confirm password: " pw2 </dev/tty; echo >&2
      if [[ "$pw1" != "$pw2" ]]; then
        fail "Passwords did not match — re-run and try again."
      fi
      if ! tg_cognito_pw_policy_ok "$pw1"; then
        fail "Password violates the policy (need ≥12 chars incl. \
lower, upper, and a number). Re-run and try again."
      fi
      pw="$pw1"
      source="provided"
    fi
    unset pw1 pw2
  else
    # Headless, no env var → Option A automatically (never stall).
    source="random"
  fi

  if [[ "$source" == "random" ]]; then
    pw="$(tg_cognito_gen_pw)"
  fi

  # ── set it PERMANENT (FORCE_CHANGE_PASSWORD → CONFIRMED) ──
  # Subshell with `set +x` so the secret can't leak via xtrace even
  # if the caller ran under `set -x`. Output discarded.
  if (
        set +x
        aws cognito-idp admin-set-user-password \
          "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$region" \
          --user-pool-id "$pool_id" --username "$email" \
          --password "$pw" --permanent
     ) >/dev/null 2>&1
  then
    if [[ "$source" == "random" ]]; then
      ok "Bootstrap admin set to CONFIRMED with a random throwaway \
password (discarded). Sign in via 'Forgot password' at the console \
URL — a reset code goes to $email."
    else
      ok "Bootstrap admin set to CONFIRMED with the password you \
provided. Sign in with it (forgot-password also works)."
    fi
  else
    # Don't print the secret. Fail loud — a non-CONFIRMED admin is
    # the locked-out state this whole change exists to prevent.
    unset pw TG_BOOTSTRAP_ADMIN_PASSWORD
    fail "admin-set-user-password failed for $email — the bootstrap \
admin is not CONFIRMED. Check the installer role has \
cognito-idp:AdminSetUserPassword on the pool, then re-run."
  fi
  unset pw TG_BOOTSTRAP_ADMIN_PASSWORD
}
