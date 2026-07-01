import React, { useEffect, useMemo, useState } from 'react'
import { api, fmtUsd, getTeams, getSummary } from '../api'
import StatusBadge from '../components/StatusBadge'
import { classifyOverCap, notEnforcedTooltip } from '../lib/overCap'
import { useTeamScope } from '../TeamScope'
import { Card } from '../ui/Card'
import { Button } from '../ui/Button'
import { Input } from '../ui/Input'
import { SkeletonBlock } from '../ui/Skeleton'
import {
  flexRender, getCoreRowModel, getSortedRowModel, useReactTable,
} from '@tanstack/react-table'
import {
  AWAITING_PRINCIPAL_CHIP, isAwaitingPrincipal,
  PREREGISTER_NOTICE_TITLE, PREREGISTER_NOTICE_BODY,
} from '../lib/governGate'

// #628 (deny-only gov D): the Users list is the OBSERVE-ONLY
// discover screen. It answers "who · how governed · how much ·
// status" and carries NO governance action — every mutating
// action (Govern/Ungovern/Set-cap/Disable, observed-models,
// quota-keying detail) lives on the detail page (#E). The whole
// row is a link to detail; a right-aligned chevron is the only
// affordance and it just navigates. Attaching the deny is a real
// IAM mutation — a per-row button on a monitoring screen makes a
// consequential, context-dependent action feel trivial (#618).

// sessionName — the last ARN segment (the role-session-name).
// arn:aws:sts::<acct>:assumed-role/<role>/<session-name>  → <session-name>
// arn:aws:iam::<acct>:user/<name>                          → <name>
// Falls back to email / identity_key when no ARN is attributed yet.
export function sessionName(u) {
  const arn = u.principal_arn || ''
  if (arn) {
    if (u.principal_type === 'root' || /:root$/.test(arn)) return 'root'
    const seg = arn.split('/').filter(Boolean).pop()
    if (seg) return seg
  }
  return u.email || u.identity_key || ''
}

// isEmailShaped — pragmatic RFC-lite predicate. A *view-time* test
// (never a stored verdict). #839: used by callerBucket on the
// identity the row carries (email / identity_key), not on the
// role-name tail of the ARN.
export function isEmailShaped(s) {
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(s || '')
}

// callerBucket — the People/Machines slice (#608). #839: key on the
// identity the row ALREADY carries — `email` + `principal_type` —
// NOT on sessionName(), which returns the ROLE-NAME tail of the ARN
// (e.g. `tg-consumer`) because ingestion stores principal_arn = the
// role ARN so the deny reconciler can attach to it (#345). The old
// `isEmailShaped(sessionName(u))` therefore mislabeled every
// email-session human (tg-org-admin+dev@…, every team/admin row) as a
// machine and collapsed many people onto one role-name string.
//
// Rule: a human principal_type (assumed_role / iam_user / federated)
// WITH an email-shaped email|identity_key → People. service /
// service_linked / root, or any row with no email-shaped identity →
// Machine. sessionName() stays the DISPLAY label; only the bucketing
// decision moved off it.
export function callerBucket(u) {
  if (!u) return 'machines'
  const t = u.principal_type
  // Explicit machine principal types are always Machines, even if a
  // session string happened to look email-shaped.
  if (t === 'service' || t === 'service_linked' || t === 'root') {
    return 'machines'
  }
  // Otherwise the row is People iff it carries an email-shaped
  // identity (email or identity_key) — a human assumed-role /
  // iam_user / federated session, OR a pre-registered human not yet
  // observed. No email-shaped identity → a genuine machine session
  // whose role-session-name wasn't an email → Machine.
  if (isEmailShaped(u.email) || isEmailShaped(u.identity_key)) {
    return 'people'
  }
  return 'machines'
}

// governanceState — #628/#856/#1011: the deny-only governance state a
// row renders in the Governance column (icon-only + legend).
//   'governed'   ✓  tg governs this principal (governed=true).
//   'ungoverned' ○  discovered but not yet governed.
// #1011: IDC is NO LONGER a terminal state — an AWSReservedSSO_*
// permission-set user IS governable now (the deny lands via tg-consumer
// or the #1010 permission-set reference, not a direct attach). So an
// IDC user is governed/ungoverned like any other; `isIdc` is a separate
// QUALIFIER the cell badges alongside the state ("Governed · IDC"), and
// the panel shows an advisory precondition for the permission-set path.
export function isIdc(u) {
  return (u.role_type || 'iam') === 'idc'
}
export function governanceState(u) {
  return u.governed ? 'governed' : 'ungoverned'
}

// Governance icon legend — shared by the column cells and the help
// expander so the meaning is both discoverable and AT-available
// (never icon/color alone). aria-label carries the words.
export const GOVERNANCE_LEGEND = {
  governed: {
    icon: '✓',
    label: 'Governed',
    desc: 'tg-BedrockQuotaDeny attached — tg governs this principal.',
    cls: 'text-teal-700',
  },
  ungoverned: {
    icon: '○',
    label: 'Ungoverned',
    desc: 'Discovered but not yet governed by tg.',
    cls: 'text-[var(--ink-4)]',
  },
}

// #1011: IDC is a QUALIFIER on top of governed/ungoverned, not its own
// terminal state. The ◆ badges an IAM Identity Center permission-set
// user; enforcement reaches it via the permission-set reference (the
// #1010 tg-QuotaDenyPermissionSet) or tg-consumer, never a direct
// attach. The legend explains the change from "not governable" to
// "enforced via the permission set."
export const IDC_QUALIFIER = {
  icon: '◆',
  label: 'IDC',
  desc: 'IAM Identity Center permission-set user — governable; the '
    + 'deny is enforced via the permission-set policy reference (or '
    + 'tg-consumer), not a direct role attach.',
  cls: 'text-amber-600',
}

