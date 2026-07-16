# Roles and permissions for the pilot

Two IAM roles in the pilot AWS account: one for everyday users, one for the
admin (the operator + maybe one IT lieutenant). Everything below assumes
one single AWS account hosts the pilot.

## How a dev's role reaches Bedrock — direct (primary) vs chained

There are two governance models; **direct is primary, chained is the
secondary fallback.**

- **DIRECT (primary).** The dev's IDC permission set carries
  `bedrock:InvokeModel` itself, so the dev invokes Bedrock **as their
  SSO role** (`AWSReservedSSO_*`) with no role-chain. The deny
  reconciler attaches `tg-BedrockQuotaDeny` to that role directly. This
  is the default — simplest path, nothing to assume.
- **CHAINED (secondary).** When the permission set can't carry the
  Bedrock policy (locked-down IDC, or it would be wiped on
  re-provision), the Bedrock permissions live on the `tg-consumer` IAM
  role and the dev's SSO session assumes it via STS role chaining. The
  rest of this section describes that chained role.

Permission sets earn their complexity at scale (multiple AWS accounts,
100+ users, central IT team). For a small single-account pilot, plain
IAM roles deliver the same enforcement model with zero dependency on
central IDC admins. The operator deploys the roles in their own account
via CFN; central IT is not in the loop after the existing Okta→IDC
federation is reused for authentication. Lifting a role definition into
a permission set later is mechanical — same trust shape, same inline
policy.

## Role 1 — `tg-consumer` (the secondary/chained user role)

**Who assumes it:** the pilot devs, **only under the chained model.**
Under the direct model they invoke Bedrock as their SSO role and never
touch this role.

**How they reach it (chained model):** they `aws sso login` against the
corporate IDC (existing Okta-federated path), land in the pilot account
in their default IDC role, then assume `tg-consumer` via STS role
chaining. AWS CLI handles the chain automatically when the dev's
`~/.aws/config` is set up with `source_profile` + `role_arn`.

### Trust policy

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "AWS": "arn:aws:iam::<pilot>:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_<your-default>_<hash>"
    },
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {
        "sts:RoleSessionName": "${aws:username}"
      }
    }
  }]
}
```

The session-name condition forces the assumed-role session to inherit the
caller's username. Without it, a malicious dev could spoof someone else's
identity in CloudTrail and `aws:userid`. With it, `aws:userid` reliably
reflects the logged-in corporate email — which is what the quota Deny
reconciler keys off.

### Inline policy (what the role can do)

This policy lets the role call the allowed Claude models across
regions — you don't need to edit it. The ARNs below are shown for
reference.

```
Allow:
  bedrock:InvokeModel
  bedrock:InvokeModelWithResponseStream
On resources:
  arn:aws:bedrock:us-east-1:<pilot>:inference-profile/us.anthropic.claude-sonnet-4-6
  arn:aws:bedrock:us-east-1:<pilot>:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0
  arn:aws:bedrock:us-east-1:<pilot>:inference-profile/us.anthropic.claude-opus-4-7
  arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-*
  arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-*
  arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-*

Allow:
  bedrock:ListFoundationModels
  bedrock:GetFoundationModel
  bedrock:ListInferenceProfiles
  bedrock:GetInferenceProfile
