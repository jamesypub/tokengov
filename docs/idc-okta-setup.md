# AWS IAM Identity Center + Okta setup guide

One-time setup: wire a fresh AWS account to an Okta tenant so users
can `aws sso login` with their Okta credentials and then invoke
Bedrock from `claude`.

Validated against the pilot account (`<PILOT-ACCOUNT>`) with an
Okta tenant. Day-to-day commands: see
[`login-commands.md`](login-commands.md).

> **Don't have Okta, or just piloting?** You don't need this guide.
> TG supports admin login via a self-contained Cognito User Pool
> (≈5 min, no external IdP). See
> [`admin-setup.md` § Auth provider choices](admin-setup.md#auth-provider-choices).
> Okta + IDC is the enterprise-SSO path; Cognito is the
> lowest-friction one.

---

## Pre-reqs

- **AWS account** where you are an admin. Standalone is fine — the wizard
  turns it into the management account of a fresh Organization.
- **Okta tenant** where you are an admin (Integrator Free Plan works).
- `aws` CLI v2 on every client that will run `aws sso login`.

## Parameters you'll pick

| Parameter | Example (pilot) | Notes |
|---|---|---|
| AWS account ID | `<PILOT-ACCOUNT>` | The 12-digit target account |
| AWS region | `us-east-1` | |
| Permission set name | `tg-Developer` | The IDC permission set devs land on (Phase 6) |
| IDC group name in Okta | `aws_<acct>_<role>` legacy convention, any name works | Group name is an arbitrary label; IDC doesn't parse it. |
| Okta tenant | `<your-okta-tenant>.okta.com` | |

---

## Phase 1 — Enable IDC in AWS

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

## Phase 2 — Add the Okta app

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

## Phase 3 — Wire SAML trust (bidirectional)

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

## Phase 4 — Turn on SCIM provisioning

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

## Phase 5 — Assign users and push groups to IDC

In Okta:

16. **Directory → Groups → Add Group** — create a group (e.g.
    `aws_<acct>_<role>`). Add members (your test Okta users).
17. Back to the AWS IDC app → **Assignments tab → Assign → Assign to
    Groups** → pick the group.
18. **Push Groups tab → Push Groups → by name** → pick the same group →
    Save.

Within ~30s the group + members sync to IDC. Verify on EC2 / admin
machine (using a profile that can read account2's identity store):

```bash
aws identitystore list-users --identity-store-id d-XXXXXXXXXX --region us-east-1 \
  --query 'Users[].UserName' --output table
aws identitystore list-groups --identity-store-id d-XXXXXXXXXX --region us-east-1 \
  --query 'Groups[].DisplayName' --output table
```

## Phase 6 — Grant the IDC group a permission set that can reach Bedrock

There are two ways a dev's IDC role reaches Bedrock under governance.
**Direct is the primary model; chained is the secondary fallback.**

**Primary — DIRECT.** The dev's IDC permission set carries
`bedrock:InvokeModel` itself, and the dev invokes Bedrock **as their
SSO role** (`AWSReservedSSO_*`). No role-chain, no `tg-consumer` in the
path. The deny reconciler governs that role directly (it attaches
`tg-BedrockQuotaDeny` to whatever role a governed principal uses).
This is the simplest setup and the one to reach for first.

**Secondary — CHAINED (locked-down-IDC fallback).** When the permission
set **can't** carry the Bedrock policy (org policy forbids it, or it
would be wiped on re-provision), keep the Bedrock permissions on the
**`tg-consumer` IAM role** (deployed by `cfn/tg-bedrock-role.yaml`) and
have the dev's IDC SSO session **assume** it via STS role chaining. The
permission set then only needs to be a **landing role** that is (a)
synced to the pilot dev group and (b) listed in the `tg-consumer` trust
policy (`TrustedIamPrincipals`). See
[roles-and-permissions.md](roles-and-permissions.md) — "Why IAM roles,
not IDC permission sets" — for the chained-role shape. (The earlier
`idc-permission-set.sh` helper that minted a `BedrockDeveloper`
permission set was retired.)

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
    [onboarding-new-user.md](onboarding-new-user.md). The direct model
    needs none of this — the profile invokes Bedrock as the SSO role.

