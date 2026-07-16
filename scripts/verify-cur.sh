#!/usr/bin/env bash
# Verify CUR + Glue + Athena are wired up correctly.
# Returns per-user Bedrock spend month-to-date.
#
# Usage:
#   AWS_PROFILE=<deploy-profile> ./scripts/verify-cur.sh
#
# Prerequisites:
#   - tg-cur-athena stack deployed
#   - CUR 2.0 "Include resource IDs" + "Include IAM principal data"
#     toggled ON in AWS Billing console (console-only; CFN cannot set these)
#   - Glue crawler has run at least once after first CUR delivery
#     (force it: aws glue start-crawler --name tg-cur-crawler --region us-east-1)

set -euo pipefail

# #985: disable the AWS CLI pager for every call in this script. The
# CLI opens its default pager (less) on table/json output, which left
# the deploy / tear-down stuck at a `(END)` prompt when this verify ran
# non-interactively. Empty AWS_PAGER is the documented "never page"
# setting and covers all `aws` invocations below.
export AWS_PAGER=""

WORKGROUP="${ATHENA_WORKGROUP:-tg-cur-analytics}"
DATABASE="${ATHENA_DATABASE:-tg_cur}"
REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-}"

AWS="aws"
[[ -n "$PROFILE" ]] && AWS="aws --profile $PROFILE"

SQL="
  SELECT
    REGEXP_EXTRACT(line_item_iam_principal,
      '/([^/]+)\$', 1)              AS email,
    ROUND(SUM(line_item_unblended_cost), 4) AS actual_usd,
    COUNT(*)                        AS line_items
  FROM \"${DATABASE}\".\"data\"
  WHERE line_item_product_code = 'AmazonBedrockService'
    AND line_item_iam_principal IS NOT NULL
    AND bill_billing_period_start_date =
          DATE_TRUNC('month', CURRENT_DATE)
  GROUP BY 1
  ORDER BY 2 DESC;
"

echo "==> Starting Athena query (workgroup: $WORKGROUP, db: $DATABASE)"

QID=$($AWS athena start-query-execution \
  --query-string "$SQL" \
  --query-execution-context Database="$DATABASE" \
  --work-group "$WORKGROUP" \
  --region "$REGION" \
  --query QueryExecutionId --output text)

echo "    execution id: $QID"
echo "==> Polling…"

while :; do
  STATE=$($AWS athena get-query-execution \
    --query-execution-id "$QID" \
    --region "$REGION" \
    --query 'QueryExecution.Status.State' \
    --output text)
  case "$STATE" in
    SUCCEEDED) break ;;
    FAILED|CANCELLED)
      REASON=$($AWS athena get-query-execution \
        --query-execution-id "$QID" \
        --region "$REGION" \
        --query 'QueryExecution.Status.StateChangeReason' \
        --output text 2>/dev/null || echo "$STATE")
      echo "✗ Query $STATE: $REASON" >&2
      exit 1 ;;
  esac
  sleep 1
done

echo "==> Results:"
# #985: project only the row VALUES (header + data rows), not the full
# ResultSet with its ResultSetMetadata/ColumnInfo — that dumped screens
# of column metadata as a giant table. --no-cli-pager belt-and-
# suspenders on top of the AWS_PAGER="" guard above.
#
# Use the [*] projection operator, NOT the [] flatten operator:
# Rows[*].Data[*] keeps the result 2-D (a list of rows, each a list of
# cell values), so `--output text` prints ONE ROW PER LINE
# (tab-separated cells: header, then each email / actual_usd /
# line_items row). `Rows[].Data[].VarCharValue` flattens to a single
# 1-D list, which `--output text` renders as one giant tab-joined line
# — every row on one line. A standalone JMESPath check on the Python
# list misses this because the defect is in the --output text
# rendering (1-D flatten → one line; 2-D `[*]` → row per line), not
# the data — so verify against the rendered output, not just the list.
$AWS athena get-query-results \
  --query-execution-id "$QID" \
  --region "$REGION" \
  --no-cli-pager \
  --query 'ResultSet.Rows[*].Data[*].VarCharValue' \
  --output text
