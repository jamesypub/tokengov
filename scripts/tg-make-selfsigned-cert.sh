#!/usr/bin/env bash
# tg-make-selfsigned-cert.sh
#
# Standalone helper: generate a self-signed X.509 cert and
# import it into ACM, then print the resulting CertificateArn
# on stdout. #484 (#474 dec 6, Option D): the installer/stack
# are cert-agnostic — they consume a CertificateArn only and
# never generate or import a cert. This script is the relocated
# self-signed path: run it BEFORE installing (or let the #487
# wizard call it), then feed the printed ARN to the installer
# as TG_CERT_ARN (or `tg install --cert-arn <arn>`).
#
# Two ways to use it:
#   1. Standalone / CI / advanced operator:
#        ARN=$(scripts/tg-make-selfsigned-cert.sh \
#                --cn my-alb-1234.us-east-1.elb.amazonaws.com)
#        TG_CERT_ARN="$ARN" scripts/tg-ecs-install.sh
#   2. Wizard-invokable (#487): the wizard calls this when the
#      user picks "generate self-signed" and threads the ARN
#      into the install params.
#
# Runs under the OPERATOR's own AWS creds (admin) — the
# installer/stack roles gain NO acm:* permissions.
#
# AWS won't issue a public cert for *.elb.amazonaws.com, so a
# self-signed cert is the zero-cost option for stage/dev where
# you just need the ALB to speak HTTPS (browsers/clients then
# need --insecure / -k). For prod, bring a real cert ARN
# instead.
#
# Idempotent + side-effect-clean: re-running reuses an existing
# tg-managed self-signed cert for the same CN when it still has
# >30 days of validity, instead of piling up ACM imports. Pass
# --force to always import a fresh one. Imported certs are
# tagged for discoverability/cleanup:
#     tg-managed=true
#     tg-make-selfsigned=true
#     tg-cn=<common-name>
# Cert lifecycle is the OPERATOR's (#474 dec 6) — the cert is
# not a stack resource, so `tg destroy` does NOT delete it.
# To clean up after teardown, list + delete by tag, e.g.:
#   aws acm list-certificates --query \
#     "CertificateSummaryList[].CertificateArn" --output text
#   # then `aws acm delete-certificate --certificate-arn <arn>`
#   # for each one whose tg-managed tag == true and which is
#   # no longer referenced by a listener.
#
# Required:
#   --cn <name>          Common Name / SAN DNS for the cert
#                        (usually the ALB DNS name). May also
#                        be supplied as TG_CERT_CN.
#
# Optional:
#   --profile <name>     AWS profile (else AWS_PROFILE / default
#                        chain)
#   --region <region>    default us-east-1 (AWS_REGION honored)
#   --days <n>           cert validity in days (default 365)
#   --force              import a fresh cert even if a valid
#                        tg-managed one already exists
#   -h | --help          show this header
#
# Output:
#   The CertificateArn is printed to STDOUT (and nothing else
#   on stdout — all progress/log lines go to stderr), so
#   `ARN=$(scripts/tg-make-selfsigned-cert.sh --cn ...)` is safe.

set -euo pipefail

# ── Color helpers (to STDERR so stdout stays ARN-only) ──
step()  { printf '\n\033[1;34m== %s ==\033[0m\n' "$*" >&2; }
ok()    { printf '\033[1;32m✓\033[0m %s\n' "$*" >&2; }
warn()  { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
fail()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── Arg parsing ─────────────────────────────────────
CN="${TG_CERT_CN:-}"
PROFILE="${AWS_PROFILE:-}"
REGION="${AWS_REGION:-us-east-1}"
DAYS=365
FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cn)      CN="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    --days)    DAYS="$2"; shift 2 ;;
    --force)   FORCE=1; shift ;;
    -h|--help) sed -n '2,60p' "$0"; exit 0 ;;
    *) fail "Unknown arg: $1 (see --help)" ;;
  esac
done

[[ -n "$CN" ]] || fail "--cn <dns-name> is required \
(or set TG_CERT_CN). See --help."
[[ "$DAYS" =~ ^[0-9]+$ && "$DAYS" -gt 0 ]] || \
  fail "--days must be a positive integer (got '$DAYS')"

command -v openssl >/dev/null 2>&1 || \
  fail "openssl not found on PATH"
command -v aws >/dev/null 2>&1 || \
  fail "aws CLI not found on PATH"

