# Install Token Governance for Amazon Bedrock

This guide takes you from a fresh checkout to signing in to the
admin console. **About 15 minutes.**

You run **one command** — `scripts/tg install` — and answer a few
plain questions. It does the rest.

> Already have it deployed and just need to log in as a developer?
> See [docs/onboarding-new-user.md](docs/onboarding-new-user.md).
> Running the deployed admin UI? See
> [docs/admin-setup.md](docs/admin-setup.md).

---

## Before you start

You need:

1. **An AWS account** you can deploy into, and **admin
   credentials** for it (an AWS CLI profile that can create IAM
   roles and CloudFormation stacks).
2. **A VPC with 2 subnets in 2 different Availability Zones.**
   The account's default VPC is fine.
3. **These tools installed locally:** AWS CLI v2, Docker, and
   Python 3.10 or newer. (Optional: `pip install -r
   scripts/python/requirements.txt` gives the nicer wizard
   prompts; without it the wizard falls back to plain text input.)
4. **An email address** for the first administrator. You'll sign
   in with it.
5. **The public IP you'll browse from.** Find it at
   <https://checkip.amazonaws.com>. (The console is locked to your
   IP — nobody else can reach it.)

Quick check that your credentials work:

```bash
aws sts get-caller-identity --profile <your-admin-profile>
```

---

## Step 1 — Get the code

Clone this repository and `cd` into it, then (optionally) pin your
AWS profile:

```bash
git clone <this repository's URL>
cd <the cloned directory>
export AWS_PROFILE=<your-admin-profile>
```

`AWS_PROFILE` is optional — leave it unset to use the default AWS
credential chain (instance role, SSO default, or `AWS_*` env
creds). Set it only to pin a specific profile. It's optional for
**both** the main install and the CUR step.

Before anything is deployed, the installer reports the credential
source it resolved and **who you are** — e.g. `Using AWS profile:
tg-install-…` then `Logged in as: arn:aws:sts::…/… (account …)`. If
your credentials are missing or expired it stops **up front** with the
fix (for an SSO profile: `aws sso login --profile <name>`), so a stale
session fails clearly instead of mid-install.

---

## Step 2 — Run the installer

```bash
scripts/tg install
```

That's the **only** command you need. It asks a few questions,
each with a plain explanation and a sensible default you can
accept by pressing Enter:

| Question | What to type |
|---|---|
| **AWS region** | Press Enter for `us-east-1` (recommended — cost reporting only operates there). |
| **Who can reach the console** | Your browsing IP from <https://checkip.amazonaws.com>, as `YOUR.IP.HERE/32`. |
| **Container image** | Press Enter for the prebuilt public image `public.ecr.aws/e9y1g4o2/tg-container:latest` (recommended — no Docker needed). Type `build` to build from source instead. |
| **Admin email** | The address you'll sign in with. |
| **HTTPS certificate** | Pick "generate a self-signed cert" for a quick start (your browser shows a one-time warning), or paste an ACM certificate ARN if you have one. |

> 💡 You don't choose whether to turn on login or cost reporting —
> those are always on. The installer just tells you it's doing
> them. Cost reporting (CUR 2.0) is how tg attributes per-user
> spend on the Cost Reports page.

> 📦 **The prebuilt image is public** — the installer pulls it for
> you, but you can also pull it directly:
> ```bash
> docker pull public.ecr.aws/e9y1g4o2/tg-container:latest
> ```
> No AWS auth or ECR login needed (it's a public ECR gallery
> image). Type `build` at the Container image prompt if you'd
> rather build from source.

Then it shows a summary and deploys. **This takes about 10
minutes** (most of it waiting for the database). You can walk
away; if you stop it, just run `scripts/tg install` again and it
picks up where it left off.

