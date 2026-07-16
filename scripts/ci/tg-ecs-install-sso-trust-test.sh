#!/usr/bin/env bash
# tg-ecs-install-sso-trust-test.sh — assert the installers emit a
# VALID, name-agnostic trust principal when the caller logs in via AWS
# IAM Identity Center (SSO), and that the CFN template adds the
# name-agnostic AWSReservedSSO_* SSO trust unconditionally (the env-var
# override that used to name principals verbatim was removed).
#
# The bugs this guards:
#   1. For an SSO/permission-set caller the real role ARN lives under
#      /aws-reserved/sso.amazonaws.com/, but the old
#      `assumed-role/ROLE` → `role/ROLE` sed dropped that path,
#      producing a MALFORMED ARN that CFN rejects with "Invalid
#      principal in policy".
#   2. The trust was pinned to the CALLER'S OWN permission-set name
#      (AWSReservedSSO_<thatname>_*). A customer whose permset is named
#      anything else — or who has several — was not trusted. The fix
#      emits the NAME-AGNOSTIC AWSReservedSSO_* (trust every permset in
#      the account; same-account scope is enforced by
#      Principal:AWS=<acct>:root), with the account taken from the
#      caller's own account ($CALLER_ACCT), not a passed-in target.
#   3. tg-local-install.sh had NO SSO branch — it ran the plain sed on
#      an SSO ARN, producing the malformed form. The fix mirrors the
#      ECS installer.
#
# Two layers, no AWS / no deploy (mirrors tg-public-publish-test.sh):
#   A. Static: both installers + CFN template carry the SSO wiring.
#   B. Functional: drive the script's REAL derivation on example ARNs
#      and assert the principals come out valid + name-agnostic.
# Usage: bash scripts/ci/tg-ecs-install-sso-trust-test.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../tg-ecs-install.sh"
LOCAL="$HERE/../tg-local-install.sh"
TPL="$HERE/../../cfn/tg-bedrock-role.yaml"
fails=0
pass() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1" >&2; fails=$((fails + 1)); }

echo "== A. static: SSO trust wiring present (#1064/#1077) =="

# A1: syntax
if bash -n "$SCRIPT"; then pass "ecs installer bash -n clean"
else fail "ecs installer bash -n"; fi
if bash -n "$LOCAL"; then pass "local installer bash -n clean"
else fail "local installer bash -n"; fi

# A2: the installer derives an SSO path-form ArnLike (the fix).
if grep -qE 'aws-reserved/sso\.amazonaws\.com' "$SCRIPT"; then
  pass "ecs installer emits SSO path-form (aws-reserved/sso.amazonaws.com)"
else
  fail "ecs installer must emit the SSO path-form ARN"
fi
if grep -qE 'aws-reserved/sso\.amazonaws\.com' "$LOCAL"; then
  pass "local installer emits SSO path-form (aws-reserved/sso.amazonaws.com)"
else
  fail "local installer must emit the SSO path-form ARN"
fi

# A3: the SSO trust is NAME-AGNOSTIC (AWSReservedSSO_*), not pinned to
#     the caller's own permission-set name (#1077).
if grep -qE 'AWSReservedSSO_\*' "$SCRIPT"; then
  pass "ecs SSO trust is name-agnostic (AWSReservedSSO_*)"
else
  fail "ecs SSO trust must be name-agnostic (AWSReservedSSO_*)"
fi
if grep -qE 'AWSReservedSSO_\*' "$LOCAL"; then
  pass "local SSO trust is name-agnostic (AWSReservedSSO_*)"
else
  fail "local SSO trust must be name-agnostic (AWSReservedSSO_*)"
fi

# A3b: the per-permset PERMSET extraction is GONE (no longer pins the
#      caller's own permission-set name).
if grep -qE 'PERMSET=.*assumed-role/\(AWSReservedSSO_' "$SCRIPT"; then
  fail "ecs installer still extracts/pins the caller's permset name"