// #844: per-column header alignment classes from a column def's
// `meta.align` ('right' | undefined). Returns the {th, span} class
// fragments so a right-aligned numeric header (Spend / Cap) lines up
// flush-right over its right-aligned cells while keeping the sort
// arrow adjacent. Pure + exported so the alignment contract is
// unit-testable without mounting the whole table.
export function headerAlignClasses(meta) {
  const right = meta?.align === 'right'
  return {
    th: right ? 'text-right' : 'text-left',
    span: right ? 'w-full justify-end' : '',
  }
}

// matchesCallerFilter — the 5-way list filter (#628):
// All / People / Machines / Ungoverned / IDC. People/Machines slice
// the session-name; Ungoverned/IDC slice the governance state.
export function matchesCallerFilter(u, filter) {
  switch (filter) {
    case 'all':        return true
    case 'people':     return callerBucket(u) === 'people'
    case 'machines':   return callerBucket(u) === 'machines'
    case 'ungoverned': return governanceState(u) === 'ungoverned'
    case 'idc':        return isIdc(u)
    default:           return true
  }
}

// GovernanceCell — icon-only ✓/○/◆ with an accessible name. #824:
// dropped the `cursor-help` (?) and the native `title` from the
// data cell — a help-cursor over a whole cell competed with the
// row-link affordance and the native tooltip was slow/unstyled/
// touch-invisible. The legend now lives behind the header ⓘ
// affordance (GovernanceLegend). The words still ride in
// aria-label so screen-reader + keyboard users hear
// Governed/Ungoverned/IDC (never color/icon alone). #628 a11y.
export function GovernanceCell({ user }) {
  const g = GOVERNANCE_LEGEND[governanceState(user)]
  // #946: an ungoverned principal that's still awaiting an AWS
  // principal (no role ARN) gets a read-only "awaiting AWS principal"
  // chip whose tooltip/aria carries the WHY — so the admin learns the
  // govern blocker from the list, without drilling into detail. The
  // row stays observe-only (#628); the fix (Add IAM role ARN) lives
  // on UserDetail. The reason copy is shared via lib/governGate.
  const awaiting = governanceState(user) === 'ungoverned'
    && isAwaitingPrincipal(user)
  // #1011: IDC is a qualifier alongside the governed/ungoverned icon,
  // so a governed IDC user reads "✓ ◆ IDC" — distinguishable at a
  // glance from an IAM-role governed user. The words ride in
  // aria-label (never icon/color alone, #628/#824 a11y).
  const idc = isIdc(user)
  return (
    <span className="inline-flex items-center gap-1">
      <span
        className={`text-base ${g.cls}`}
        aria-label={g.label}
        role="img"
      >
        {g.icon}
      </span>
      {idc && (
        <span
          className={`text-[11px] ${IDC_QUALIFIER.cls} whitespace-nowrap`}
          title={IDC_QUALIFIER.desc}
          aria-label={`${IDC_QUALIFIER.label} — ${IDC_QUALIFIER.desc}`}
        >
          {IDC_QUALIFIER.icon} {IDC_QUALIFIER.label}
        </span>
      )}
      {awaiting && (
        <span
          className="text-[11px] text-[var(--ink-4)] whitespace-nowrap"
          title={AWAITING_PRINCIPAL_CHIP.title}
          aria-label={AWAITING_PRINCIPAL_CHIP.title}
        >
          {AWAITING_PRINCIPAL_CHIP.label}
        </span>
      )}
    </span>
  )
}

// GovernanceLegend — #824: the deliberate, accessible header
// affordance that replaces the bare help-cursor + native `title`.
// A small ⓘ info button (the ONLY element carrying cursor-help)
// reveals the ✓/○/◆ legend via a styled popover on BOTH hover and
// keyboard focus (focus-within), not the native title attribute.
// role="tooltip" + aria-describedby give AT users the same content.
export function GovernanceLegend() {
  return (
    <span className="group relative inline-flex items-center gap-1">
      <span>Governance</span>
      <button
        type="button"
        aria-label="Governance column legend"
        aria-describedby="governance-legend-tip"
        // The header <th> owns the column-sort onClick; the legend
        // button reveals on hover/focus, so swallow its click so a
        // tap on ⓘ doesn't also toggle the sort. (#824 — no sort
        // regression.)
        onClick={e => e.stopPropagation()}
        className="cursor-help inline-flex h-4 w-4 items-center justify-center rounded-full border border-[var(--ink-4)] text-[10px] leading-none text-[var(--ink-4)] hover:text-[var(--ink-2)] hover:border-[var(--ink-2)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
      >
        i
      </button>
      <span
        id="governance-legend-tip"
        role="tooltip"
        className="pointer-events-none absolute left-0 top-full z-20 mt-1 hidden w-64 rounded-lg border border-[var(--border-2)] bg-[var(--surface)] p-3 text-left text-[12px] font-normal normal-case tracking-normal text-[var(--ink-2)] shadow-lg group-hover:block group-focus-within:block"
      >
        <span className="mb-1 block font-medium text-[var(--ink-1)]">
          How tg governs this principal
        </span>
        <span className="block space-y-1">
          {Object.entries(GOVERNANCE_LEGEND).map(([key, g]) => (
            <span key={key} className="flex items-start gap-2">
              <span className={`${g.cls} w-4 text-center`} aria-hidden="true">{g.icon}</span>
              <span><strong>{g.label}</strong> — {g.desc}</span>
            </span>
          ))}
          {/* #1011: IDC qualifier row — a badge that rides alongside
              governed/ungoverned, no longer a terminal state. */}
          <span className="flex items-start gap-2">
            <span className={`${IDC_QUALIFIER.cls} w-4 text-center`} aria-hidden="true">{IDC_QUALIFIER.icon}</span>
            <span><strong>{IDC_QUALIFIER.label}</strong> — {IDC_QUALIFIER.desc}</span>
          </span>
        </span>
      </span>
    </span>
  )
}

