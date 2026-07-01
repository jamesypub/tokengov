# Install — ECS Fargate (production-shape)

The default install ([INSTALL.md](../INSTALL.md)) brings the
stack up via `docker-compose` on your laptop or a single EC2
host. For shared / production-shape pilots you want ECS
Fargate behind an ALB with RDS Postgres.

Same prerequisites as the main install path: install is a
one-time action run with your **existing AWS admin profile**
(#474 dec 6c / #461 — the scoped `tg-installer` role was
retired from the happy path; see "Scoped installer role
(optional)" below). The cost is roughly $20/mo for the ALB on
top of compute.

## Required ECS-specific config

```bash
export AWS_PROFILE=<your-admin-profile>
export AWS_REGION=us-east-1
export TG_TARGET_ACCOUNT_ID=<12-digit account id>

# REQUIRED — public ingress allowlist + OIDC. Defaults are
# fail-closed (no internet ingress, login gate on); you
# must configure both before deploy.
export TG_ALLOWED_INGRESS_CIDRS="203.0.113.0/24,198.51.100.7/32"
export TG_OIDC_ISSUER=https://example.okta.com
export TG_OIDC_CLIENT_ID=<okta-app-client-id>
export TG_OIDC_CLIENT_SECRET=<okta-app-client-secret>
export TG_OIDC_REDIRECT_URI=http://<alb-dns>/auth/callback
```

The client secret is stored in AWS Secrets Manager and
injected into the api task at launch (never plaintext in the
task definition). On a **re-run/upgrade** you may leave
`TG_OIDC_CLIENT_SECRET` unset — the value already in Secrets
Manager is preserved. Set it again only to rotate.

Optional:

```bash
# export TG_AUTH_REQUIRE_LOGIN=0  # opens SPA (only with a
                                  # tight CIDR allowlist)
```

## TLS — pick one (the install fails with none set)

The installer is **cert-agnostic** (#484): it never generates
or imports a cert. HTTP is never silent — with no TLS choice
the deploy fails fast. Choose exactly one:

```bash
# 1. Bring your own ACM cert → HTTPS :443
export TG_CERT_ARN=arn:aws:acm:us-east-1:...:certificate/...

# 2. Self-signed (stage/dev — AWS won't issue a public cert
#    for *.elb.amazonaws.com). Generate + import out-of-band,
#    then pass the printed ARN:
ARN=$(scripts/tg-make-selfsigned-cert.sh \
  --cn <alb-dns-or-your-domain>)
export TG_CERT_ARN="$ARN"
#    (clients then need --insecure / -k; tg-admin --insecure)

# 3. Auto-issue a public ACM cert via Route53 DNS validation
export TG_ISSUE_ACM_CERT=1
export TG_DOMAIN_NAME=tg.example.com
export TG_HOSTED_ZONE_ID=Z0123456789ABCDEFGHIJ

# 4. HTTP only, on purpose (no TLS) — explicit opt-in
export TG_ALLOW_PLAINTEXT_ALB=1
```

The self-signed helper is standalone + idempotent (reuses a
valid `tg-managed`-tagged cert rather than piling up imports)
and runs under **your** admin creds — the installer/stack
roles get no `acm:*` permissions. Cert lifecycle (including
teardown) is yours: a self-signed/imported cert is not a
stack resource, so `tg destroy` won't delete it.

## Deploy

```bash
bash scripts/tg-ecs-install.sh
```

What happens (~12-15 min):

1. Pre-flight + ECS service-linked role idempotent create
2. CFN: `tg-bedrock-role`, `tg-container-stack`, `tg-cur-athena` (CUR 2.0 — the spend source)
3. Container image build + ECR push
4. ECS services scaled from 0 → 1 via CFN parameter update
5. Waits for ECS steady state + `/api/version` 200
6. Bootstrap admin auto-seeded on api startup

When it finishes the script prints the ALB endpoint:

```
http://<alb-dns>/api/version
```

ECS console:
<https://us-east-1.console.aws.amazon.com/ecs/v2/clusters/tg-cluster>

## Step A — `tg-consumer` is customer-owned (#474 6b)

The `tg-bedrock-role` stack (deployed in step 2 above) creates
**`tg-consumer`** — the IAM role your developers assume to
reach Bedrock — plus the empty **`tg-BedrockQuotaDeny`** managed
policy the quota reconciler mutates. These are the **governance
chokepoint you own**, conceptually separate from the app:

- They define *who* can invoke Bedrock and carry the per-user
  deny statements. Their trust policy (`TrustedIamPrincipals` /
  `TrustedSamlProviderArn`) is **yours to configure** — TG never
  decides who your developers are.
- The rest of the stack (api/worker/ALB/RDS) is the *app*; it
  only **reads** logs and **appends** deny statements to this
  role. You can rotate or re-scope `tg-consumer`
  independently.

For a fully separated deploy (Step A owned by a different team /
account boundary than the app), deploy `cfn/tg-bedrock-role.yaml`
on its own first and pass its outputs in; the bundled deploy
above is the convenience path.

## Install identity — run under your admin profile

The install runs with your **existing AWS admin profile**
(#474 6c / #461). The older scoped `tg-installer` IAM role +
its permission set were **removed entirely** (#591/#566D) — they
added a matched-pair-maintenance burden (the #459/#460 class of
bug) for a one-time operator action. There is no installer role
to provision: point `AWS_PROFILE` at an admin/SSO profile in the
target account and run `tg install`. (No service roles remain
after the #566 consolidation — smaller standing IAM surface.)

## Auth + ingress allowlist (read carefully)

**Layer 1 — `TG_ALLOWED_INGRESS_CIDRS`.** Comma-separated
CIDRs that may reach the ALB. Up to 4 slots wired into CFN.
Empty = no ingress (fail-closed). The script rejects
`0.0.0.0/0` outright.

```bash
# Office egress + a single VPN exit IP
TG_ALLOWED_INGRESS_CIDRS="203.0.113.0/24,198.51.100.7/32"

# A bastion + your laptop's current public IP
TG_ALLOWED_INGRESS_CIDRS="10.20.30.40/32,$(curl -s ifconfig.me)/32"
```

**Layer 2 — OIDC login gate.** When
`TG_AUTH_REQUIRE_LOGIN=1` (default), anonymous browser
requests get 302'd to `/auth/login`; admins reach the UI
through this web login. Only `/api/version` and `/api/csrf`
stay open (all other `/api/*` require an authenticated
session).

Configure your Okta app first, then export the four
`TG_OIDC_*` vars. The redirect URI must match the ALB DNS
printed by the install script — easiest to deploy once with
a placeholder, read the ALB DNS from the script output, set
the real URI in Okta, then re-run the install (idempotent).

You can set `TG_AUTH_REQUIRE_LOGIN=0` to open the SPA, but
only with a tight CIDR allowlist. The installer refuses to
turn both layers off.

## Endpoint

The ALB is the only endpoint (#497): production-shape, ALB on
:80 (and :443 with a cert) with a stable DNS name; the api task
runs in private subnets behind it (~$20/mo). Internal vs
internet-facing is controlled by `AlbScheme` (#495); the SG is
locked to your `TG_ALLOWED_INGRESS_CIDRS` either way. The old
`TG_ENABLE_ALB=false` public-task-IP demo path is retired.

## Tear down

```bash
bash scripts/tg-ecs-destroy.sh

# Also drop the shared bedrock-* stacks:
bash scripts/tg-ecs-destroy.sh --full
```

Empties ECR, scales services to 0, deletes
`tg-container-stack` (ALB + RDS + VPC + everything). Takes
~10-15 min for RDS.

### RDS data protection on teardown

The stack **always creates** an RDS Postgres 16 instance, and
`DataProtection` defaults to **`disposable`** (#474 dec 3) — so
teardown leaves a true clean slate: the DB is deleted, no final
snapshot, no orphaned paid storage, no re-create blocks.

If this deployment holds data that must survive a stack delete,
opt into protection before deploying:

```bash
export TG_DATA_PROTECTION=protected   # Snapshot on delete,
                                      # DeletionProtection on
```

### Reuse an existing database (escape hatch)

TG always provisions its own RDS — there is **no guided "reuse
my DB" wizard branch**. To point the app at a database you
already run instead, set `DATABASE_URL` in the api/worker
environment (it takes precedence over the in-stack `DB_*`
wiring; see `container/db/session.py`). You own that database's
lifecycle, networking (app tasks must reach it), and schema
bootstrap. This is a deliberate escape hatch, not a supported
one-click path.
