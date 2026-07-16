# Design rationale

## Why this shape

**Principal-agnostic, deny-only.** *Principal-agnostic* means tg
governs whoever calls Bedrock, no matter how they signed in.
tg governs whatever calling
principal appears in `/aws/bedrock/invocations` — IAM user,
federated/SAML, IDC permission-set role, or machine/service role —
regardless of how it reached Bedrock. It never mints or refuses
credentials; it subtracts via IAM Deny. The notes below describe how
*this pilot* is set up, not constraints tg imposes.

**No Okta changes (this pilot).** IDC is already a SAML app in corporate
Okta. All work is AWS-side, which the customer's team owns.

**This pilot federates via IDC.** IDC issues short-lived credentials —
zero credential rotation burden, MFA inherited from Okta, automatic
offboarding via Okta deprovisioning. An install whose principals are
IAM users or machine roles is equally governable; the federation
choice is a property of the pilot's setup, not a requirement.

**No per-user inference profiles needed.** CUR 2.0's
`line_item_iam_principal` column (the billing report's field naming
who made each call) gives native per-principal spend attribution
straight from the session ARN — no tagging or per-user provisioning
needed. See:
https://github.com/aws-solutions-library-samples/guidance-for-claude-code-with-amazon-bedrock/blob/main/assets/docs/COST_ATTRIBUTION.md

**Quota enforcement via Deny**, not by refusing to mint credentials —
because we don't mint them (AWS does). Spend is the AWS-billed cost
from CUR 2.0, lagged up to ~24h; enforcement converges on the next
reconciler pass. Acceptable for monthly token budgets.

## Non-goals (explicitly dropped)

- **OIDC / OAuth via Okta** — would require a new Okta app.
- **Custom `credential-process` binary** — the upstream `ccwb` tool's approach,
  unnecessary when IDC already handles credential issuance.
- **Per-user inference profiles** — provisioning overhead not justified when
  CUR 2.0 + invocation logs give the same attribution.
- **OTEL collector on ECS** — upstream ships this; invocation logs cover the
  same real-time dashboard need.

## Related

- **Upstream reference:** https://github.com/aws-solutions-library-samples/guidance-for-claude-code-with-amazon-bedrock
  — we intentionally do **not** depend on `ccwb` tooling; this repo is the narrower pilot.
- **Cost attribution guide:** https://github.com/aws-solutions-library-samples/guidance-for-claude-code-with-amazon-bedrock/blob/main/assets/docs/COST_ATTRIBUTION.md
