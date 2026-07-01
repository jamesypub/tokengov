# Admin setup (reference)

> **First-time installer?** Use [INSTALL.md](../INSTALL.md) — it's the
> linear walkthrough. This doc is the **reference**: parameter knobs,
> day-2 ops, teardown, and what's NOT in this deploy. Once your install
> is working, this is what you come back to.

---

## What gets deployed

`scripts/tg-local-install.sh` (local path) and `scripts/tg-ecs-install.sh`
(ECS path) deploy the same CFN stacks:

| Stack | Purpose |
|---|---|
| `tg-bedrock-role` | `tg-consumer` (assume-role) + `tg-BedrockQuotaDeny` (managed policy mutated by reconciler) |
| `tg-container-stack` *(ECS path only)* | ECS cluster + ALB + RDS Postgres + ECR |
| `tg-cur-athena` | CUR 2.0 export + Glue catalog + Athena workgroup — the spend source |
| `tg-permission-sets` *(optional, IDC mgmt account)* | `tg-Developer` + `tg-Admin` IDC permission sets (see [INSTALL.md](../INSTALL.md) §"Configure IDC permission sets") |

Plus the **api** + **worker** containers themselves: FastAPI on
`/api/*`, APScheduler-driven worker, Postgres backing store.

The api auto-seeds the bootstrap admin (`TG_BOOTSTRAP_ADMIN_EMAIL`)
into the `admin_roles` table on first startup.

---

## Prerequisites

- **CLI session has installer permissions** in the target account
  (`tg-installer` role from [INSTALL.md](../INSTALL.md) §Pre-flight
  step 1).
- **Bedrock model access granted** in the target region for
  Anthropic Claude Sonnet 4.6, Haiku 4.5, Opus 4.7.
  Console → Bedrock → Model access.
- **You know which IAM principal trusts `tg-consumer`.** The
  install script writes a default trust (your installer principal);
  override via `TRUSTED_SAML_PROVIDER_ARN` or `TRUSTED_PRINCIPALS` if
  your dev SSO role is different.
- **CUR pre-flight (optional, only if you want spend reconciliation):**
  In the AWS Billing console, enable "Include resource IDs" and
  "IAM principal data" in your CUR settings. Without these,
  per-user spend attribution from CUR will be empty.
  `tg-cur-deploy.sh` handles the rest.

---

## Auth provider choices

`TG_AUTH_PROVIDER` (set before `tg-ecs-install.sh` /
`tg-local-install.sh`) decides who can log into the admin UI.
Three modes; pick one:

| Mode      | When to pick it                                                                | What you need                                                                                  |
|-----------|--------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------|
| `cognito` | **Default — recommended.** Pilots, demos, anyone without an existing IdP.      | Nothing extra. The install stack provisions a Cognito User Pool and seeds the bootstrap admin. |
| `okta`    | You already run Okta and want SSO. Adds "Sign in with Okta" to the Cognito UI. | An Okta admin to wire a SAML app. See [cognito-okta-federation.md](cognito-okta-federation.md). |

Notes:

- `cognito` and `okta` share the **same** Cognito User Pool —
  switching is additive and reversible. `okta` is `cognito` plus
  a SAML IdP wired into the same pool.