// UserCell — #628: display_name (when set) as the primary label,
// with email / session-name beneath. Machine principals show the
// role-session-name (monospace) since they carry no email.
function UserCell({ user }) {
  const session = sessionName(user)
  const isRoot = session === 'root'
  const secondary = user.is_service ? session : (user.email || session)
  return (
    <div className="min-w-0">
      {user.display_name ? (
        <>
          <div className="font-medium truncate">{user.display_name}</div>
          <div className="text-[11px] text-[var(--ink-4)] font-mono truncate" title={user.principal_arn || secondary}>
            {isRoot && <span className="mr-1" title="root should be rare — investigate">⚠</span>}
            {secondary || '—'}
          </div>
        </>
      ) : (
        <div className={user.is_service ? 'font-mono text-[12px] truncate' : 'font-medium truncate'} title={user.principal_arn || secondary}>
          {isRoot && <span className="mr-1" title="root should be rare — investigate">⚠</span>}
          {secondary || <span className="text-[var(--ink-4)]">—</span>}
          {user.is_service && (
            <span className="ml-2 text-[10px] uppercase tracking-wider text-[var(--ink-4)]">service</span>
          )}
        </div>
      )}
    </div>
  )
}

// SpendCell — #628: $mtd / $cap with a thin usage bar. No
// cap-wording, no action — observe only.
function SpendCell({ user }) {
  const spend = user.mtd_spend_usd ?? 0
  const cap = user.cap_usd ?? user.effective_quota_usd ?? null
  const pct = user.pct_used
  // Billed-vs-estimated split: billed (= mtd_spend_usd) stays the
  // authoritative primary figure; the estimate fills the CUR lag window
  // and shows as a secondary "+$Y (unbilled ~Nh)" badge — never blended
  // into the billed number. Hidden when there's no unbilled gap.
  const est = user.estimated ?? 0
  const unbilled = user.unbilled_hours ?? 0
  const lowSample = user.estimate_low_sample
  // Over-cap classification (shared with UserDetail + Velocity so the
  // three surfaces can't drift). One of null/'enforced'/'warn'/
  // 'not_enforced' — mutually exclusive, so a row never double-badges.
  // 'enforced' is already covered by the Status column's blocked chip;
  // here we render the 'warn' (amber, existing) and the NEW
  // 'not_enforced' (grey) signals.
  const overCap = classifyOverCap(user)
  const warnOverCap = overCap === 'warn'
  const notEnforcedOverCap = overCap === 'not_enforced'
  return (
    <div className="min-w-[8em]">
      <div className="text-right font-mono tabular-nums text-[13px]">
        {fmtUsd(spend)}
        <span className="text-[var(--ink-4)]"> / {cap != null ? fmtUsd(cap) : '—'}</span>
      </div>
      {est > 0 && unbilled > 0 && (
        <div
          className="text-right text-[11px] text-[var(--ink-4)] font-mono"
          title={
            `Estimated unbilled spend over the last ~${Math.round(unbilled)}h `
            + `of CUR lag, projected from this user's recent billed rate`
            + (lowSample ? ' (low-confidence: thin history → average)' : '')
          }
        >
          est. +{fmtUsd(est)} (unbilled ~{Math.round(unbilled)}h)
          {lowSample ? ' ·approx' : ''}
        </div>
      )}
      {warnOverCap && (
        <div
          className="mt-0.5 text-right text-[11px] font-semibold text-amber-700"
          title={
            'Projected (billed + estimated unbilled) is over this '
            + "user's cap, though billed alone is still under. Warning "
            + 'only — no block (estimate enforcement is set to Warn).'
          }
        >
          ⚠ approaching/over cap (estimated)
        </div>
      )}
      {notEnforcedOverCap && (
        <div
          className="mt-0.5 text-right text-[11px] font-semibold text-[var(--ink-3)]"
          title={notEnforcedTooltip(user, fmtUsd)}
        >
          over cap · not enforced
        </div>
      )}
      {pct != null && (
        <div className="mt-1 h-1 rounded bg-[var(--surface-2)] overflow-hidden" aria-hidden="true">
          <div
            className={
              'h-full rounded ' +
              (pct >= 100 ? 'bg-[var(--red)]'
                : pct >= 80 ? 'bg-amber-500'
                  : 'bg-[var(--accent)]')
            }
            style={{ width: `${Math.min(pct, 100)}%` }}
          />
        </div>
      )}
    </div>
  )
}

// AccessCell — #628: the admin role(s) on this principal (org/team
// admin) or "member". Grant-side context, not tg governance.
function AccessCell({ user }) {
  const roles = user.roles || []
  if (roles.length === 0) {
    return <span className="text-[var(--ink-4)]">member</span>
  }
  return (
    <div className="flex flex-wrap gap-1">
      {roles.map((r, i) => <RolePill key={i} role={r.role} />)}
    </div>
  )
}