On: *
```

### Attached managed policy

`tg-BedrockQuotaDeny` — this is **mutated at runtime** by the
worker's deny reconciler job. When a dev's billed (CUR) spend exceeds
their dollar cap, this policy gains a Deny statement scoped to their
`aws:userid`. When their cap is raised or the month resets, the Deny
is removed.

Devs never see this policy directly; they just experience 403 responses
from Bedrock when over budget.

### What devs CANNOT do

- Edit any quota / pricing / policy data
- View other users' usage
- Read the Cost and Usage Report / billing data
- Modify any IAM policy
- Touch CFN, the api/worker containers, the database, etc.

The role is purely "use Bedrock, subject to your cap."

### Defined in

`cfn/tg-bedrock-role.yaml`.

---

## Role 2 — `tg-BedrockAdmin` — **REMOVED**

`tg-BedrockAdmin` no longer exists. The desktop `tg-admin` client
that assumed it (chaining in from the operator's IDC default role)
was retired, so the role had no remaining caller and was deleted.

**Admins now reach the `org_admin` UI via the web login** — the
Cognito invitation email, or OIDC/Okta if wired. Caps, unblocks,
and pricing are still edited through the api (`/api/*`),
authenticated as an `org_admin` / `team_admin` row; the api runs in
the customer's account and writes to Postgres. The log-query and
Athena/CUR reads that the admin role once granted are now performed
by the api/worker task role inside the container, not by a
human-assumed STS role.

The only assumed role left in the pilot is `tg-consumer` (Role 1
above).

---

## Side-by-side

| | `tg-consumer` |
|---|---|
| Invoke Bedrock | ✅ Yes (subject to cap) |
| Subject to dollar cap | ✅ Yes (tg-BedrockQuotaDeny) |
| Audit | CloudTrail on every Bedrock call |

(`tg-consumer` is the only assumed role left — see the removal note
above. Caps/unblocks/pricing edits and log/Athena reads now happen
inside the api/worker container under its task role, reached via the
web-login UI rather than a human-assumed admin role.)

---

## Onboarding workflows

### Adding a new dev

1. The operator tells the Okta admin: "add `alice@example.com` to Okta
   group `BedrockPilot`" (or whatever group has been earmarked for this).
2. Okta SCIM syncs Alice into the corporate IDC (~5 min).
3. (Optional) The operator opens admin UI → Users → "Pre-register" → enters
   `alice@example.com` with a custom cap, e.g. $500/mo. This sets her cap
   before she ever invokes Bedrock. Without this, she gets the default cap.
4. Alice on her Mac:
   - One-time: configure `~/.aws/config` per `docs/developer-setup.md`.
   - Daily: `aws sso login` → `claude` just works.

### Removing a dev

1. The operator tells the Okta admin: "remove alice@example.com from
   `BedrockPilot`".
2. SCIM syncs removal (~5 min).
3. Her next `aws sso login` succeeds (still has Okta access for other
   things) but she's no longer in the IDC group → can't reach the pilot
   AWS account's role → can't assume `tg-consumer` → Bedrock
   inaccessible.
4. (Optional) The operator archives her usage from the admin UI for
   chargeback.

### Block a dev immediately mid-month

1. Open admin UI → Users → Alice → "Set cap" → $0.01.
2. Within 5 min, the worker's `deny_reconciler` writes a Deny statement
   scoped to her `aws:userid` into `tg-BedrockQuotaDeny`.
3. Her next Bedrock call returns `AccessDeniedException` citing
   `tg-BedrockQuotaDeny`.

### Let an over-cap dev through

There is **no time-boxed "temporary unblock"** — the
`unblock_expires_at` reprieve was removed. To let an over-cap
dev keep working, **raise their cap above their current spend**:

1. Open admin UI → Users → Alice → "Edit cap" → a number above her
   current spend.
2. The deny reconciler sees `spend < cap` on its next pass (within a
   few minutes) and drops her Deny; her next Bedrock call works.
3. Lower the cap again later if you want.

(The `Unblock` action clears a manual **Force block** only — it does
not grant a time-boxed reprieve from a cap-based block. See
`docs/quota-admin.md` Workflow 2.)

### Adjust pricing (when AWS rates change)

1. Open admin UI → Settings → Pricing → edit per-1M-token rates.
2. The pricing table is an admin-editable reference for the Cost
   Reports view; actual capped spend is the billed cost from CUR 2.0,
   not a pricing-table estimate. Past usage isn't recalculated
   retroactively.

---

## What the operator tells central IT (one ticket, total)

Once, before the pilot starts:

> "Add the Okta group `BedrockPilot` (or whatever name your standard is) to
> the existing AWS IAM Identity Center app. Assign that group access to
> AWS account `<pilot-account-id>` in the role they already have for that
> account (or create a minimal one — read-only or null is fine; we just
> need them to land in the account so they can role-chain from there)."

That's it. After this single ticket, the operator manages everything else
(adding/removing devs from the Okta group, setting caps, unblocking) via
the admin UI in their own account. No more central-IT involvement.

---

## CloudTrail audit summary

Every action is attributable:

| Event | Who | Captured how |
|---|---|---|
| Dev invokes Bedrock | aws:userid = `*:alice@example.com` | CUR `line_item_iam_principal` (billed spend) + CloudTrail |
| Dev hit cap and got 403 | Same | CloudTrail shows AccessDeniedException with `tg-BedrockQuotaDeny` |
| Reconciler wrote a Deny statement | The worker task role | CloudTrail `iam:CreatePolicyVersion` |
| Operator changed a cap | api request from the operator's web-login session (session cookie → admin email) | api access log (records the admin's email); CloudTrail still covers the underlying AWS calls |
| Operator unblocked Alice | Same | api access log |
| Operator edited pricing | Same | api access log |

For chargeback / audit purposes, no separate audit table is needed.
CloudTrail is the source of truth.
