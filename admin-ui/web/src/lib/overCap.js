// Shared over-cap classification for the spend surfaces (Users list,
// user-detail, Velocity/Cost). A user can be over their cap in three
// distinct ways, and one of them is invisible-by-design unless we say
// so: a "managed" user whose cap is NOT being enforced (ungoverned, or
// governed but estimate-enforcement is off) can run far over cap with
// no deny and no warn badge — the reconciler correctly never flags an
// ungoverned principal. "Managed" reads to an admin as "under control,"
// so an unenforced over-cap must be surfaced as a GAP to act on, not
// left silent. (Prior art: AWS Budgets distinguishes a tracked "Alert"
// from an enforced "Action" — never silencing the over-limit state.)
//
// Display-only: this classifies what to SHOW; it never changes
// enforcement. The reconciler's governed-gate is the source of truth
// for what's actually denied.
//
// Single source so the three surfaces can't drift. Pure +
// framework-free → unit-testable, importable by any page.

// classifyOverCap(row) → one of:
//   null            — under cap, or no cap set: show no over-cap badge.
//   'enforced'      — an active deny is in effect (status
//                     blocked/force_blocked): the existing red badge.
//   'warn'          — governed + estimate-enforcement 'warn' +
//                     projected over cap: the existing amber estimated
//                     badge (billed under cap, projection crosses it).
//   'not_enforced'  — billed OR projected is at/over cap, but nothing
//                     above applies — the cap is NOT being enforced
//                     (the user is ungoverned, or governed with
//                     enforcement off). The NEW neutral/grey badge.
//
// Inputs read off the row (all already in the API payloads):
//   cap_usd, mtd_spend_usd, projected, status, governed,
//   estimate_enforcement, projected_over_cap.
export function classifyOverCap(row) {
  if (!row) return null
  const cap = row.cap_usd ?? row.effective_quota_usd ?? null
  // No cap → nothing to be over.
  if (cap == null || cap <= 0) return null

  const status = row.status
  // Case 1 — an active deny is in effect. Governed + blocked: the
  // existing enforced (red) badge owns this; never double-signal.
  if (status === 'blocked' || status === 'force_blocked') {
    return 'enforced'
  }

  const billed = row.mtd_spend_usd ?? 0
  const projected = row.projected ?? billed
  const enforcement = row.estimate_enforcement

  // Case 2 — governed warn-mode: billed under cap but projected
  // crosses it. The existing amber estimated badge owns this.
  if (row.governed && enforcement === 'warn' && row.projected_over_cap) {
    return 'warn'
  }

  // Case 3 — over cap (billed OR projected) but none of the enforced /
  // warn signals apply, so the cap is NOT being enforced. This is the
  // gap: ungoverned (the reconciler never flags it), OR governed with
  // enforcement off (no deny yet, no warn badge). Either way the admin
  // should see it and can Govern / turn on enforcement.
  const overCap = billed >= cap || projected >= cap
  if (overCap) return 'not_enforced'

  return null
}

// The reason a 'not_enforced' over-cap isn't being enforced, for the
// tooltip. 'ungoverned' when the principal isn't governed; otherwise
// (governed but enforcement off) name that instead — so the admin
// knows which lever to pull (Govern vs turn enforcement on).
export function notEnforcedReason(row) {
  if (row && row.governed) {
    return 'estimate enforcement is off'
  }
  return 'this principal is ungoverned'
}

// The full tooltip text for the 'not_enforced' badge: the concrete
// numbers + why it isn't enforced + the action. fmtUsd is passed in so
// this stays framework/format-free (the pages already import it).
export function notEnforcedTooltip(row, fmtUsd) {
  const cap = row?.cap_usd ?? row?.effective_quota_usd ?? 0
  const billed = row?.mtd_spend_usd ?? 0
  const reason = notEnforcedReason(row)
  const action = (row && row.governed)
    ? 'Turn on estimate enforcement to enforce the cap.'
    : 'Govern the user to enforce the cap.'
  return (
    `Billed ${fmtUsd(billed)} is over the ${fmtUsd(cap)} cap, but tg `
    + `isn't enforcing it — ${reason}. ${action}`
  )
}