// #818/#846: pure model for the governance-drift banner so the
// show/hide + per-row shape is unit-testable without a full render
// (matches this file's "test the exported helper" convention).
// Returns {show, count, sweepLabel, rows[]}. `show` is false for an
// empty/missing drift list (drift==0 OR the org-admin-only endpoint
// 403'd → caller passes []), so the banner never renders an empty
// shell.
//
// #846: each row carries PLAIN, action-oriented copy keyed on the
// stable `direction` enum (governed_no_deny / deny_no_governed) —
// NOT the raw expected/actual/detail internals — plus a link to the
// user's detail page (where the admin acts). The raw role ARN +
// expected/actual/detail stay on the row for the Technical-details
// disclosure, never the primary text.
const _DRIFT_COPY = {
  // governed=true but the deny isn't attached → enforcing nothing.
  governed_no_deny: {
    plain: who =>
      `${who} is set to be governed, but the spend and model `
      + `limits aren’t actually being enforced yet.`,
    action: who =>
      `Open ${who} and re-apply Govern to attach enforcement `
      + `(or Ungovern if they shouldn’t be governed).`,
  },
  // deny attached but the row is marked not-governed.
  deny_no_governed: {
    plain: who =>
      `${who} still has enforcement applied but is marked `
      + `ungoverned.`,
    action: who =>
      `Open ${who} and Ungovern to remove it (or Govern to keep `
      + `governing).`,
  },
}

export function driftBannerModel(drift, sweepAt) {
  const list = Array.isArray(drift) ? drift : []
  return {
    show: list.length > 0,
    count: list.length,
    // #846: friendlier "Last checked <time>" wording (no "sweep").
    sweepLabel: sweepAt
      ? `${String(sweepAt).slice(0, 16).replace('T', ' ')} UTC`
      : null,
    rows: list.map(d => {
      const who = d.email || d.identity_key || '(unknown principal)'
      const copy = _DRIFT_COPY[d.direction]
      const navId = d.identity_key || d.email
      return {
        key: d.identity_key || d.role_arn || d.email,
        who,
        direction: d.direction || null,
        // plain-language primary copy (falls back gracefully if a
        // future direction value arrives without a copy entry).
        plain: copy
          ? copy.plain(who)
          : `${who}’s enforcement doesn’t match its intended state.`,
        action: copy
          ? copy.action(who)
          : `Open ${who} to review and re-apply Govern/Ungovern.`,
        href: navId
          ? `#/users/${encodeURIComponent(navId)}`
          : null,
        // technical-details fields (disclosure only)
        roleArn: d.role_arn || null,
        expected: String(d.expected),
        actual: String(d.actual),
        detail: d.detail || null,
      }
    }),
  }
}

// one summary card. value === null → skeleton (still loading);
// a resolved value (number or formatted string) renders as-is.
export function SummaryTile({ label, value, sub }) {
  return (
    <Card className="px-4 py-4 border-t-4 border-t-[var(--accent)]">
      <div className="text-xs uppercase tracking-wider font-bold text-[var(--ink-3)]">{label}</div>
      {value === null ? (
        <SkeletonBlock className="mt-2 h-6 w-16 rounded" />
      ) : (
        <div className="mt-2 text-2xl font-semibold leading-none tabular-nums">{value}</div>
      )}
      <div className="mt-1 text-xs text-[var(--ink-4)]">{sub}</div>
    </Card>
  )
}

