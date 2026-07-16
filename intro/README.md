# Token Governance

## Tokens at full throttle. Spend on rails.

*Velocity, governed.*

## The problem

- Customers want to embrace Claude Code on Amazon Bedrock
  but fear runaway token spend.
- No clear signal between developer productivity and
  Bedrock spend — high usage might mean shipped features
  or a loop nobody noticed.
- Enterprise SSO, team structure, and visibility
  requirements slow adoption; some changes IT prohibits
  outright (new Okta apps, IAM users, per-user inference
  profiles).

## What TG is

A drop-in spend-governance layer for Amazon Bedrock that
deploys in the customer's own AWS account. SSO sessions
assume a single scoped IAM role; spend is attributed per
user via CUR 2.0; quota state reconciles into a managed
IAM Deny policy every five minutes.

[Architecture →](../docs/architecture.md) — diagram and
component-by-component overview for architects/admins.

## Five enterprise requirements TG meets

| Requirement | How TG covers it |
|---|---|
| **SSO without changes** | Reuses your existing IDC group. No new Okta app, no per-user inference profile. |
| **Team structure + visibility** | Hierarchical teams with admin scope inherited down the tree; Velocity & Cost dashboards per team. |
| **Per-user dollar caps** | Monthly USD caps per user, plus an org-wide default so new users aren't uncapped on day one. |
| **Hard enforcement** | Caps are reconciled into a managed IAM Deny policy within five minutes — not just dashboards. |
| **CUR-grade attribution** | CUR 2.0's `line_item_iam_principal` ties every Bedrock invoke to a real email; audited via Athena. |

## Open source + customizable

TG is open source. Deploy it in your own AWS account. No
SaaS, no third-party control plane, no shared multi-tenant
infrastructure. Adapt the source to your environment.

## How to install

See `INSTALL.md` at the repo root.

*Tokens at full throttle. Spend on rails.*