**Login provider.** `tg install` is **Cognito-only** — the installer
stands up the login for you, no Okta tenant and no extra questions.
The install is a single pass (no redirect-URI pause). **SAML/OIDC
federation to your own IdP (Okta, Ping, Azure AD, …) is configured
*after* install**, not during it: tg flips from owning the directory
to federating via a runtime setting (the admin IdP-config screen),
with no redeploy. For the step-by-step runtime SSO setup (SAML via AWS
IAM Identity Center, configured from Settings → Authentication), see
[Ask A in docs/idc-okta-setup.md](docs/idc-okta-setup.md#ask-a-register-tgs-admin-console-as-a-saml-app-in-idc).
If your users reach Bedrock **directly from an IAM
Identity Center permission-set role** (rather than assuming
`tg-consumer`), see [Ask B in
docs/idc-okta-setup.md](docs/idc-okta-setup.md#ask-b-attach-the-deny-policy-reference-to-a-permission-set)
for the `scripts/tg-idc-quota-permset.sh` reference an IDC admin runs
to layer the tg deny on durably.

> **Tip — use a custom domain for a stable sign-in URL.** Without
> a domain, the console address is the load balancer's generated
> hostname, which **changes if the load balancer is ever
> recreated** — and a Cognito invite email already sent keeps the
> old address, so its link can stop resolving. Set a domain
> (`TG_DOMAIN_NAME` + `TG_HOSTED_ZONE_ID`, optionally
> `TG_ISSUE_ACM_CERT=1`) and the sign-in URL never churns. If you
> skip the domain and an invite link goes dead, re-send it (see
> the troubleshooting table below) — the installer also warns at
> the end of a deploy if the addresses have drifted apart.

**Preview without deploying:**

```bash
scripts/tg install --dry-run   # show the plan + env, change nothing
```

---

## Step 3 — Sign in

When it finishes, the installer prints exactly what to do — it
looks like this:

```
✓ Install complete — tg is running.

Next step — open the admin console and sign in:
  1. Open:        https://<your-address>/
  2. Sign in as:  you@example.com
  3. You're the first admin — set up everyone else from here.

Signing in (you're the first admin):
  • If you set an admin password during install, sign in with it.
  • Otherwise click "Forgot password" — a reset code is emailed
    to you@example.com; set your password, then sign in.
```

1. **Open the URL** in your browser. (If you chose a self-signed
   certificate, your browser warns once that it's not trusted —
   that's expected; click through.)
2. **Sign in.** On a Cognito install the first admin is
   **pre-confirmed**, so you have two ways in:
   - **You set a password at install** (the wizard asked, or you
     passed `TG_BOOTSTRAP_ADMIN_PASSWORD`) → sign in with it
     directly.
   - **You left it blank** (the default) → the installer set a
     random throwaway it never shows you and confirmed the
     account. Click **Forgot password**, enter the code emailed to
     your admin address, and set your own password.

   Either way **Forgot password works immediately** (the account
   is confirmed). Once you federate to your own IdP post-install,
   the login redirects through it instead.
3. **You're in.** The Users page lists you as the only
   administrator. Add people and set token budgets from here.

**Bringing people into the login.** On a user's detail page,
**Enable login** authorizes them and (on a Cognito deployment)
emails them — they click **Forgot password** to set their own
password. Pick their role: **Member** (no admin access),
**Team admin**, or **Org admin**. On a deployment federated to your
own IdP, "Enable login" only authorizes them — they sign in via your
SSO, no email. (The member's in-app view is still being built; a
member who signs in lands on a limited page for now.)

---

## Check it anytime

```bash
scripts/tg status
```

Shows the stack status, the console URL, and the admin email.

---

## Cost reporting (CUR 2.0) — required

CUR 2.0 is the **sole spend + discovery source** — it's how the
Cost Reports page attributes per-user $, how the Activity page's
spend is populated (both read the same CUR-synced data), and how
the deny reconciler's billed-MTD caps are computed. So it is a
**required** part of `scripts/tg install` (the `tg-cur-athena`
stack), the same tier as the VPC / RDS / ALB / ECS the installer
always creates — there is **no opt-out**, and a CUR deploy failure
**fails the install** (re-run `tg install` once the cause is fixed;
it resumes idempotently and self-heals a broken CUR stack). The
installer also verifies the wiring afterward. (Billed CUR data
backfills over ~24–48h, so Cost Reports shows an "awaiting data"
state at first, never an error.)

There is no `--no-cur` flag, and `TG_SKIP_CUR` is no longer honored
(a cloud install fails fast with a clear message if it's set, so a
stale automation env can't silently expect the old behavior).
`--with-cur` still parses but is a deprecated no-op. More detail:
[docs/install-cur-athena.md](docs/install-cur-athena.md).

**Already have a shared/org CUR export?** The installer **detects**
every CUR 2.0 export in the account and **validates** which are usable
by tg (a CUR 2.0 export with IAM-principal allocation data + resource
IDs, in us-east-1). If it finds a usable one, it **offers a choice**:

- **Reuse** it — tg points its Glue DB (`tg_cur`) + Athena workgroup
  (`tg-cur-analytics`) at the existing export's S3 location and creates
  **no** second export (no duplicate billing). On reuse, the app's task
  role is granted read-only access to that one bucket so it can query
  the CUR.
- **Create** tg's own `tg-bedrock-cur` export alongside the existing
  one (the previous always-create behavior).

A bare Enter never silently reuses — you must pick **R** or **C**. A
**customer-owned export is never deleted**: if an existing export
*can't* be reused (e.g. it lacks IAM-principal data — a creation-only
setting that can't be added later), the installer explains why and
offers to create tg's own instead, leaving yours untouched. (Only tg's
*own* `tg-bedrock-cur` export is ever auto-recreated, and only when it
itself is missing the required flag.) Non-interactive installs default
to **create** (a pre-answer flag is a planned follow-up).

**Install permissions (now a hard prerequisite).** Because CUR is
required, the installer principal must be able to deploy it. CUR/BCM
export creation is **us-east-1-only and account-global**, so the
install now fails (not just degrades) if any of these are missing or
blocked:

- `cur:PutReportDefinition` / `bcm-data-exports:*` — and no org SCP
  denying them, and the account not already at its export-count
  limit (a second export can fail creation).
- S3 / Glue / Athena permissions for the `tg-cur-athena` stack
  (bucket create, Glue catalog, Athena workgroup), plus
  `athena:DeleteWorkGroup` (`--recursive-delete-option`) for the
  self-heal path that recreates a broken CUR stack.

---

## If something goes wrong

| What you see | What to do |
|---|---|
| `AccessDenied` errors | Your AWS profile isn't an admin in this account. Check with `aws sts get-caller-identity --profile <your-admin-profile>`. |
| "No TLS configured" and it stops | You skipped the certificate question. Re-run and pick a certificate option — the console is never served over plain HTTP by accident. |
| Deploy seems stuck (~8–15 min) | Normal on first install — the database takes a while. Only worry after 15 minutes, then check `aws cloudformation describe-stack-events --stack-name tg-container-stack`. |
| Login → 404 or redirect mismatch | Cognito installs shouldn't hit this (no redirect URI to register). If you've federated to your own IdP post-install, register the redirect URI the IdP-config screen shows. |
| Can't sign in | If you didn't set a password at install, use **Forgot password** to set one (a code is emailed to your admin address). Still stuck? Run `scripts/tg status` and check the console URL matches. |
| Invite link is a dead/non-resolving host | The load balancer was recreated after the invite went out, so the email's address is stale. Re-send the invite from the Admins page ("Re-send invite") — the new email carries the current address. (A `TG_DOMAIN_NAME` avoids this; the install also warns when the addresses drift.) |
| Cost Reports page is empty | Billing data arrives from AWS up to ~24 hours after the first usage. It fills in on its own. |
| `'tg-alb' already exists` / install rolled back, re-run won't proceed | A prior failed install left a resource behind that AWS CloudFormation can't reuse automatically. On a re-run the installer **detects** it, explains what it found, and offers a choice: **[d]** delete the leftover and continue (recommended when the previous install rolled back and nothing is serving traffic), **[r]** retry as-is, or **[a]** abort. For an unattended/scripted run, set `TG_ON_ORPHAN=delete` to clear it automatically (default is `abort` — it never deletes without your say-so). |

---

## Remove everything

```bash
scripts/tg destroy          # removes all tg- resources
scripts/tg destroy --full   # also drops the shared bedrock-* stacks
```

Removes everything the installer created. Run the installer again
to put it back. A self-signed cert you generated isn't a stack
resource, so teardown leaves it — delete it in ACM if you no
longer need it.

---

## For developers and testers only

**The supported way to install is `scripts/tg install`.** It is
the one front door — it always sets up the login wall so the
admin console is never exposed without a sign-in.

If you are running a **throwaway test environment** and want to
skip standing up login (for example, a local click-through on a
private network), set the test environment flag:

```bash
TG_ENVIRONMENT=dev scripts/tg install
```

In `dev`/`test` mode the installer allows login to be turned off
(`TG_AUTH_REQUIRE_LOGIN=0`) for convenience. **This is never
allowed on a real (`prod`) install** — there, login is always on
and the installer refuses to finish in an unauthenticated state.

For a local laptop / single-EC2 docker-compose stack (no ALB or
ECS), add `--local`:

```bash
scripts/tg install --local      # docker-compose: api + worker + postgres
scripts/tg destroy --local      # tear it down
```

> ⚠ Don't use `TG_ENVIRONMENT=dev` for anything reachable from the
> internet or holding real data. It also enables a test-only
> sign-in bypass.

---

## More detail

- [docs/admin-setup.md](docs/admin-setup.md) — admin reference and
  day-to-day operations
- [docs/onboarding-new-user.md](docs/onboarding-new-user.md) —
  developer setup once it's deployed
- [docs/install-ecs.md](docs/install-ecs.md) — the AWS resources
  the installer creates (ALB + RDS + ECR + ECS)
- [docs/install-cur-athena.md](docs/install-cur-athena.md) — cost
  reporting details
