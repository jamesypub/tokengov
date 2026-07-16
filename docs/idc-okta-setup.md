# Add tg to your existing AWS IAM Identity Center

**Who this is for:** the team standing up tg (Token Governance)
inside an enterprise that **already runs** SSO — an Okta (or other
IdP) tenant federated into AWS IAM Identity Center (IDC), operated by
a **central identity/cloud team**. You do not click in the IDC
console yourself; you interact with that team through a support-ticket
process.

So this guide is not "wire a fresh account to Okta from scratch." It
is: **here is exactly what to ask your identity team for — as
paste-ready support tickets — to add tg to your already-working
Okta→IDC, with zero changes to the existing federation.**

There are only **two new things** to request:

- **Ask A —** register tg's admin-console (Cognito) as a **SAML app**
  in your existing IDC, so admins sign in with company SSO.
- **Ask B —** attach a **customer-managed-policy reference**
  (`tg-BedrockQuotaDeny`) to a **permission set** in the tg Bedrock
  account, so quota enforcement survives IDC re-provisioning.

Each ask below has a fill-in-the-blanks ticket you paste into your
ticketing system.

> **No existing IdP, or just piloting?** You don't need this guide at
> all. tg supports admin login via a self-contained Cognito User Pool
> (≈5 min, no external IdP). See
> [`admin-setup.md` § Auth provider choices](admin-setup.md#auth-provider-choices).
> Okta + IDC is the enterprise-SSO path; Cognito is the
> lowest-friction one. (If you are standing up a **brand-new** IDC
> from scratch, the appendix at the bottom has the full first-time
> Okta→IDC setup.)

---

> ## ⚠ Adding tg does NOT change your existing federation
>
> Nothing about your current Okta→IDC setup is touched. Adding tg
> **only ADDS**:
>
> 1. one **new SAML application** in IDC (Ask A), and
> 2. one **customer-managed-policy reference** on a permission set
>    (Ask B).
>
> It does **not** modify your Okta application, your IDC identity
> source, SCIM provisioning, or any existing user/group assignment.
> No new Okta app is required — IDC reuses the federation you already
> have.

---

## Two accounts (they may be the same one)

tg spans two logically distinct AWS accounts. Note which account each
ask targets:

| Account | What lives there | Which ask |
|---|---|---|
| **tg admin-console account** | tg's Cognito user pool (the SAML service provider for admin login) | **Ask A** |
| **tg Bedrock (member) account** | `tg-consumer` role + the `tg-BedrockQuotaDeny` deny policy | **Ask B** |

In many installs (including the pilot) these are the **same** account
— that's fine. The distinction matters only so you tell your identity
team the correct account id in each ticket.

---

## Ask A: Register tg's admin console as a SAML app in IDC

**Takeaway:** your identity team creates one custom SAML 2.0
application in the existing IDC; you provide the Cognito
service-provider values and paste back the metadata URL they return.

tg's admin console can use your company SSO instead of
username/password. The trust chain is:

```
Your IdP (e.g. Okta) → AWS IAM Identity Center (IDC) → Cognito → tg
```

tg's Cognito user pool is the SAML **service provider (SP)**; IDC is
the SAML **identity provider (IdP)**. Because IDC reuses whatever
upstream IdP it is already federated to, **no new application in your
upstream IdP is required.**

> **No-change guarantee (Ask A):** this reuses your existing Okta→IDC
> federation and adds exactly one IDC application. Your Okta app,
> identity source, and SCIM config are untouched.

**Three steps, in order:**

### Step A1 — Get the SP values from tg (you do this)

In the tg admin console (**tg admin-console account**), go to
**Settings → Authentication**. tg shows the **Cognito
service-provider values** to register in IDC:

- **ACS URL** (Assertion Consumer Service):
  `https://<your-cognito-domain>.auth.<your-region>.amazoncognito.com/saml2/idpresponse`
- **SP entity ID / audience:** `urn:amazon:cognito:sp:<your-user-pool-id>`
- **Email attribute:** `email` (tg matches users on their verified email)

> These are your **Cognito** values, not tg's — Cognito is the SP. tg
> computes them from your deployed pool, so copy them exactly as the
> page shows. Cognito publishes no SP-metadata file, so the identity
> team enters these two values manually.

### Step A2 — Support ticket to your identity team

Fill in the placeholders and paste this into your ticketing system:

```text
Subject: Create a custom SAML 2.0 application in IAM Identity Center

Please add ONE new custom SAML 2.0 application to our existing IAM
Identity Center. This does not change our Okta app, identity source,
SCIM, or any existing assignment — it only adds a new application.

Steps (IAM Identity Center console → Applications → Add application →
"Add a custom SAML 2.0 application"):

1. Display name: tg-saml
2. Application metadata → "Manually type your metadata values":
   - Application ACS URL     = <ACS_URL from tg Settings>
   - Application SAML audience = <SP_ENTITY_ID, i.e.
                                  urn:amazon:cognito:sp:<POOL_ID>>
     ("SAML audience" and "SP entity ID" are the same field.)
3. Edit attribute mappings (Actions → Edit attribute mappings):
   | User attribute in the application | Maps to value    | Format       |
   | Subject                           | ${user:email}    | emailAddress |
   | email                             | ${user:email}    | unspecified  |
   The `email` attribute is REQUIRED (Cognito needs an email claim).
4. Assign the following IDC group(s) to the app: <IDC_GROUP>

Please return to us: the application's IAM Identity Center SAML
metadata URL (looks like
https://portal.sso.<region>.amazonaws.com/saml/metadata/<id>).
```

### Step A3 — Apply it in tg (you do this)

Back in tg **Settings → Authentication**, switch to **Company login
(SAML)** and enter:

- **IdP metadata URL** = the IDC metadata URL the identity team
  returned.
- **Login button label** = e.g. `Login with <Your Company> SSO`.
- (The provider-name default is fine; email attribute = `email`.)

**Save.** tg applies the change to your live Cognito user pool with
**no redeploy**: it creates the SAML identity provider on the pool and
adds it to the app client. The Settings page then shows live status —
*IdP present*, *on app client*, and any error. A bad metadata URL
surfaces the IdP error inline (not a 500), so you can fix and re-apply.

The equivalent API call (what the page performs):

```
PUT /api/settings/saml
{
  "provider_name":    "<your-provider-name>",
  "metadata_url":     "https://portal.sso.<your-region>.amazonaws.com/saml/metadata/<id>",
  "email_attribute":  "email",
  "sso_button_label": "Login with <Your Company> SSO"
}
```

### Step A4 — Verify

- The **login page** now shows the **"Login with <your label>"**
  button in place of (or alongside) the password form.
- A user assigned to the IDC app completes the round-trip: click the
  button → IDC → (your upstream IdP) → back to tg, signed in.
- (Operator check) your Cognito pool lists the new SAML provider, and
  the app client's supported-identity-provider list includes it.

### Ask A notes

- **Email is the user key.** The IDC app **must** send an `email`
  attribute, or Cognito rejects the assertion and tg can't match the
  user.
- **No lockout — org-admin break-glass recovery.** A misconfigured
  SAML setup cannot lock everyone out. On the login page, a
  low-prominence **"Trouble signing in?"** link reveals an org-admin
  recovery that signs in with a username/password and **bypasses SSO**
  (it goes to Cognito's native password page, not your IdP), so it
  works even when the SAML connection is broken. The same link appears
  on the sign-in **error page**, so an error is never a dead end. This
  recovery is **org-admin-only** — a member cannot use the password
  bypass while SSO is the configured method. Keep at least one **org
  admin** with a working Cognito password as your break-glass account;
  recovery sign-ins are logged.
- **Removing SSO.** In Settings, pick username/password — an *Unsaved
  change* bar appears, and **Save** opens a confirm that names the
  consequence and reassures that the org-admin recovery login stays
  available. The picker is save-gated: the live login changes on Save,
  not when you click the radio.
- **Request signing is OFF (single-logout is degraded).** With AWS IDC
  as the IdP, tg does **not** enable Cognito SAML request signing.
  Signing is a pool-provider-level flag with no per-message toggle, so
  turning it on to sign the single-logout `LogoutRequest` would also
  sign the SP-initiated login `AuthnRequest` — and IDC's custom-SAML
  app **rejects a signed AuthnRequest** (`Federate 403`), so login
  never completes. tg keeps signing off so **login works**; the
  accepted tradeoff is that **single-logout is degraded** — after
  logging out you may land on the IDC access-portal screen instead of
  the tg `/login` page. This is expected. IDC does not validate SP
  request signatures for a custom-SAML app, so clean SLO cannot be
  recovered by re-enabling request signing; it would require a
  different mechanism.

---

## Ask B: Attach the deny-policy reference to a permission set

**Takeaway:** your identity team attaches a **customer-managed-policy
reference** (`tg-BedrockQuotaDeny`) to a permission set and provisions
it to the tg Bedrock account, so quota enforcement survives IDC
re-provisioning. You provide the account id, the policy name, and the
IDC group.

Under the primary (direct) model, governed users call Bedrock **from
their IDC permission-set role** (an `AWSReservedSSO_*` role), so the
deny has to land on that role. A direct `iam:AttachRolePolicy` there
is wiped the next time IDC re-provisions the permission set.

The durable path is a **customer-managed-policy reference** on a
permission set: **IDC owns the attachment** (so it survives
re-provision), while the tg reconciler keeps editing the policy
*content* in the member account.

> **No-change guarantee (Ask B):** this adds a policy reference to a
> (new or existing) permission set. It does not alter your existing
> permission sets or user/group assignments.

**Precondition:** `tg-BedrockQuotaDeny` **must already exist in the tg
Bedrock account** — the tg deny reconciler creates it. The reference
resolves to nothing until then. This is **org-instance only** (an IDC
*account instance* has no permission sets).

### Step B1 — Support ticket to your identity team

Fill in the placeholders and paste this into your ticketing system:

```text
Subject: Attach tg-BedrockQuotaDeny (customer-managed policy ref) to a
         permission set and provision to our Bedrock account

Please attach a customer-managed-policy REFERENCE to a permission set
and provision it to our tg Bedrock account. This does not change any
existing permission set or assignment — it only adds one reference.

Values we're providing:
  - Bedrock (member) account id : <MEMBER_ACCOUNT_ID>
  - Customer-managed policy name: tg-BedrockQuotaDeny
  - IDC group to assign         : <IDC_GROUP>
(Precondition: tg-BedrockQuotaDeny already exists in the member
account — the tg deny reconciler created it.)

Steps (from the IDC management / delegated-admin account, under a
profile with sso-admin permissions):

  # 1. Create the permission set (or reuse an existing one)
  aws sso-admin create-permission-set \
    --instance-arn <IDC_INSTANCE_ARN> \
    --name tg-QuotaDenyPermissionSet

  # 2. Reference tg-BedrockQuotaDeny (customer-managed) on it by name
  aws sso-admin attach-customer-managed-policy-reference-to-permission-set \
    --instance-arn <IDC_INSTANCE_ARN> \
    --permission-set-arn <PERMISSION_SET_ARN from step 1> \
    --customer-managed-policy-reference Name=tg-BedrockQuotaDeny

  # 3. Provision to the member account
  aws sso-admin provision-permission-set \
    --instance-arn <IDC_INSTANCE_ARN> \
    --permission-set-arn <PERMISSION_SET_ARN> \
    --target-id <MEMBER_ACCOUNT_ID> --target-type AWS_ACCOUNT

  # 4. Assign the governed IDC group to that permission set
  #    (create-account-assignment for <IDC_GROUP>)

Please return to us: the permission-set ARN and confirmation that
provisioning to <MEMBER_ACCOUNT_ID> succeeded.
```

> **Scripted convenience.** The four steps above are also wrapped by
> `scripts/tg-idc-quota-permset.sh`, which an IDC admin runs from the
> management account to create + reference + provision + assign in one
> pass (it always provisions and polls to a terminal `SUCCEEDED`, so
> governance is never silently off). The manual CLI and the script do
> the same thing — offer whichever your identity team prefers.

### Step B2 — On the tg admin side (you do this)

Once the permission-set reference is wired, an org/team admin governs
the IDC user from the admin UI like any other — **Users → the user →
Govern**. tg sets `governed=true` and the reconciler emits the
per-person deny; it does **not** attach to the `AWSReservedSSO_*` role
directly.

Until the reference above (or the user assuming `tg-consumer`) is in
place, the UI shows the govern action as **advisory** — it states this
precondition rather than implying hard enforcement, because tg can't
see the IDC management account. See
[quota-admin.md](quota-admin.md) Workflow 0.

---

## How a dev's role reaches Bedrock — direct vs chained

There are two ways a dev's IDC role reaches Bedrock under governance.
**Direct is the primary model; chained is the secondary fallback.**

**Primary — DIRECT.** The dev's IDC permission set carries
`bedrock:InvokeModel` itself, and the dev invokes Bedrock **as their
SSO role** (`AWSReservedSSO_*`). No role-chain, no `tg-consumer` in
the path. The deny reconciler governs that role directly (it attaches
`tg-BedrockQuotaDeny` to whatever role a governed principal uses) —
via the Ask B reference so the attachment survives re-provisioning.
This is the simplest setup and the one to reach for first.

**Secondary — CHAINED (locked-down-IDC fallback).** When the
permission set **can't** carry the Bedrock policy (org policy forbids
it, or it would be wiped on re-provision), keep the Bedrock
permissions on the **`tg-consumer` IAM role** (deployed by
`cfn/tg-bedrock-role.yaml`) and have the dev's IDC SSO session
**assume** it via STS role chaining. The permission set then only
needs to be a **landing role** that is (a) synced to the dev group and
(b) listed in the `tg-consumer` trust policy
(`TrustedIamPrincipals`). See
[roles-and-permissions.md](roles-and-permissions.md) — "Why IAM roles,
not IDC permission sets" — for the chained-role shape.

For the chained model, make sure the assumed-role ARN for the landing
permission set is included in `TrustedIamPrincipals` when you deploy
`cfn/tg-bedrock-role.yaml`, so the SSO session can chain into
`tg-consumer`. The direct model needs none of this — the profile
invokes Bedrock as the SSO role.

---

## Client-side configuration

On the client machine (your Mac, or an EC2):

1. Append the `~/.aws/config` profile. The canonical block lives in
   [onboarding-new-user.md](onboarding-new-user.md) §1 — the
   `tg-developer-<PILOT-ACCOUNT>` profile (`sso_role_name = tg-Developer`,
   `sso_account_id = <PILOT-ACCOUNT>`). Use that single source of truth
   rather than duplicating it here.

2. Log in:
   ```bash
   aws sso login --profile tg-developer-<PILOT-ACCOUNT>
   ```

   - **Caution (EC2):** on a headless box the default flow tries to
     open a browser and opens a local callback on `127.0.0.1:<random>`.
     Your remote browser can't reach that. Use **device-code flow**:
     ```bash
     aws sso login --profile tg-developer-<PILOT-ACCOUNT> --use-device-code
     ```
     Paste the short URL + code into any browser on your Mac, approve.

3. Verify:
   ```bash
   aws sts get-caller-identity --profile tg-developer-<PILOT-ACCOUNT>
   ```
   ARN should end with
   `assumed-role/AWSReservedSSO_tg-Developer_<hash>/<okta-username>`.

4. Invoke Bedrock via `claude` (set `ANTHROPIC_MODEL` to any model id
   your org allows — e.g. a US CRIS inference-profile id):
   ```bash
   AWS_PROFILE=tg-developer-<PILOT-ACCOUNT> AWS_REGION=us-east-1 CLAUDE_CODE_USE_BEDROCK=1 \
     ANTHROPIC_MODEL=<model-id> \
     claude -p "say hi"
   ```

---

# Appendix — Establishing Okta→IDC from scratch

> **Your identity team almost certainly already did this.** Skip this
> appendix unless you are standing up a **brand-new** IDC. The content
> below is the full first-time Okta→IDC federation; it is reference
> material, **not** a task for the tg team. For adding tg to an
> existing IDC, use Ask A + Ask B above.

Validated against the pilot account (`<PILOT-ACCOUNT>`) with an Okta
tenant. Day-to-day commands: see
[`login-commands.md`](login-commands.md).

## Appendix pre-reqs

- **AWS account** where you are an admin. Standalone is fine — the
  wizard turns it into the management account of a fresh Organization.
- **Okta tenant** where you are an admin (Integrator Free Plan works).
- `aws` CLI v2 on every client that will run `aws sso login`.

## Appendix parameters you'll pick

| Parameter | Example (pilot) | Notes |
|---|---|---|
| AWS account ID | `<PILOT-ACCOUNT>` | The 12-digit target account |
| AWS region | `us-east-1` | |
| Permission set name | `tg-Developer` | The IDC permission set devs land on |
| IDC group name in Okta | `aws_<acct>_<role>` legacy convention, any name works | Group name is an arbitrary label; IDC doesn't parse it. |
| Okta tenant | `<your-okta-tenant>.okta.com` | |

### Phase 1 — Enable IDC in AWS

1. Sign in to the AWS console for the target account, select the region
   you want (`us-east-1` here).
2. Open **IAM Identity Center** → **Enable**.
3. When asked *"Organization instance or Account instance"*, pick
   **Organization instance**. Standalone accounts become the management
   account of a fresh 1-account Organization automatically.
   - **Caution:** "Account instance" works but doesn't support
     `CustomerManagedPolicyReferences` in permission sets, which blocks
     the later quota-enforcement work. Don't pick it unless you know why.
4. After IDC provisions (~30s), record these values. You'll need them
   below and in your client config.

   ```
   Identity Store ID:    d-XXXXXXXXXX
   AWS access portal URL: https://d-XXXXXXXXXX.awsapps.com/start
   IDC Instance ARN:     arn:aws:sso:::instance/ssoins-XXXXXXXXXXXXXXXX
   ```

### Phase 2 — Add the Okta app

In the **Okta admin console** (not the end-user dashboard):

5. **Applications → Browse App Catalog → search `AWS IAM Identity Center`**
   → Add Integration.
   - **Caution:** The catalog also lists **"AWS Account Federation"**
     which looks nearly identical but produces a different SAML response
     shape. The *correct* one has description "Manage SSO access to your
     AWS accounts, roles, and applications." The underlying Okta template
     label may still show as `AWS Account Federation` on the app's General
     tab — that's expected. The display name is what matters.
6. Give the app a distinguishing label (e.g. `AWS IDC <account-id>`).
7. Save with defaults for now. You'll configure Sign On + Provisioning
   after IDC hands you the values in Phase 3.

### Phase 3 — Wire SAML trust (bidirectional)

In AWS IDC: **Settings → Identity source → Actions → Change identity source**
→ **External identity provider**.

8. Copy the **Service provider metadata** values AWS shows you:
   ```
   ACS URL:     https://us-east-1.sso.signin.aws/platform/saml/acs/<uuid>
   Issuer URL:  https://us-east-1.signin.aws.amazon.com/platform/saml/d-XXXXXXXXXX
   ```

9. **Back in Okta** → your AWS IDC app → **Sign On tab → Edit** → fill in
   **Advanced Sign-on Settings**:
   - `AWS SSO ACS URL` = ACS URL from step 8
   - `AWS SSO issuer URL` = Issuer URL from step 8
   - **Caution:** if this field is left blank, Okta posts the SAML
     assertion to a default endpoint (`signin.aws.amazon.com/saml`), which
     IDC doesn't listen on. Login will silently land on the Okta
     dashboard instead of returning to the CLI.
   - Under **Maximum App Session Lifetime**: check **Send value in
     response** and set to **90 Days**. Without this checkbox, Okta
     omits `SessionNotOnOrAfter` from the SAML assertion and IDC falls
     back to its own default (8 hours), ignoring whatever you've
     configured in IDC Settings → Session duration.
   - Save.

10. In Okta → same app → **Sign On tab → Metadata URL** → copy this URL.
    It looks like
    `https://<tenant>.okta.com/app/exk<id>/sso/saml/metadata`.

11. **Back in AWS IDC's "Change identity source" wizard** → upload Okta's
    metadata XML. Options:
    - Open the Metadata URL in a browser, save as XML, upload via
      "IdP SAML metadata → Choose file", **or**
    - Fill the three fields manually: IdP sign-in URL, IdP issuer URL,
      IdP certificate (export cert from the Sign On tab).
    - Type `ACCEPT` to confirm. Save.

After this, AWS IDC's "Identity source" page should show:
- `IdP sign-in URL` populated
- `IdP issuer URL` populated
- At least one valid certificate listed

### Phase 4 — Turn on SCIM provisioning

SCIM (automatic user-sync from Okta) keeps AWS's user list in step
with Okta — add or remove someone in Okta and the change flows to AWS
without manual re-entry.

In AWS IDC:

12. **Settings → scroll to "Automatic provisioning" → Enable**.
13. AWS returns a SCIM endpoint URL and a bearer token.
    - **Caution:** the token is shown once. Copy it immediately to a
      password manager.

In Okta → AWS IDC app:

14. **Provisioning tab → Configure API Integration** → paste SCIM URL +
    token → **Test API Credentials** → Save.
15. **Provisioning → To App → Edit** → enable:
    - Create Users
    - Update User Attributes
    - Deactivate Users

    Save.

### Phase 5 — Assign users and push groups to IDC

In Okta:

16. **Directory → Groups → Add Group** — create a group (e.g.
    `aws_<acct>_<role>`). Add members (your test Okta users).
17. Back to the AWS IDC app → **Assignments tab → Assign → Assign to
    Groups** → pick the group.
18. **Push Groups tab → Push Groups → by name** → pick the same group →
    Save.

Within ~30s the group + members sync to IDC. Verify on EC2 / admin
machine (using a profile that can read the target account's identity store):

```bash
aws identitystore list-users --identity-store-id d-XXXXXXXXXX --region us-east-1 \
  --query 'Users[].UserName' --output table
aws identitystore list-groups --identity-store-id d-XXXXXXXXXX --region us-east-1 \
  --query 'Groups[].DisplayName' --output table
```

### Phase 6 — Grant the IDC group a permission set that can reach Bedrock

(For the tg-specific attachment, use **Ask B** above. This phase
covers creating the landing/dev permission set itself.)

In the IAM Identity Center console:

19. **IAM Identity Center → Permission sets → Create** the permission
    set the dev group lands on (e.g. `tg-Developer`, 4-hour session).
    For the **direct** model, give it an inline `bedrock:InvokeModel`
    policy (see `cfn/permission-set.yaml` for the CRIS-scoped shape).
    For the **chained** fallback, leave it as a minimal landing role
    with **no** Bedrock policy — its only job is to `sts:AssumeRole`
    into `tg-consumer`.
20. **AWS accounts → select target account → Assign users/groups** →
    pick the IDC group synced from Okta → pick that permission set.
21. **Chained model only:** make sure the assumed-role ARN for this
    permission set is included in `TrustedIamPrincipals` when you deploy
    `cfn/tg-bedrock-role.yaml`, so the SSO session can chain into
    `tg-consumer`. The client-side profile wiring (`source_profile` +
    `role_arn`) is in
    [onboarding-new-user.md](onboarding-new-user.md).

## Troubleshooting — lessons from a first-time walkthrough

| Symptom | Real cause | Fix |
|---|---|---|
| After Okta login, browser lands on Okta dashboard; terminal hangs | AWS IDC has no IdP metadata on file (Phase 3 step 11 skipped) | Upload Okta's metadata XML to AWS IDC |
| `400 Bad SAML request` at Okta | Stale browser state / logging into wrong Okta app | Incognito window, clear `~/.aws/sso/cache/*`, retry |
| Browser shows "Allow access" but after clicking it says `127.0.0.1 refused to connect` | `aws sso login` ran on a remote EC2 — local callback URL isn't reachable from your Mac | Use `--use-device-code` |
| `aws sso login` error: `Missing the following required SSO configuration values` | Forgot `--profile <name>` | Always pass `--profile` |
| Okta System Log shows `User single sign on to app SUCCESS` but CLI still errors | SAML chain worked; the error is downstream (browser/callback/CLI) | Check the CLI error directly, not Okta |
| `aws sso login` token expires after ~8 hours despite IDC set to 7 days | Okta's "Send value in response" checkbox was unchecked — IDC never received a session duration and fell back to its 8-hour default | In Okta → AWS IDC app → Sign On → Edit → check "Send value in response" under Maximum App Session Lifetime; re-login |
| CloudTrail `Authenticate` event with `userIdentity.type=IdentityCenterUser` | Success signal — IDC accepted the assertion | No action |

## Cautions

- **Don't switch identity source back** after going External IdP. AWS
  will drop local IDC users on the transition; going back is messy.
- **Don't share the SCIM bearer token.** It's equivalent to the ability
  to create users in your AWS account. Rotate via AWS IDC console if
  leaked.
- **The Okta group name is arbitrary**, but per legacy AWS convention
  people still use `aws_<account>_<role>`. Don't let this confuse you
  — IDC doesn't parse the name.
- **Two Okta AWS apps exist.** "AWS IAM Identity Center" and "AWS
  Account Federation." Don't mix them up. If you already have an
  Account Federation app for direct-SAML-to-IAM-role federation, that
  app is unrelated to IDC — leave it alone.