export default function Users() {
  const [users, setUsers] = useState(null)
  const [teamMap, setTeamMap] = useState(null)
  const [summary, setSummary] = useState(null)  // cards
  const [error, setError] = useState(null)
  const [statusFilter, setStatusFilter] = useState('all')
  // #628: 5-way caller filter — All / People / Machines /
  // Ungoverned / IDC. People/Machines view the session-name;
  // Ungoverned/IDC view the governance state. Persisted across
  // navigation (sessionStorage).
  const [callerFilter, setCallerFilter] = useState(() =>
    (typeof sessionStorage !== 'undefined' &&
      sessionStorage.getItem('users.callerFilter')) || 'all'
  )
  useEffect(() => {
    if (typeof sessionStorage !== 'undefined') {
      sessionStorage.setItem('users.callerFilter', callerFilter)
    }
  }, [callerFilter])
  const [search, setSearch] = useState('')
  const [showPrereg, setShowPrereg] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [sorting, setSorting] = useState([])
  // #818: governance-drift banner. The drift endpoint is org-admin
  // scoped (403s for everyone else), so we just fetch it and treat
  // ANY error — 403, network — as "no drift": fail-closed, no banner,
  // no console noise. drift==0 also renders nothing. This explains
  // the nav badge (#649) that was previously an unexplained dead-end.
  const [drift, setDrift] = useState([])
  const [driftSweepAt, setDriftSweepAt] = useState(null)

  const { selectedTeam, setSelectedTeam, persona } = useTeamScope()
  const isOrgAdmin = persona === 'org_admin'

  // Honor a ?team=<id> query in the hash (e.g. arriving from
  // a click-through on the V&C leaderboard). One-shot: clear
  // the param after applying so a later scope-dropdown change
  // wins.
  useEffect(() => {
    const h = window.location.hash || ''
    const q = h.indexOf('?')
    if (q === -1) return
    const params = new URLSearchParams(h.slice(q + 1))
    const t = params.get('team')
    if (t) {
      setSelectedTeam(t)
      window.history.replaceState(
        null, '', h.slice(0, q))
    }
  }, [])

  async function load() {
    try {
      setError(null)
      const [data, teamData, summaryData] = await Promise.all([
        api.listUsers({ team: selectedTeam }),
        getTeams(),
        // summary cards — role/team-scoped, re-fetched on team
        // switch (load() runs in the [selectedTeam] effect). Fail-soft:
        // a summary error shouldn't blank the user table, so swallow it
        // and leave the cards in their skeleton/zero state.
        getSummary(selectedTeam).catch(() => null),
      ])
      // Stamp the org-wide spend-estimate enforcement mode onto each
      // row so SpendCell can gate the warn badge without a separate
      // prop/context (it's a single org-level value).
      const estEnf = data.estimate_enforcement || 'off'
      setUsers((data.users || []).map(
        u => ({ ...u, estimate_enforcement: estEnf })))
      setSummary(summaryData)
      const m = {}
      for (const t of (teamData.teams || [])) m[t.team_id] = t.name
      setTeamMap(m)
      // If users reference teams we didn't get back, retry once. Seen on the
      // desktop binary's SigV4 proxy when /api/teams races a credential
      // refresh; the second call hits warm creds and returns the full set.
      const referenced = new Set(
        (data.users || [])
          .map(u => u.team_id)
          .filter(Boolean)
      )
      const missing = [...referenced].some(id => !(id in m))
      if (missing) {
        const retry = await getTeams()
        const m2 = {}
        for (const t of (retry.teams || [])) m2[t.team_id] = t.name
        setTeamMap(m2)
      }
    } catch (e) {
      setError(e.message)
    }
  }
  useEffect(() => { load() }, [selectedTeam])

  // #818: fetch the latest sweep's drift set. Fail-closed — any
  // error (network) → empty, so the banner simply doesn't render.
  // #703: GET /api/governance/drift is org-admin-only
  // (governance.py require_org_admin). The Users page is visible to
  // team_admins too, so firing it unconditionally put a 403 in their
  // console on a working page. Gate on the role the SPA already knows
  // from /api/whoami: only org_admin fetches drift; for everyone else
  // the banner stays hidden (same outcome as the old .catch, minus
  // the denied round-trip).
  useEffect(() => {
    if (!isOrgAdmin) { setDrift([]); setDriftSweepAt(null); return }
    api.governanceDrift()
      .then(d => {
        setDrift(d?.drift || [])
        setDriftSweepAt(d?.sweep_at || null)
      })
      .catch(() => { setDrift([]); setDriftSweepAt(null) })
  }, [isOrgAdmin])

  const filtered = useMemo(() => {
    if (!users) return []
    return users.filter(u => {
      if (statusFilter !== 'all' && u.status !== statusFilter) return false
      if (!matchesCallerFilter(u, callerFilter)) return false
      if (search) {
        const q = search.toLowerCase()
        const name = (u.display_name || '').toLowerCase()
        const idy = (u.email || u.identity_key || '').toLowerCase()
        const arn = (u.principal_arn || '').toLowerCase()
        const team = (teamMap?.[u.team_id] || u.team_id || '').toLowerCase()
        if (!name.includes(q) && !idy.includes(q) &&
            !arn.includes(q) && !team.includes(q)) return false
      }
      return true
    })
  }, [users, statusFilter, callerFilter, search, teamMap])

  // #628: counts shown next to each filter label.
  const callerCounts = useMemo(() => {
    const out = { all: 0, people: 0, machines: 0, ungoverned: 0, idc: 0 }
    if (!users) return out
    for (const u of users) {
      out.all += 1
      out[callerBucket(u)] += 1
      // #1011: ungoverned + idc are independent now (an IDC user can be
      // governed OR ungoverned). Count each by its own predicate.
      if (governanceState(u) === 'ungoverned') out.ungoverned += 1
      if (isIdc(u)) out.idc += 1
    }
    return out
  }, [users])

  const columns = useMemo(() => [
    {
      accessorKey: 'team_id',
      header: 'Team',
      cell: info => {
        const id = info.getValue()
        if (!id) return <em className="text-[var(--ink-4)]">—</em>
        if (!teamMap) return <SkeletonBlock className="h-4 w-24 rounded" />
        return teamMap[id] || id
      },
    },
    {
      id: 'user',
      accessorFn: row => row.display_name || row.email || row.identity_key || '',
      header: 'User',
      cell: info => <UserCell user={info.row.original} />,
    },
    {
      id: 'access',
      accessorKey: 'roles',
      header: 'Access',
      enableSorting: false,
      cell: info => <AccessCell user={info.row.original} />,
    },
    {
      id: 'governance',
      accessorFn: row => governanceState(row),
      header: () => <GovernanceLegend />,
      cell: info => <GovernanceCell user={info.row.original} />,
    },
    {
      id: 'spend',
      accessorKey: 'mtd_spend_usd',
      // #844: drive header alignment per-column from meta.align so the
      // sortable <th> right-aligns this numeric header to match
      // SpendCell's right-aligned numbers (the bare text-right div
      // below had no effect under the #832 inline-flex/text-left th).
      header: 'Spend / Cap',
      meta: { align: 'right' },
      cell: info => <SpendCell user={info.row.original} />,
    },
    {
      accessorKey: 'status',
      header: 'Status',
      cell: info => <StatusBadge status={info.getValue()} sub={subFor(info.row.original)} />,
    },
    {
      id: 'chevron',
      header: '',
      enableSorting: false,
      cell: () => (
        <span className="text-[var(--ink-4)] text-lg" aria-hidden="true">›</span>
      ),
    },
  ], [teamMap])

  const table = useReactTable({
    data: filtered, columns,
    state: { sorting }, onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })

  function goToDetail(u) {
    const id = u.identity_key || u.email
    window.location.hash = `#/users/${encodeURIComponent(id)}`
  }

  if (error) {
    return (
      <div className="p-8">
        <h1 className="text-2xl font-semibold mb-4">Users</h1>
        <div className="bg-red-50 border border-red-200 text-red-800 p-4 rounded">
          <strong>Error:</strong> {error}
        </div>
      </div>
    )
  }

  const isEmpty = users != null && users.length === 0

  return (
    <div className="p-8">
      <header className="flex justify-between items-center mb-4">
        <h1 className="text-2xl font-semibold m-0">Users</h1>
        <Button variant="primary" onClick={() => setShowPrereg(true)}>+ Pre-register user</Button>
      </header>

      {/* summary cards relocated here from Activity. Scoped to
          the current role/team selection (getSummary(selectedTeam)), so
          the counts agree with the user table by construction —
          "Blocked" comes from persisted User.status, not windowed
          spend. */}
      <div className="grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3 mb-5">
        <SummaryTile
          label="Active users"
          value={summary ? summary.active_users : null}
          sub="Month to date"
        />
        <SummaryTile
          label="≥90% of cap"
          value={summary ? summary.approaching_cap_count : null}
          sub="Approaching cap"
        />
        <SummaryTile
          label="Blocked"
          value={summary ? summary.blocked_count : null}
          sub="Status: blocked"
        />
        <SummaryTile
          label="Total spend"
          value={summary ? fmtUsd(summary.total_spend_usd || 0) : null}
          sub="Month to date"
        />
      </div>

      {/* #818: governance-drift banner. Renders only for an org-admin
          with drift>0 (the endpoint 403s for others → empty drift →
          nothing). Explains the nav badge (#649) and lists WHAT
          drifted. Informational + navigational only — the Users list
          stays OBSERVE-ONLY (#628). */}
      {(() => {
        const b = driftBannerModel(drift, driftSweepAt)
        if (!b.show) return null
        return (
          <div
            id="governance-drift"
            className="mb-4 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900"
          >
            <div className="font-semibold mb-1">
              {b.count} user{b.count === 1 ? " isn't" : "s aren't"} being
              governed as intended
            </div>
            <div className="mb-3 text-[13px]">
              {b.sweepLabel
                ? `Last checked ${b.sweepLabel}.`
                : ''}
            </div>
            <ul className="space-y-2">
              {b.rows.map((r, i) => (
                <li
                  key={r.key || i}
                  className="rounded border border-amber-200 bg-white/60 p-2"
                >
                  {/* #846: plain meaning + the row links to the user's
                      detail page (where the admin acts). */}
                  {r.href ? (
                    <a
                      href={r.href}
                      className="font-medium underline hover:no-underline"
                    >
                      {r.who}
                    </a>
                  ) : (
                    <div className="font-medium">{r.who}</div>
                  )}
                  <div className="text-[13px] mt-1">{r.plain}</div>
                  <div className="text-[13px] mt-1">
                    <span className="font-semibold">What to do: </span>
                    {r.action}
                  </div>
                  {/* #846/#837: raw internals behind a disclosure. */}
                  <details className="mt-2">
                    <summary className="text-[11px] text-amber-800 cursor-pointer select-none hover:text-amber-900">
                      Technical details
                    </summary>
                    <div className="mt-1 text-[11px] text-amber-800">
                      {r.roleArn && (
                        <div className="font-mono break-all">{r.roleArn}</div>
                      )}
                      <div className="mt-1">
                        expected <span className="font-mono">{r.expected}</span>
                        {' · '}actual <span className="font-mono">{r.actual}</span>
                      </div>
                      {r.detail && <div className="mt-1">{r.detail}</div>}
                    </div>
                  </details>
                </li>
              ))}
            </ul>
          </div>
        )
      })()}

      {/* #726 (#720 slice 4): spend is sourced from AWS-billed CUR,
          which lags ~24h, so a newly-active user/principal can take
          up to a day to appear here. */}
      <div className="mb-4 text-[13px] text-[var(--ink-4)]">
        New users can take up to ~24h to appear — spend is sourced
        from AWS billed-usage data (CUR), which is delivered with a
        delay.
      </div>

      {/* #628: grant-vs-govern explainer + governance icon legend,
          collapsed by default. */}
      <div className="mb-4">
        <button
          type="button"
          onClick={() => setShowHelp(v => !v)}
          aria-expanded={showHelp}
          className="inline-flex items-center gap-1.5 text-sm text-[var(--accent)] hover:underline"
        >
          {showHelp ? '✕ Hide' : 'ⓘ How governance works'}
        </button>
        {showHelp && (
          <div className="mt-2 p-4 rounded-lg bg-[var(--surface)] border border-[var(--border-2)] text-sm text-[var(--ink-2)] space-y-3">
            <p className="m-0">
              <strong>Grant</strong> (who can call Bedrock) happens
              outside tg — in IAM or IAM Identity Center. <strong>tg
              governs</strong> by <em>subtracting</em>: it attaches a
              deny policy to block listed models and enforce
              per-person spend caps. A principal appears here once it
              has invoked Bedrock; tg never grants access.
            </p>
            {/* #822: opt-in scope — enrollment is the gate. */}
            <p className="m-0">
              <strong>Governance is opt-in.</strong> tg enforces only
              on principals you’ve <strong>enrolled</strong>:{' '}
              <strong>Governed</strong> = enforced (cap + blocked-models
              deny); <strong>Ungoverned</strong> = ignored — no cap, no
              deny, <em>even if it spends past a cap</em>. Spend never
              auto-enrolls anyone. Use <strong>Govern</strong> on a
              principal to enroll it, <strong>Ungovern</strong> to
              remove it.
            </p>
            <div>
              <div className="font-medium mb-1">Governance column legend</div>
              <ul className="m-0 list-none space-y-1">
                {Object.entries(GOVERNANCE_LEGEND).map(([key, g]) => (
                  <li key={key} className="flex items-start gap-2">
                    <span className={`${g.cls} text-base w-4 text-center`} aria-hidden="true">{g.icon}</span>
                    <span><strong>{g.label}</strong> — {g.desc}</span>
                  </li>
                ))}
                {/* #1011: IDC is a qualifier, not a terminal state. */}
                <li className="flex items-start gap-2">
                  <span className={`${IDC_QUALIFIER.cls} text-base w-4 text-center`} aria-hidden="true">{IDC_QUALIFIER.icon}</span>
                  <span><strong>{IDC_QUALIFIER.label}</strong> — {IDC_QUALIFIER.desc}</span>
                </li>
              </ul>
            </div>
          </div>
        )}
      </div>

      {/* #628: 5-way caller filter. */}
      <div
        className="flex gap-2 my-4 items-center flex-wrap"
        role="group"
        aria-label="Filter principals"
      >
        {[
          { key: 'all',        label: 'All' },
          { key: 'people',     label: 'People' },
          { key: 'machines',   label: 'Machines' },
          { key: 'ungoverned', label: 'Ungoverned' },
          { key: 'idc',        label: 'IDC' },
        ].map(c => (
          <button
            key={c.key}
            type="button"
            aria-pressed={callerFilter === c.key}
            onClick={() => setCallerFilter(c.key)}
            className={
              'px-3 py-1 rounded-full text-xs font-medium border ' +
              (callerFilter === c.key
                ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
                : 'bg-white text-[var(--ink-3)] border-[var(--border-2)] hover:bg-[var(--surface-2)]')
            }
          >
            {c.label}
            <span className={
              'ml-1.5 text-[10px] ' +
              (callerFilter === c.key
                ? 'text-white/85' : 'text-[var(--ink-4)]')
            }>
              {callerCounts[c.key]}
            </span>
          </button>
        ))}
      </div>

      <div className="flex gap-4 my-4 items-center flex-wrap">
        <label className="text-sm flex items-center gap-2">
          Status:
          <select
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value)}
            className="px-2 py-1 border border-[var(--border)] rounded text-sm bg-white"
          >
            <option value="all">All</option>
            <option value="active">Active</option>
            <option value="blocked">Blocked</option>
            <option value="force_blocked">Force-blocked</option>
          </select>
        </label>
        <Input
          placeholder="Search name, email, or team…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="min-w-[20em]"
        />
        <span className="ml-auto text-sm text-[var(--ink-4)]">
          {filtered.length} of {users?.length ?? 0}
        </span>
      </div>

      <Card className="overflow-hidden">
        {(users == null || teamMap == null) ? (
          <div className="p-4 space-y-2">
            <SkeletonBlock className="h-8 rounded" />
            <SkeletonBlock className="h-8 rounded" />
            <SkeletonBlock className="h-8 rounded" />
          </div>
        ) : isEmpty ? (
          <div className="p-12 text-center text-[var(--ink-4)]">
            <div className="text-base font-medium mb-1">No principals discovered yet</div>
            <div className="text-sm">
              Principals appear here once they invoke Bedrock. Grant
              access in IAM / Identity Center, then check back after
              the next discovery cycle.
            </div>
          </div>
        ) : (
          <table className="w-full border-collapse text-sm">
            <thead>
              {table.getHeaderGroups().map(hg => (
                <tr key={hg.id} className="border-b-2 border-[var(--border)] bg-[var(--surface)]">
                  {hg.headers.map(h => {
                    const sort = h.column.getIsSorted()
                    const canSort = h.column.getCanSort()
                    // #844: per-column alignment from the column def's
                    // meta.align (default left). A right-aligned header
                    // needs BOTH text-right on the <th> AND a full-width
                    // inline-flex span justified to the end, so the
                    // label+sort-arrow group sits flush right over its
                    // right-aligned numeric cells.
                    const a = headerAlignClasses(h.column.columnDef.meta)
                    return (
                      <th
                        key={h.id}
                        onClick={canSort ? h.column.getToggleSortingHandler() : undefined}
                        className={
                          'p-3 text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)] select-none ' +
                          a.th +
                          (canSort ? ' cursor-pointer' : '')
                        }
                      >
                        <span className={
                          ('inline-flex items-center gap-1 ' + a.span).trim()
                        }>
                          {flexRender(h.column.columnDef.header, h.getContext())}
                          <span className="text-[10px] opacity-60">
                            {sort === 'asc' ? '▲' : sort === 'desc' ? '▼' : ''}
                          </span>
                        </span>
                      </th>
                    )
                  })}
                </tr>
              ))}
            </thead>
            <tbody>
              {table.getRowModel().rows.map(row => {
                const u = row.original
                return (
                  <tr
                    key={row.id}
                    onClick={() => goToDetail(u)}
                    onKeyDown={e => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        goToDetail(u)
                      }
                    }}
                    tabIndex={0}
                    role="link"
                    aria-label={`Open detail for ${u.display_name || u.email || sessionName(u)}`}
                    className="border-b border-[var(--border)] hover:bg-[var(--surface-2)] cursor-pointer focus:outline-none focus:bg-[var(--surface-2)] focus:ring-2 focus:ring-inset focus:ring-[var(--accent)]"
                  >
                    {row.getVisibleCells().map(c => (
                      <td key={c.id} className="p-3 align-middle">
                        {flexRender(c.column.columnDef.cell, c.getContext())}
                      </td>
                    ))}
                  </tr>
                )
              })}
              {table.getRowModel().rows.length === 0 && (
                <tr><td colSpan={7} className="p-8 text-center text-[var(--ink-4)]">
                  No principals match these filters.
                </td></tr>
              )}
            </tbody>
          </table>
        )}
      </Card>

      {showPrereg && <PreregisterModal onClose={() => { setShowPrereg(false); load() }} />}
    </div>
  )
}