# Build the --profile flag only when one is set, so the
# default credential chain still works in CI.
AWS_PROFILE_FLAG=()
[[ -n "$PROFILE" ]] && AWS_PROFILE_FLAG=(--profile "$PROFILE")

step "Self-signed cert for CN=$CN (region $REGION)"

# ── 1. Reuse an existing tg-managed cert if still valid ──
# Match on DomainName == CN AND the tg-managed tag, with
# >30 days left. ACM list-certificates doesn't return tags,
# so we filter candidates by DomainName then confirm the tag
# per-candidate.
if (( FORCE == 0 )); then
  CANDIDATES=$(aws acm list-certificates \
    "${AWS_PROFILE_FLAG[@]}" --region "$REGION" \
    --query "CertificateSummaryList[?DomainName=='${CN}'].CertificateArn" \
    --output text 2>/dev/null || true)
  for ARN in $CANDIDATES; do
    [[ -n "$ARN" && "$ARN" != "None" ]] || continue
    IS_TG=$(aws acm list-tags-for-certificate \
      "${AWS_PROFILE_FLAG[@]}" --region "$REGION" \
      --certificate-arn "$ARN" \
      --query "length(Tags[?Key=='tg-managed' && Value=='true'])" \
      --output text 2>/dev/null || echo 0)
    [[ "$IS_TG" == "1" ]] || continue
    NOT_AFTER=$(aws acm describe-certificate \
      "${AWS_PROFILE_FLAG[@]}" --region "$REGION" \
      --certificate-arn "$ARN" \
      --query Certificate.NotAfter --output text 2>/dev/null || true)
    [[ -n "$NOT_AFTER" && "$NOT_AFTER" != "None" ]] || continue
    EXPIRES_S=$(date -d "$NOT_AFTER" +%s 2>/dev/null || \
      date -j -f "%Y-%m-%dT%H:%M:%S" \
      "${NOT_AFTER%%+*}" +%s 2>/dev/null || echo 0)
    NOW_S=$(date +%s)
    DAYS_LEFT=$(( (EXPIRES_S - NOW_S) / 86400 ))
    if (( DAYS_LEFT >= 30 )); then
      ok "Reusing tg-managed cert (expires in ${DAYS_LEFT}d): $ARN"
      (( DAYS_LEFT < 60 )) && warn "Expires in ${DAYS_LEFT}d \
— re-run with --force near expiry to rotate."
      printf '%s\n' "$ARN"
      exit 0
    fi
    warn "Existing tg-managed cert expires in ${DAYS_LEFT}d \
(<30) — importing a fresh one."
  done
fi

# ── 2. Generate + import a fresh self-signed cert ────
TMPDIR_CERT=$(mktemp -d)
trap 'rm -rf "$TMPDIR_CERT"' EXIT

openssl req -x509 -newkey rsa:2048 -nodes -days "$DAYS" \
  -keyout "$TMPDIR_CERT/tls.key" \
  -out "$TMPDIR_CERT/tls.crt" \
  -subj "/CN=$CN" \
  -addext "subjectAltName=DNS:$CN" \
  2>/dev/null \
  || fail "openssl failed to generate self-signed cert"
ok "Generated self-signed cert (${DAYS}d) for $CN"

# #888: a tg- Name tag gives the cert a recognizable identity in the
# ACM console (the console's "Name" column reads the Name tag) so a
# tg-managed self-signed cert is obvious + greppable among others.
# Keep the existing tg-managed / tg-cn tags (the rotation logic above
# filters on tg-managed).
CERT_NAME="tg-selfsigned-$CN"
ARN=$(aws acm import-certificate \
  "${AWS_PROFILE_FLAG[@]}" --region "$REGION" \
  --certificate "fileb://$TMPDIR_CERT/tls.crt" \
  --private-key "fileb://$TMPDIR_CERT/tls.key" \
  --tags \
    "Key=Name,Value=$CERT_NAME" \
    "Key=tg-managed,Value=true" \
    "Key=tg-make-selfsigned,Value=true" \
    "Key=tg-cn,Value=$CN" \
  --query CertificateArn --output text) \
  || fail "aws acm import-certificate failed"
ok "Tagged Name=$CERT_NAME (visible in the ACM console)"
ok "Imported to ACM: $ARN"

warn "Self-signed: clients must use --insecure / -k. \
Pass this ARN as TG_CERT_ARN (or tg install --cert-arn)."

# Stdout: the ARN only.
printf '%s\n' "$ARN"