- **You can configure SSO entirely at runtime — no redeploy.** An
  org-admin sets the SAML IdP connection (metadata URL, provider
  name, email-attribute mapping) and the sign-in button label from
  **Settings → Authentication**; tg applies it to the live Cognito
  pool. The build-time Okta params below are an optional bootstrap
  that seeds that runtime config on first boot. See
  [Configure SSO from Settings](#configure-sso-from-settings).
- Switching modes after install: prefer Settings → Authentication.
  (Re-running the install script with a new `TG_AUTH_PROVIDER` still
  works and is idempotent; Cognito resources stay if they exist.)

The bootstrap admin (`TG_BOOTSTRAP_ADMIN_EMAIL`) is seeded into
`admin_roles` regardless of which mode you pick — it's the
identity the api recognises, not a login credential.

### Configure SSO from Settings

You can wire (or rename, or remove) a SAML IdP **at runtime** —
no rebuild, no redeploy. **Settings → Authentication** has two
sign-in method choices:

1. **Username & password (Cognito)** — the default.
2. **Company login (SAML)** — federate to your IdP. The pilot path
   is Okta → AWS IAM Identity Center: IAM Identity Center integrates
   a third-party web app as a **SAML 2.0** customer-managed
   application, so the trust that reaches your IdP is SAML. No new
   Okta app is needed — IAM Identity Center reuses your existing
   Okta → IDC federation.

For Company login you provide:

- a **login button label** (defaults to “Login with Your SSO”),
- the **IdP metadata URL** (preferred — Cognito auto-refreshes it)
  or an uploaded metadata XML,
- a **provider name** and the **email attribute** to map.

Settings also shows the **values to register on the IdP side**. The
SAML service provider is **Cognito** (tg is an OIDC client of the
pool behind it), so register Cognito’s values in IAM Identity
Center: the ACS URL
(`https://<domain>.auth.<region>.amazoncognito.com/saml2/idpresponse`)
and the SP entity id (`urn:amazon:cognito:sp:<user-pool-id>`), plus
the `email` attribute mapping. Cognito publishes no SP-metadata
document, so enter those two values manually.

A bad metadata URL surfaces the IdP error inline — not a 500 — so
you can fix it and re-apply.

**Org-admin break-glass recovery (lockout escape).** A wrong SAML
config can’t lock everyone out. On the login page a low-prominence
**“Trouble signing in?”** link (and the same link on the sign-in
**error page**) reveals an **org-admin-only** recovery that signs in
with a Cognito username/password and **bypasses SSO** — it lands on
Cognito’s native password page without calling the (possibly broken)
IdP, so it works even when SAML is misconfigured. It is restricted to
**org admins**: while SSO is the configured method, a non-org-admin
member cannot use the password bypass to get in (they must sign in via
SSO). Recovery sign-ins are logged. Keep at least one org admin with a
working Cognito password as your break-glass account. (The path is
still `/auth/login?identity_provider=COGNITO` under the hood — the link
just makes it discoverable and gates the landing to org admins.)

### Cognito quickstart (≈5 min)

The local/ECS install scripts deploy `tg-cognito-pool` for you
when `TG_AUTH_PROVIDER=cognito`. To deploy it standalone (or to
understand what the script does):

```
# 1. Deploy the Cognito stack
aws cloudformation deploy \
  --stack-name tg-cognito-pool \
  --template-file cfn/tg-cognito-pool.yaml \
  --parameter-overrides \
    BootstrapAdminEmail=$TG_BOOTSTRAP_ADMIN_EMAIL \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile $AWS_PROFILE

# 2. Capture stack outputs into env
eval $(aws cloudformation describe-stacks \
  --stack-name tg-cognito-pool \
  --query 'Stacks[0].Outputs[*].
    [join(`=`,[OutputKey,OutputValue])]' \
  --output text --profile $AWS_PROFILE)

# 3. Install with the Cognito provider
TG_AUTH_PROVIDER=cognito \
TG_OIDC_ISSUER=$OidcIssuer \
TG_OIDC_CLIENT_ID=$OidcClientId \
  scripts/tg-local-install.sh
```

The bootstrap admin receives a Cognito invitation email with a
temp password, clicks the link, sets a real password, and lands
on TG. **Adding further admins is one-click from the UI** — no
CLI needed (see next section).

### Adding Cognito admins from the UI ("Send invite")

When `TG_AUTH_PROVIDER=cognito`, the **Org Settings → Org Admins**
panel shows an extra checkbox under the grant form:

> ☑ **Also create Cognito user (sends invite email)**

Checked by default in Cognito mode. With it checked, granting an
admin does two things in one transactional step:

1. Calls `cognito-idp:AdminCreateUser` (with
   `DesiredDeliveryMediums=[EMAIL]`, so Cognito sends the
   invitation) — the new user gets a temp password by email.
2. Inserts the `admin_roles` row.

If the Cognito call fails, the admin row is **not** written (no
half-state where TG thinks the admin exists but they can't log
in). The UI confirms: *"Both email and Cognito user created. They
will receive an invitation email shortly."*

This requires the api/worker task role to hold
`cognito-idp:AdminCreateUser`, which is gated behind the
`EnableCognitoAdminProvisioning=true` CFN parameter on
`tg-container-stack` (scoped to your pool ARN only). Okta-only
installs (no Cognito-managed users) never carry this permission
and never show the checkbox.

### Cognito limitations

What you're choosing when you pick Cognito:

- ✗ **No SSO into AWS.** A Cognito-logged-in admin views the TG
  dashboard but does **not** get IAM creds. AWS-side operations
  (CLI-style actions on the underlying account) aren't available
  from a Cognito-only login — use Okta+IDC for those.
- ✗ **MFA via email/SMS only**, not TOTP authenticator apps,
  unless you enable Cognito advanced security features (extra
  cost).
- ✗ **Group-to-role mapping not yet implemented.** `admin_roles`
  is per-email; a Cognito group named `tg-admins` does **not**
  auto-grant `org_admin` (follow-up).
- ✓ **Same TG UI** as the Okta path.
- ✓ **Same RBAC** (`org_admin` / `team_admin`).
- ✓ **Same audit trail** (caller email recorded).

---

## Configuration knobs

All CFN parameters have sensible defaults. Common overrides:

| Variable | Default | Where it goes |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Bedrock region; affects CRIS ARNs in the role policy |
| `MAX_SESSION_SECONDS` | `28800` (8h) | TTL for `tg-consumer` STS sessions, max 12h |
| `TG_BOOTSTRAP_ADMIN_EMAIL` | unset | Email seeded as the first `org_admin` |
| `TG_ALERT_EMAIL` | `$TG_BOOTSTRAP_ADMIN_EMAIL` | Where quota alerts go |

Set as env vars before the install script, or pass via
`--parameter-overrides` on individual `aws cloudformation deploy` calls.

---

## After the deploy

### How admins reach the UI

Admins reach the `org_admin` UI via the **web login** — the
Cognito invitation email, or OIDC/Okta if you wired it. **The web
login is the only admin entry path.** (There is no desktop client
to download; an earlier `tg-admin` client and its
`tg-BedrockAdmin` role-chain were retired.)

- Local install: `http://localhost:8000` (or via SSH tunnel if api is
  on a remote host)
- ECS install: `http://<alb-dns>/` (or the public-IP task URL printed
  by `tg-ecs-install.sh`)

### Verify spend attributes to the right identity

Spend is sourced from the **Cost and Usage Report**, which lands
on the AWS bill ≤24h after usage (CUR delivery cadence). Once CUR
has delivered, confirm a developer's usage attributes to their
email via the CUR `line_item_iam_principal` column — query it in
Athena (workgroup `tg-cur-analytics`):

```sql
SELECT line_item_iam_principal, SUM(line_item_unblended_cost) AS usd
FROM tg_cur.data
WHERE line_item_product_code = 'AmazonBedrockService'
  AND bill_billing_period_start_date >= DATE_TRUNC('month', CURRENT_DATE)
GROUP BY 1 ORDER BY 2 DESC;
```

The Cost Reports page in the admin UI runs the equivalent saved
query. (CUR is billing data, so a brand-new developer's first
calls won't appear until the next CUR delivery.)

Each event's `identity.arn` should end in the user's email — that's
how the aggregator attributes spend per-user.

### Seed model pricing

The api ships with `model_pricing` empty. Until you populate it, the
metrics aggregator records token counts but writes `spend_usd = 0`.
Seed via the API:

```bash
curl -X PUT http://<api-host>:8000/api/pricing/<model-id> \
  -H 'Content-Type: application/json' \
  -d '{"input_per_1k_usd": 0.003, "output_per_1k_usd": 0.015}'
```

(Or use the Pricing page in the admin UI.)

---

## Day-2 operations

| Task | Where |
|---|---|
| Set / change a user cap | Users page → set cap, OR `PUT /api/users/<email>/cap` |
| Unblock a user | Users page → unblock, OR `DELETE /api/users/<email>/unblock` |
| Promote to team admin | Users page → admin role, OR `POST /api/admin-roles` |
| Edit model pricing | Pricing page, OR `PUT /api/pricing/<model-id>` |
| Review spend | Activity / Cost Reports pages, OR `GET /api/activity`, `/api/usage` |
| Export per-user spend | Cost Reports → Run (queries Athena via CUR — needs `tg-cur-athena`) |

See [quota-admin.md](quota-admin.md) for the quota workflow in detail.

---

## Teardown

```bash
# Local path
bash scripts/tg-local-destroy.sh

# ECS path
bash scripts/tg-ecs-destroy.sh
# To also drop the shared bedrock-* stacks:
bash scripts/tg-ecs-destroy.sh --full

# CUR (optional, separate)
bash scripts/tg-cur-destroy.sh
```

The local destroy script removes containers, the pgdata volume, and
both bedrock-* stacks. The ECS destroy script empties ECR, scales
services to 0, and deletes `tg-container-stack` (RDS + ALB + VPC).

---

## What's NOT in this deploy

- **No Okta tenant changes.** This deploy assumes the customer's
  Okta side already federates into the AWS account; we do not touch
  their tenant.
- **No IAM Identity Center admin rights required.** The pilot uses
  an IAM role developers assume from their existing SSO session.
- **No multi-tenant SaaS.** Everything runs in the customer's own
  AWS account.
- **No multi-region.** Single region per install (us-east-1 by
  default; CUR 2.0 only operates here anyway).