function subFor(u) {
  // #750: temp-unblock "grace until" removed (no unblock_expires_at).
  if (u.status === 'active' && u.pct_used != null && u.pct_used >= 80)
    return `${u.pct_used}%`
  return null
}

function RolePill({ role }) {
  const tone = role === 'org_admin'
    ? 'bg-purple-50 text-purple-800 border-purple-200'
    : 'bg-blue-50 text-blue-800 border-blue-200'
  const label = role === 'org_admin' ? 'org admin' : 'team admin'
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs border ${tone}`}>
      {label}
    </span>
  )
}

function buildTeamOptions(teams) {
  const byParent = {}
  for (const t of teams) {
    const p = t.parent_team_id || ''
    if (!byParent[p]) byParent[p] = []
    byParent[p].push(t)
  }
  for (const list of Object.values(byParent))
    list.sort((a, b) => a.name.localeCompare(b.name))
  const out = []
  function walk(parentId, depth) {
    for (const t of (byParent[parentId] || [])) {
      out.push({ ...t, depth })
      walk(t.team_id, depth + 1)
    }
  }
  walk('', 0)
  const seen = new Set(out.map(t => t.team_id))
  for (const t of teams) if (!seen.has(t.team_id)) out.push({ ...t, depth: 0 })
  return out
}

function PreregisterModal({ onClose }) {
  const [email, setEmail] = useState('')
  const [teamId, setTeamId] = useState('')
  const [teams, setTeams] = useState([])
  const [cap, setCap] = useState('')
  const [useDefault, setUseDefault] = useState(true)
  const [notes, setNotes] = useState('')
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  // #946: after a successful pre-register, hold the modal open on an
  // informational notice (the new user has no AWS principal yet, so
  // it isn't governable until a role ARN is recorded or observed) —
  // surfaced at the point of creation, not left for a disabled-button
  // tooltip to reveal later.
  const [done, setDone] = useState(false)

  useEffect(() => {
    getTeams().then(d => setTeams(d.teams || [])).catch(() => {})
  }, [])

  const teamOptions = buildTeamOptions(teams)

  async function submit(e) {
    e.preventDefault()
    if (!teamId) { setErr('Team is required'); return }
    setBusy(true); setErr(null)
    try {
      await api.preregister({
        email: email.trim(),
        team_id: teamId,
        cap_usd: useDefault ? null : Number(cap),
        notes: notes.trim(),
      })
      setDone(true)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (done) {
    return (
      <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
        <div className="bg-white rounded-xl p-6 max-w-md w-full flex flex-col gap-3 shadow-xl">
          <h3 className="m-0 text-lg font-semibold">
            {email.trim()} pre-registered
          </h3>
          <div
            className="p-3 rounded border border-[var(--border-2)] bg-[var(--surface-2)] text-sm text-[var(--ink-3)]"
            role="note"
          >
            <div className="font-medium mb-1">
              {AWAITING_PRINCIPAL_CHIP.label}
              {' — '}{PREREGISTER_NOTICE_TITLE}
            </div>
            <div>{PREREGISTER_NOTICE_BODY}</div>
            <div className="mt-2 text-[var(--ink-4)]">
              Open the user to <strong>Add IAM role ARN</strong> now,
              or wait for their first Bedrock activity.
            </div>
          </div>
          <div className="mt-2 flex justify-end">
            <Button type="button" variant="primary" onClick={onClose}>
              Got it
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <form onSubmit={submit} className="bg-white rounded-xl p-6 max-w-md w-full flex flex-col gap-3 shadow-xl">
        <div className="flex justify-between items-center">
          <h3 className="m-0 text-lg font-semibold">Pre-register user</h3>
          <button type="button" onClick={onClose} className="text-xl text-[var(--ink-4)] hover:text-[var(--ink)]">✕</button>
        </div>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Email</span>
          <Input value={email} onChange={e => setEmail(e.target.value)} required />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Team <span className="text-[var(--red)]">*</span></span>
          <select
            value={teamId}
            onChange={e => setTeamId(e.target.value)}
            required
            className="px-3 py-2 border border-[var(--border)] rounded text-sm bg-white"
          >
            <option value="">— select a team —</option>
            {teamOptions.map(t => (
              <option key={t.team_id} value={t.team_id}>
                {'  '.repeat(t.depth)}{t.depth > 0 ? '└ ' : ''}{t.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={useDefault} onChange={e => setUseDefault(e.target.checked)} />
          <span>Use default cap</span>
        </label>
        {!useDefault && (
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Cap (USD/month)</span>
            <Input type="number" step="0.01" value={cap} onChange={e => setCap(e.target.value)} required />
          </label>
        )}
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Notes</span>
          <textarea
            value={notes}
            onChange={e => setNotes(e.target.value)}
            maxLength={500}
            rows={3}
            className="px-3 py-2 border border-[var(--border)] rounded text-sm font-sans"
          />
        </label>
        {err && <div className="bg-red-50 border border-red-200 text-red-800 p-2 rounded text-sm">{err}</div>}
        <div className="mt-2 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy}>{busy ? 'Submitting…' : 'Pre-register'}</Button>
        </div>
      </form>
    </div>
  )
}