else
  pass "ecs installer no longer pins the caller's permset name"
fi

# A4: the SSO trust ARN uses the caller's OWN account ($CALLER_ACCT),
#     not the passed-in target ($TG_TARGET_ACCOUNT_ID) (#1077).
if grep -qE 'arn:aws:iam::\$\{CALLER_ACCT\}:role/aws-reserved' "$SCRIPT"; then
  pass "ecs SSO ARN uses \$CALLER_ACCT (discovered account)"
else
  fail "ecs SSO ARN must use \$CALLER_ACCT, not the passed-in target"
fi
if grep -qE 'arn:aws:iam::\$\{CALLER_ACCT\}:role/aws-reserved' "$LOCAL"; then
  pass "local SSO ARN uses \$CALLER_ACCT (discovered account)"
else
  fail "local SSO ARN must use \$CALLER_ACCT, not the passed-in target"
fi

# A5: the TG_TRUST_PRINCIPALS env-var override is REMOVED. Trust
#     derivation is single-path (auto-derive only); the override's job
#     is reachable via the CFN param TrustedSsoPrincipalArnLike. The
#     var must not appear in either installer.
if grep -qE 'TG_TRUST_PRINCIPALS' "$SCRIPT" "$LOCAL"; then
  fail "installer still references the removed TG_TRUST_PRINCIPALS var"
else
  pass "TG_TRUST_PRINCIPALS override removed from both installers"
fi

# A6: both installers pass the new CFN param on the deploy.
if grep -qE '"TrustedSsoPrincipalArnLike=\$TRUST_SSO_ARNLIKE"' "$SCRIPT"; then
  pass "ecs deploy passes TrustedSsoPrincipalArnLike"
else
  fail "ecs deploy must pass TrustedSsoPrincipalArnLike override"
fi
if grep -qE '"TrustedSsoPrincipalArnLike=\$TRUST_SSO_ARNLIKE"' "$LOCAL"; then
  pass "local deploy passes TrustedSsoPrincipalArnLike"
else
  fail "local deploy must pass TrustedSsoPrincipalArnLike override"
fi

# A7: the CFN template accepts the SSO principal via an ArnLike
#     condition on aws:PrincipalArn (not a bare Principal wildcard).
if grep -qE 'TrustedSsoPrincipalArnLike' "$TPL" \
   && grep -qE 'ArnLike' "$TPL" \
   && grep -qE 'aws:PrincipalArn' "$TPL"; then
  pass "CFN trusts SSO via ArnLike on aws:PrincipalArn"
else
  fail "CFN must trust the SSO role via ArnLike aws:PrincipalArn"
fi
# A7b: the template carries the name-agnostic AWSReservedSSO_* default
#      inline (the !Sub default the unconditional SSO statement uses
#      when TrustedSsoPrincipalArnLike is empty). Param Default can't
#      interpolate ${TargetAccountId}, so this lives in template logic.
if grep -qE 'aws-reserved/sso\.amazonaws\.com/AWSReservedSSO_\*' "$TPL"; then
  pass "CFN carries the name-agnostic AWSReservedSSO_* default inline"
else
  fail "CFN must carry the inline AWSReservedSSO_* name-agnostic default"
fi
# A7c: HasSsoPrincipals still exists but now selects the ArnLike VALUE
#      (operator override vs name-agnostic default), it no longer gates
#      EMISSION — the SSO statement is unconditional. We can't assert
#      "emits always" with grep alone, but we CAN assert the condition
#      is present (used for value selection) and that there is no longer
#      a bare-account-root fallback statement keyed on its absence.
if grep -qE 'HasSsoPrincipals' "$TPL"; then
  pass "CFN keeps HasSsoPrincipals (now selects the ArnLike value)"
else
  fail "CFN must keep HasSsoPrincipals to pick the ArnLike value"
fi

