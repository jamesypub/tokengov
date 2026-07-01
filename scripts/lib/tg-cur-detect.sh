#!/usr/bin/env bash
# tg-cur-detect.sh — pure (AWS-free) helpers for CUR 2.0
# detect → classify → choose. Sourced by tg-cur-deploy.sh; the AWS
# enumeration (bcm-data-exports list-exports / get-export) lives in
# the caller, which feeds the classified fields here. Kept side-effect
# free so scripts/ci/tg-cur-detect-test.sh can drive every branch with
# no AWS, no deploy.
#
# These functions echo a single token and return 0; the caller
# branches on the token. No `set -e` assumptions, no globals written.

# tg_cur_classify <is_cur2> <iam_principal> <include_resources>
#                 <s3_region> <install_region>
#
# Decide whether one discovered export is reuse-worthy. Args mirror the
# GetExport field values (TRUE/FALSE/empty):
#   is_cur2            'TRUE' if DataQuery.TableConfigurations carries
#                      COST_AND_USAGE_REPORT (it's CUR 2.0, not FOCUS).
#   iam_principal      INCLUDE_IAM_PRINCIPAL_DATA (creation-only; its
#                      absence is disqualifying, not fixable).
#   include_resources  INCLUDE_RESOURCES (needed for resource_id→model).
#   s3_region          DestinationConfigurations.S3Destination.S3Region.
#   install_region     the region tg is installing into (us-east-1).
#
# Echoes 'valid' when reuse-worthy, else 'invalid:<reason>' where
# reason is one of: not_cur2 / no_iam_principal / no_resources /
# region_mismatch / no_s3. The reason drives the operator-facing "why
# it can't be reused" message.
tg_cur_classify() {
  local is_cur2="$1" iam_principal="$2" include_resources="$3"
  local s3_region="$4" install_region="$5"

  if [[ "$is_cur2" != "TRUE" ]]; then
    echo "invalid:not_cur2"; return 0
  fi
  # IAM principal first — it's the most common + creation-only defect,
  # and the most useful reason to surface.
  if [[ "$iam_principal" != "TRUE" ]]; then
    echo "invalid:no_iam_principal"; return 0
  fi
  if [[ "$include_resources" != "TRUE" ]]; then
    echo "invalid:no_resources"; return 0
  fi
  if [[ -z "$s3_region" || "$s3_region" == "None" ]]; then
    echo "invalid:no_s3"; return 0
  fi
  if [[ "$s3_region" != "$install_region" ]]; then
    echo "invalid:region_mismatch"; return 0
  fi
  echo "valid"
}

# tg_cur_reason_text <reason>
# Human-readable explanation for an 'invalid:<reason>' classification,
# for the present-but-invalid installer message.
tg_cur_reason_text() {
  case "$1" in
    not_cur2)
      echo "it isn't a CUR 2.0 export (no COST_AND_USAGE_REPORT)" ;;
    no_iam_principal)
      echo "it has no IAM-principal allocation data (a creation-only \
setting that can't be added later)" ;;
    no_resources)
      echo "it doesn't include resource IDs (needed to attribute \
per-model spend)" ;;
    region_mismatch)
      echo "its S3 data is in a different region (CUR is us-east-1 \
only here)" ;;
    no_s3)
      echo "its S3 destination couldn't be resolved" ;;
    *)
      echo "it isn't usable by tg" ;;
  esac
}

# tg_cur_decide <num_valid> <answer>
#
# Map the operator's prompt answer to a decision token, given how many
# valid-for-reuse candidates exist. Pure: no prompting here (the caller
# reads the answer; tests pass it directly).
#
#   num_valid  count of reuse-worthy candidates.
#   answer     the operator's char (case-insensitive): R / C / A, or
#              empty (bare Enter).
#
# Echoes: reuse / create / abort / invalid.
# Rules (owner): a bare Enter on the valid-found prompt must NOT
# silently reuse — it's 'invalid' (re-prompt). 'R' is only meaningful
# when a valid candidate exists.
tg_cur_decide() {
  local num_valid="$1" answer="$2"
  local a
  a=$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')
  case "$a" in
    r)
      if [[ "$num_valid" -gt 0 ]]; then echo "reuse"
      else echo "invalid"; fi ;;
    c) echo "create" ;;
    a) echo "abort" ;;
    *) echo "invalid" ;;   # bare Enter / anything else → re-prompt
  esac
}

# tg_cur_should_delete_export <export_name> <tg_owned_name>
#
# The self-heal delete (an export missing INCLUDE_IAM_PRINCIPAL_DATA is
# delete+recreated) must fire ONLY for tg's OWN export name — never a
# discovered foreign export. Echoes 'yes' / 'no'. This is the core
# safety property; the caller must consult it before any delete-export.
tg_cur_should_delete_export() {
  local export_name="$1" tg_owned_name="$2"
  if [[ -n "$export_name" && "$export_name" == "$tg_owned_name" ]]
  then echo "yes"; else echo "no"; fi
}
