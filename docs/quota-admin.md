# Quota admin workflow

How to set, raise, and unblock per-user dollar quotas. All workflows
reduce to **api calls** (or admin-UI clicks); the worker's reconciler
job converges the IAM Deny policy within 5 minutes.

## Mental model

```
  cur_user_spend     ◄─ cur_spend_sync (hourly) ◄─ CUR 2.0 via Athena
   (billed spend $)                                  (AWS bill, ≤24h lag)

  quota_policies     ◄─ admin edits (UI / api)
   (desired caps $)

           │  both tables read by
           ▼
   deny_reconciler (5min) ───►  tg-BedrockQuotaDeny managed policy
                                       │
                                       ▼
                          attached to tg-consumer IAM role
                                       │
                                       ▼
                            ✓ or 403 on next Bedrock call
```

**The api is source of truth.** The reconciler always makes IAM match
Postgres. Don't hand-edit the managed policy — your changes get
overwritten on the next tick.

## Governance scope — opt-in

tg governs only principals an admin has **enrolled** (the **Govern**
action). An un-enrolled (**Ungoverned**) principal is **ignored** by
every tg job — no cap evaluation, no deny, no status change — **even
if it spends past a cap**. Spend never auto-enrolls a principal; an
admin must opt it in.

So a principal has two independent dimensions:

- **Governed vs Ungoverned** — is tg enforcing on it at all? Set by
  **Govern** / **Ungovern** (the enroll / un-enroll actions).
  Ungoverned = out of scope; its spend is uncapped until you enroll
  it.
- **Within governance, the cap state** — only meaningful once
  Governed: under cap (allowed) vs over cap (denied), plus a manual
  **Force block** override.

Discovering a principal (it appears under **Users** once it has
invoked Bedrock) does **not** enforce anything on it — it surfaces it
so an admin can decide whether to Govern it.

## Tables (Postgres)

| Table | What it holds |
|---|---|
| `quota_policies` | Default + per-user dollar caps (monthly + daily) |
| `cur_user_spend` | Per-(user, model, day) billed spend + tokens, synced from CUR by `cur_spend_sync` |
| `model_pricing` | Per-model $/1M-token rates (admin-editable reference; CUR carries the billed cost) |
| `users` | The user roster: status, team, cap override |
| `admin_roles` | `org_admin` / `team_admin` grants |

Schema lives in `container/db/models.py`. To inspect from a host with
the local install up:

```bash
docker compose exec postgres \
  psql -U tg -d tg -c '\dt'
```

---

## Workflow 0 — enroll / un-enroll a principal (Govern / Ungovern)

The prerequisite for every other workflow: a principal must be
**Governed** (enrolled) before any cap, default policy, or block
applies to it. Govern / Ungovern are the enroll / un-enroll actions.

UI: **Users → alice@example.com → Govern** (enroll) /
**Ungovern** (un-enroll).

- **Govern** — a one-time `AttachRolePolicy` of `tg-BedrockQuotaDeny`
  to the principal's role and sets `governed=true`. From the next
  governance job run (within a few minutes) tg evaluates Alice's cap
  and applies the org blocked-models deny.
- **Ungovern** — clears `governed`, and detaches the policy from the
  role once no other Governed principal still uses it. Alice drops
  out of governance scope: her spend is no longer capped.

API (the HTTP route names are unchanged — `/manage` enrolls,
`/unmanage` un-enrolls):
```bash
curl -X POST http://<api>/api/users/alice@example.com/manage    # Govern
curl -X POST http://<api>/api/users/alice@example.com/unmanage  # Ungovern
```

**IDC permission-set principals (`AWSReservedSSO_*`) — now
governable.** Govern works the same way in the UI: it sets
`governed=true` and the reconciler emits the per-person deny. tg does
**not** attach the policy directly to the `AWSReservedSSO_*` role (a
direct attach is wiped on the next IDC re-provision) — instead the
deny takes effect once it reaches a role the user actually uses,
either:

- they assume **`tg-consumer`** (tg attaches the deny there itself), or
- your **IDC admin referenced `tg-BedrockQuotaDeny` on the user's
  permission set** — the durable path, since IDC owns that attachment.
  See [idc-okta-setup.md](idc-okta-setup.md) "Govern a direct IDC
  permission-set role" for the `aws sso-admin` steps that create the
  `tg-QuotaDenyPermissionSet` reference.

So governing an IDC user is **advisory until one of those is in
place** — tg can't see the IDC management account, so the UI states
the precondition rather than blocking. Ungovern just clears
`governed` (it never touches the IDC role).

## Workflow 1 — raise a user's cap permanently

Use case: Alice burned through her $100 monthly cap; admin wants $300.

UI: **Users → alice@example.com → Edit cap → 300**

API:
```bash
curl -X PUT http://<api>/api/users/alice@example.com/cap \
  -H 'Content-Type: application/json' \
  -d '{"monthly_cap_usd": 300}'
```