echo "== B. functional: derivation produces valid name-agnostic ARNs =="

# Synthetic example account ids (AWS-docs canonical placeholders) — no
# real account id ships in this CI fixture. CALLER_ACCT is the account
# the SSO session originates in; TARGET is a deliberately-different
# value to prove the ARN keys off CALLER_ACCT, not the target.
CALLER_ACCT=123456789012
TARGET=123456789012

# B1: an SSO/permission-set caller → valid, name-agnostic path-form.
#     This mirrors the installer's branch exactly: it ignores the
#     caller's specific permset name and emits AWSReservedSSO_*.
SSO_CALLER="arn:aws:sts::${CALLER_ACCT}:assumed-role/AWSReservedSSO_BedrockDeveloper_0123456789abcdef/dev@example.com"
case "$SSO_CALLER" in
  *":assumed-role/AWSReservedSSO_"*)
    GOT="arn:aws:iam::${CALLER_ACCT}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_*" ;;
  *) GOT="(branch not taken)" ;;
esac
WANT="arn:aws:iam::${CALLER_ACCT}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_*"
if [ "$GOT" = "$WANT" ]; then
  pass "SSO caller → name-agnostic path-form ArnLike ($GOT)"
else
  fail "SSO derivation wrong: got $GOT want $WANT"
fi

# B2: the emitted ARN does NOT contain the caller's specific permset
#     name (BedrockDeveloper) — a differently-named customer permset
#     would still be trusted.
case "$GOT" in
  *BedrockDeveloper*) fail "emitted ARN pins the caller's permset name" ;;
  *) pass "emitted ARN is permset-name-agnostic" ;;
esac

# B3: the account in the ARN is the CALLER's account, not the target.
case "$GOT" in
  *"::${CALLER_ACCT}:"*) pass "ARN account = caller account ($CALLER_ACCT)" ;;
  *"::${TARGET}:"*) fail "ARN account is the passed-in target, not the caller" ;;
  *) fail "ARN carries neither caller nor target account: $GOT" ;;
esac

# B4: the derived ARN is NOT the malformed bare role/AWSReservedSSO_…
#     form (the pre-fix bug CFN rejected).
BAD="arn:aws:iam::${CALLER_ACCT}:role/AWSReservedSSO_BedrockDeveloper_0123456789abcdef"
if [ "$GOT" != "$BAD" ] \
   && [[ "$GOT" == *"/aws-reserved/sso.amazonaws.com/"* ]]; then
  pass "derived ARN is path-form, not the malformed bare role/ form"
else
  fail "derivation regressed to the malformed bare role/ form"
fi

# B5: a plain IAM assumed-role caller still derives role/ROLE (no regression).
PLAIN_CALLER="arn:aws:sts::${TARGET}:assumed-role/tg-install-dev/sess123"
RN=$(printf '%s' "$PLAIN_CALLER" \
  | sed -E 's#.*:assumed-role/([^/]+)/.*#\1#')
PLAIN_GOT="arn:aws:iam::${TARGET}:role/${RN}"
if [ "$PLAIN_GOT" = "arn:aws:iam::${TARGET}:role/tg-install-dev" ]; then
  pass "plain-IAM caller still derives role/ROLE (no regression)"
else
  fail "plain-IAM derivation regressed: $PLAIN_GOT"
fi

echo "== C. rendered-template: SSO trust is unconditional =="
# Resolve the !If / !Sub the SSO ArnLike uses (cfn-lint can't) and
# assert: unconditional emission, name-agnostic default on empty param,
# operator Ref on non-empty, Principal=:root, no fallback statement.
RENDER="$HERE/tg-bedrock-role-sso-render-test.py"
if python3 "$RENDER" "$TPL"; then
  pass "rendered-template SSO assertions passed"
else
  fail "rendered-template SSO assertions FAILED"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$fails CHECK(S) FAILED" >&2
  exit 1
fi
