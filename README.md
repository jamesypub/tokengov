# Token Governance for Amazon Bedrock

Per-user dollar quotas on Amazon Bedrock with no changes to your
identity provider. Token Governance is **principal-agnostic** — it
governs whatever principal appears in the Cost and Usage Report
(federated SSO, IAM user, or machine role), deny-only, however they
authenticated. It federates through your existing identity — any
OIDC, SAML, or Cognito provider — into a single IAM role; spend and
tokens come from AWS CUR 2.0 (the sole spend source), and quota
enforcement is IAM Deny — all in the customer's own AWS account.

tg is also **model- and tool-agnostic**: because it governs spend at
the Bedrock/CUR layer, it caps any model and any client — Claude
Code, Cowork, Codex, Kiro, or any AI tool running on Amazon Bedrock —
without per-tool integration.

**[▶ See it in action — product demo](docs/demo.md)**

---

## Who uses it — four experiences

What each role actually sees and does. Pick yours; the linked doc
has the step-by-step.

**Installer** — one-time. You run a single command (`scripts/tg
install`) to deploy the stacks into your own AWS account, either
local docker-compose or ECS Fargate. We recommend running the
install with Administrator rights, since it creates IAM roles,
policies, and other account-level resources. After that you hand off
to an org-admin and never touch it again unless you upgrade.
→ [INSTALL.md](INSTALL.md)

**Org-admin** — the full web UI. You set the default monthly
budget, **restrict selected models** with the org-wide
blocked-models denylist, and decide who the other admins are; you
can cap any user and you see **all** spend across every team (Cost
Reports, Activity, Users, Teams). You manage a **multi-level team
structure** with **data visibility scoped to that structure** — each
admin sees only their own subtree's spend. You can also configure
single sign-on via **IDC / SAML in addition to Cognito**. This is
the control seat for the whole deployment.
→ [docs/admin-setup.md](docs/admin-setup.md) (day-2 quota ops:
[docs/quota-admin.md](docs/quota-admin.md))

**Team-admin** — the same UI, **scoped to your team and its
sub-teams**. You see and cap only the users in your subtree; there's
no Org Settings and no org-wide spend view. Good for delegating
budget management without handing over the whole org.
→ [docs/admin-setup.md](docs/admin-setup.md)

**Developer** — you use Claude Code (or any Bedrock client) under
your quota; nothing changes in your workflow until you go over cap,
at which point Bedrock returns `AccessDeniedException` naming
`tg-BedrockQuotaDeny`. You can also sign in to the web UI as a
**member** to see your own month-to-date spend against your cap.
→ [docs/onboarding-new-user.md](docs/onboarding-new-user.md)

If you cloned this repo, you are the **Installer**. Go to [INSTALL.md](INSTALL.md).

---

## What is this product

A drop-in spend-governance layer for Amazon Bedrock that customers
deploy in their own AWS account. It gives org admins:

- **Per-user dollar caps** enforced against AWS-billed spend
  (CUR 2.0, up to ~24h lag)
- **Hard enforcement** via IAM Deny when a user exceeds their cap —
  not just an alert
- **Spend reconciliation** against AWS CUR 2.0 for the authoritative
  bill
- **An admin web UI** served by the FastAPI backend, reached over
  the browser via Cognito login or OIDC/Okta.
- **Spend vs. velocity** — measure spend against delivery signals
  from GitHub and Jira *(experimental)*

It works because every Bedrock invocation carries the caller's
identity in the AWS (IAM) session ARN, so AWS CUR 2.0 attributes the
billed cost per user. A worker syncs that CUR spend hourly, sums it
per user, and the reconciler writes a targeted Deny statement when
anyone is over cap.

**No identity-provider changes required.** The auth model is "trust
whatever AWS session/role the caller already uses."

---

## Architecture

Callers assume one scoped IAM role (in this install, via your
existing SSO); spend is attributed per user from AWS CUR 2.0 (the
sole spend source); the reconciler
writes a per-user IAM Deny once billed spend (CUR 2.0, ≤24h
lagged) crosses the cap.

**[Full architecture — diagram, components →](docs/architecture.md)**

Two install paths, one app:

| Path | Where it runs | Best for |
|---|---|---|
| Local | docker-compose on a laptop or EC2 | Pilot / demo / dev loop |
| ECS  | Fargate behind an ALB (us-east-1) | Production-shape pilot |

Either path deploys the same CFN stacks:

| Stack | Purpose |
|---|---|
| `tg-bedrock-role` | `tg-consumer` IAM role + `tg-BedrockQuotaDeny` managed policy |
| `tg-container-stack` *(ECS path only)* | ECS cluster + ALB + RDS Postgres + ECR |
| `tg-cur-athena` | CUR 2.0 export + Glue catalog + Athena workgroup — the spend source |

The app itself runs in two containers (`api` + `worker`) backed by
Postgres. On the local path, docker-compose runs Postgres on the host;
on the ECS path, it's RDS.

---

## Repo layout

```
admin-ui/    React SPA (admin-ui/web), served by the api
cfn/         CloudFormation templates for every stack
container/   FastAPI api + APScheduler worker (the live app)
docker-compose.yml  postgres + api + worker for the local path
docs/        Reference docs and runbooks
scripts/     Install/destroy/test orchestration
```

---

## Status

Production-ready for pilot scale.
Local docker-compose and ECS Fargate paths are both supported.

---

## Related docs

- [INSTALL.md](INSTALL.md) — installer's linear walkthrough
- [docs/admin-setup.md](docs/admin-setup.md) — admin reference + day-2 ops
- [docs/onboarding-new-user.md](docs/onboarding-new-user.md) — developer onboarding: keep your login, see your spend
- [docs/quota-admin.md](docs/quota-admin.md) — day-2 quota operations
- [docs/roles-and-permissions.md](docs/roles-and-permissions.md) — IAM role design
- [docs/idc-okta-setup.md](docs/idc-okta-setup.md) — add tg to an existing AWS IDC (or wire IDC↔Okta from scratch)
- [configure company SSO (SAML via IDC) at runtime](docs/idc-okta-setup.md#ask-a-register-tgs-admin-console-as-a-saml-app-in-idc) — Settings → Authentication
- [docs/design-rationale.md](docs/design-rationale.md) — why this shape