Within 5 min, reconciler sees `spend < cap` → drops Alice's Deny
statement. Alice's next call works.

## Workflow 2 — let an over-cap user through

Use case: Alice is over her cap and blocked, but needs Bedrock for a
deadline.

There is **no time-boxed "temporary unblock"** — the
`unblock_expires_at` reprieve was removed. The shipped model is
simpler: **raise the cap** to a number above her current spend
(Workflow 1). The reconciler drops her Deny within a few minutes on
its next run; lower the cap again later if you want.

```bash
# raise Alice's monthly cap above her current spend
curl -X PUT http://<api>/api/users/alice@example.com/cap \
  -H 'Content-Type: application/json' \
  -d '{"monthly_cap_usd": 500}'
```

(`POST /users/{email}/unblock` exists, but it only clears a **manual
Force block** — see Workflow 4 — it does not grant a time-boxed
reprieve from a cap-based block.)

## Workflow 3 — change the default cap

UI: **Settings → Default policy → Monthly cap**

API:
```bash
curl -X PUT http://<api>/api/policies/default \
  -H 'Content-Type: application/json' \
  -d '{"monthly_cap_usd": 200}'
```

Applies to every user without an override.

## Workflow 4 — force block / unblock a user

Force block adds an explicit Deny **regardless of spend** (an admin
override on top of the cap); Unblock clears it and returns the user to
the normal cap-based state (this replaced the older
`/disable` + `/enable` actions). The principal must be **Governed** first
(tg can't block a principal it isn't enforcing on).

UI: **Users → alice@example.com → Force block** / **Unblock**.

```bash
# Force block — explicit Deny regardless of spend
curl -X POST http://<api>/api/users/alice@example.com/force-block

# Unblock — clears the manual force-block; status returns to
# "active" (the cap still applies — over-cap users stay denied
# until spend drops or you raise the cap)
curl -X POST http://<api>/api/users/alice@example.com/unblock
```

## Workflow 5 — inspect current usage

UI: **Activity** page (per-user, per-model rows for the current month)

API:
```bash
curl http://<api>/api/usage?email=alice@example.com
curl http://<api>/api/activity?month=2026-05
```

## Workflow 6 — inspect the current Deny policy

```bash
POLICY_ARN=arn:aws:iam::<account>:policy/tg-BedrockQuotaDeny
VERSION=$(aws iam get-policy --policy-arn $POLICY_ARN \
  --query 'Policy.DefaultVersionId' --output text)
aws iam get-policy-version \
  --policy-arn $POLICY_ARN --version-id $VERSION \
  --query 'PolicyVersion.Document'
```

A single `NoOpPlaceholder` statement means nobody is blocked.

---

## Alerts

The worker's `quota_monitor` job runs every 15 min and emits 80/90/100%
alerts via SES (using `TG_ALERT_EMAIL`). Each `(user, threshold)` fires
at most once per period to avoid spam.

To send a test alert email:
```bash
curl -X POST http://<api>/api/settings/alerts/test
```

## Reset schedules

- **Daily:** worker runs `quota_reset` at 00:00 UTC every day. Zeros
  `daily_spend_usd` on every row.
- **Monthly:** same job, day 1 of month. Zeros monthly totals.
  Next reconciler tick drops all monthly-triggered Denies.

---

## Known limitations

- **~5 min lag** in both directions (block and unblock). Worst-case
  overshoot on block is one full-output request.
- **Managed-policy size cap: 6,144 chars.** At ~3 models × N users this
  comfortably handles 50 users; shard before ~100 fully-capped users.
- **Explicit Deny survives in-flight streaming requests** — a stream
  open at time of Deny completes; next request is blocked.
- **`aws:userid` matching depends on IDC session-name format.** Profiles
  must set `role_session_name = <user-email>` for per-user attribution
  to work.
- **API-level auth.** Anyone authenticated as `org_admin` can raise any
  user's cap. Tier admin grants accordingly (use `team_admin` for
  scoped grants).

## Troubleshooting

| Symptom | Check |
|---|---|
| Set cap low but user still works | CUR hasn't billed the spend yet (≤24h lag), the hourly `cur_spend_sync` hasn't run, or the reconciler hasn't run (≤5 min). |
| Reconciler logs say "oversize" | Deny policy exceeded 6144 chars. Need to shard — increase `DenyPolicyShardCount` CFN param and redeploy. |
| User still blocked after raising cap | Check Activity page — actual `spend_usd` might be higher than you thought. |
| Alert email never arrives | Verify SES is out of sandbox in this account, and `TG_ALERT_EMAIL` is verified. |
| Policy version limit hit (5) | Reconciler handles this — deletes oldest non-default before creating. If you see the error in worker logs, likely an IAM permission issue on `iam:DeletePolicyVersion`. |