## Govern an IDC permission-set role directly (the primary model)

Under the direct model, users call Bedrock **from their IDC
permission-set role** (an `AWSReservedSSO_*` role, never assuming
`tg-consumer`), so the deny has to land on that role. A direct
`iam:AttachRolePolicy` there is wiped the next time IDC re-provisions
the permission set.

The durable path is a **customer-managed-policy reference** on a
permission set: IDC owns the attachment (so it survives re-provision),
while the tg reconciler keeps editing the policy *content* in the
member account. From the **IDC management (or delegated-admin)
account**, under a profile with `sso:*` (NOT the member-account
installer profile), create a dedicated permission set (e.g.
`tg-QuotaDenyPermissionSet`), reference `tg-BedrockQuotaDeny` on it by
name, and provision it to the member account:

```sh
# 1. Create the permission set
aws sso-admin create-permission-set \
  --instance-arn <arn:aws:sso:::instance/ssoins-..> \
  --name tg-QuotaDenyPermissionSet

# 2. Reference tg-BedrockQuotaDeny (customer-managed) on it by name
aws sso-admin attach-customer-managed-policy-reference-to-permission-set \
  --instance-arn <arn:aws:sso:::instance/ssoins-..> \
  --permission-set-arn <permission-set-arn from step 1> \
  --customer-managed-policy-reference Name=tg-BedrockQuotaDeny

# 3. Provision it to the member account (and assign to the governed
#    IDC group via create-account-assignment)
aws sso-admin provision-permission-set \
  --instance-arn <arn:aws:sso:::instance/ssoins-..> \
  --permission-set-arn <permission-set-arn> \
  --target-id <member account id> --target-type AWS_ACCOUNT
```

Notes:
- `tg-BedrockQuotaDeny` must exist in the member account (the tg deny
  reconciler creates it); the reference resolves to nothing until then.
- This is **org-instance only** — an IDC *account instance* has no
  permission sets.

**On the tg admin side:** once the permission-set reference is wired,
an org/team admin governs the IDC user from the admin UI like any
other — **Users → the user → Govern**. tg sets `governed=true` and the
reconciler emits the per-person deny; it does NOT attach to the
`AWSReservedSSO_*` role directly. Until the reference above (or the
user assuming `tg-consumer`) is in place, the UI shows the govern
action as **advisory** — it states this precondition rather than
implying hard enforcement, because tg can't see the IDC management
account. See [quota-admin.md](quota-admin.md) Workflow 0.

## Phase 7 — Client-side configuration

On the client machine (your Mac, or an EC2):

21. Append the `~/.aws/config` profile. The canonical block lives in
    [onboarding-new-user.md](onboarding-new-user.md) §1 — the
    `tg-developer-<PILOT-ACCOUNT>` profile (`sso_role_name = tg-Developer`,
    `sso_account_id = <PILOT-ACCOUNT>`). Use that single source of truth
    rather than duplicating it here.

22. Log in:
    ```bash
    aws sso login --profile tg-developer-<PILOT-ACCOUNT>
    ```

    - **Caution (EC2):** on a headless box the default flow tries to open
      a browser and opens a local callback on `127.0.0.1:<random>`. Your
      remote browser can't reach that. Use **device-code flow** instead:
      ```bash
      aws sso login --profile tg-developer-<PILOT-ACCOUNT> --use-device-code
      ```
      Paste the short URL + code into any browser on your Mac, approve.

23. Verify:
    ```bash
    aws sts get-caller-identity --profile tg-developer-<PILOT-ACCOUNT>
    ```
    ARN should end with `assumed-role/AWSReservedSSO_tg-Developer_<hash>/<okta-username>`.

24. Invoke Bedrock via `claude` (set `ANTHROPIC_MODEL` to any model
    id your org allows — e.g. a US CRIS inference-profile id):
    ```bash
    AWS_PROFILE=tg-developer-<PILOT-ACCOUNT> AWS_REGION=us-east-1 CLAUDE_CODE_USE_BEDROCK=1 \
      ANTHROPIC_MODEL=<model-id> \
      claude -p "say hi"
    ```

---

## Troubleshooting — lessons from the 2026-05-13 walkthrough

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
