import React, { useEffect, useState } from 'react'
import { api, fmtUsd, getTeams } from '../api'
import StatusBadge from '../components/StatusBadge'
import TypedConfirmModal from '../components/TypedConfirmModal'
import SpendAsOf from '../components/SpendAsOf'
import GovernanceApplyStatus, { applyLabel } from '../components/GovernanceApplyStatus'
import { classifyOverCap, notEnforcedTooltip } from '../lib/overCap'
import { isExternalIdp, helpText, ssoName as ssoLabel } from '../lib/inviteCopy'
import { useTeamScope } from '../TeamScope'
import { Card } from '../ui/Card'
import { Button } from '../ui/Button'
import { Input } from '../ui/Input'
import {
  ROLE_ARN_RE, NO_ROLE_ARN_REASON, AWAITING_PRINCIPAL_CHIP,
  isAwaitingPrincipal, isRoleArn, isIdcRoleArn, arnAccountOf,
  IDC_GOVERN_NOTICE_TITLE, IDC_GOVERN_NOTICE_BODY,
  IDC_GOVERNED_PENDING_NOTE, IDC_GOVERNED_ENFORCED_NOTE,
  IDC_REMEDIATION_SUMMARY, IDC_REMEDIATION_BODY,
} from '../lib/governGate'

// The "How to finish enabling this →" disclosure — collapsed by
// default; the ONLY place the technical remediation appears (per the
// owner's UI-copy directive). Shown under a governed-but-pending IDC
// user's status. Plain <details> so it's keyboard-accessible with no
// extra deps.
export function IdcRemediationDisclosure() {
  return (
    <details className="mt-1">
      <summary className="text-[12px] text-[var(--accent)] cursor-pointer select-none">
        {IDC_REMEDIATION_SUMMARY} →
      </summary>
      <p className="text-[12px] text-[var(--ink-4)] m-0 mt-1">
        {IDC_REMEDIATION_BODY}
      </p>
    </details>
  )
}

// #608: break the role + account out of a principal ARN for the
// Access card. arn:aws:sts::<acct>:assumed-role/<role>/<session>
// → role=<role>, account=<acct>; also handles iam :role/ :user/.
export function arnRole(arn) {
  if (!arn) return null
  const m = arn.match(/:(?:assumed-role|role)\/([^/]+)/)
  if (m) return m[1]
  const u = arn.match(/:user\/(.+)$/)
  if (u) return `IAM user: ${u[1]}`
  if (/:root$/.test(arn)) return 'root'
  return null
}
export function arnAccount(arn) {
  if (!arn) return null
  // arn:aws:<svc>::<account>:... — account is the 5th colon-field.
  const parts = (arn || '').split(':')
  return parts.length > 4 && /^\d+$/.test(parts[4]) ? parts[4] : null
}

// #629/#856/#1011: the deny-only governance state for the panel +
// action gating — 'governed' (governed=true) or 'ungoverned'. #1011:
// IDC is NO LONGER terminal; an AWSReservedSSO_* user is governable
// (the deny lands via tg-consumer or the #1010 permission-set
// reference), so it's governed/ungoverned like any other and `isIdc`
// is a separate qualifier. Mirrors the Users-list governanceState.
export function isIdc(user) {
  return ((user && user.role_type) || 'iam') === 'idc'
}
export function governanceState(user) {
  return user.governed ? 'governed' : 'ungoverned'
}

// #629: per-principal quota keying string for the governance panel
// — how the deny matches this principal once governed. Email-pinned
// humans key on aws:userid; machine / unpinned roles key on
// aws:PrincipalArn (the role IS the identity; #627).
export function quotaKeying(user) {
  const email = user.email || ''
  if (email && email.includes('@')) {
    return { kind: 'aws:userid', value: `*:${email}` }
  }
  return {
    kind: 'aws:PrincipalArn',
    value: user.principal_arn || '(role ARN)',
  }
}

// #629/#750: gating reason for the directory actions that only make
// sense once tg can enforce. Returns null when the action is
// allowed, or a tooltip string when it's gated. Set cap / Force
// block are no-ops on a principal tg doesn't govern, so they're
// disabled until Governed. Set team is directory metadata — always
// allowed. #1011: IDC is no longer a terminal gate — a governed IDC
// user's cap/force-block apply like any governed principal (the deny
// statement is keyed the same way), so only the ungoverned-advisory
// gate remains.
export function actionGateReason(user) {
  const g = governanceState(user)
  if (g === 'ungoverned') {
    // #822: make the opt-in gate explicit — not governed = ignored.
    return 'This principal is not governed — tg enforces no cap or '
      + 'deny until you Govern (enroll) it, even if it spends past a '
      + 'cap. So a cap or block set here has no effect yet.'
  }
  return null
}

// #707: whether the Govern action can actually attach the deny.
// Govern does a one-time AttachRolePolicy, which needs an IAM role
// ARN. The server (users.py _role_name_from_arn) accepts ONLY
// `arn:aws:iam::<acct>:role/<name>` — the shape the aggregator
// rebuilds for assumed-role / service principals. A pre-registered
// user never observed in Bedrock has principal_arn=null; an IAM-user
// or root principal has a non-role ARN. All of those 400 server-side
// with "principal has no IAM role to attach the deny policy to".
// Returns null when Govern is attachable, else a tooltip explaining
// why the button is disabled — so the admin learns the reason
// instead of clicking into that 400 (the #707 dead-end).
const _ROLE_ARN_RE = ROLE_ARN_RE
export function manageGateReason(user) {
  if (!user) return null
  // #1011: Govern of an IDC user does NOT attach to its role (the deny
  // lands via tg-consumer or the #1010 permission-set reference), so
  // the "no attachable IAM role" gate does not apply — Govern is
  // always allowed (the advisory precondition is shown separately).
  if (isIdc(user)) return null
  // already-governed is handled by the panel's own branches; this
  // guard is specifically the "no attachable role" case that applies
  // while ungoverned.
  if (_ROLE_ARN_RE.test(user.principal_arn || '')) return null
  if (!user.principal_arn) {
    // #946: name the real blocker (no role ARN, NOT missing Bedrock
    // spend) and both ways out — shared with the row reason, the
    // chip tooltip, and the pre-register notice so they never drift.
    return NO_ROLE_ARN_REASON
  }
  return 'This principal has no attachable IAM role '
    + `(${user.principal_type || 'unknown'} — `
    + `${arnRole(user.principal_arn) || user.principal_arn}). `
    + 'tg can only attach the deny to an assumed-role / service '
    + 'role ARN, not to an IAM user or root.'
}

// #650: the caller's authority tier over THIS user, mirroring the
// server-side rule (api/auth.py Scope.can_admin_user / is_self).
// `me` is the whoami payload {email, persona, org_admin, team_ids}.
//   - canAdmin: org_admin (any user) OR team_admin whose subtree
//     (team_ids) includes the user's team. Gates management +
//     governance actions (govern/ungovern, cap, status, team,
//     disable/enable, unblock).
//   - isSelf: caller IS the target. Gates self-service (display
//     name, GitHub link) even for a plain member.
// UI gating is cosmetic — the API enforces the same matrix; this
// just hides controls the caller can't use.
export function userActionTier(me, user) {
  if (!me || !user) {
    return { isSelf: false, canAdmin: false, isOrgAdmin: false }
  }
  const isOrgAdmin = !!me.org_admin || me.persona === 'org_admin'
  const isSelf =
    !!me.email && !!user.email &&
    me.email.toLowerCase() === user.email.toLowerCase()
  const isTeamAdminOfUser =
    me.persona === 'team_admin' &&
    !!user.team_id &&
    (me.team_ids || []).includes(user.team_id)
  return {
    isSelf,
    isOrgAdmin,
    canAdmin: isOrgAdmin || isTeamAdminOfUser,
  }
}

// #837: per-action authority gate, matching the API authz exactly
// (verified against container/api/routes/users.py). Every management
// action is `require_team_admin_for` (org-admin OR team-admin of the
// user's team) EXCEPT delete_user, which is `require_org_admin`. The
// UI gate must be neither stricter nor looser than the endpoint, so
// a team-admin sees+uses everything the API lets them, and only
// delete is org-admin-only. `tier` is the userActionTier() result.
export function canPerform(action, tier) {
  if (!tier || !tier.canAdmin) return false
  if (action === 'delete_user') return !!tier.isOrgAdmin
  return true   // govern/ungovern/cap/set_team/force_block/unblock
}

// #821: whether to show the over-cap "Raise cap to unblock" affordance.
// TRUE only for the AUTO over-cap state (status='blocked'), never the
// manual force_blocked (that keeps its own Unblock button) or active —
// and only for an admin who can actually set the cap (the affordance
// routes into the existing CapModal, whose willUnblock path does the
// unblock). Pure + exported so the gating is unit-testable without
// mounting the page.
export function showOverCapAffordance(user, tier) {
  return (user || {}).status === 'blocked' && canPerform('cap', tier)
}

// #837: plain-language description of who a governed principal's spend
// cap applies to, and the scope — replacing the raw
// `aws:userid = *:<email>` / `aws:PrincipalArn` keying jargon in the
// primary copy. Mirrors quotaKeying()'s person-vs-role split.
export function capAppliesTo(user) {
  const email = user.email || ''
  if (email && email.includes('@')) {
    return {
      who: email,
      scope: 'this person, across any role they use',
    }
  }
  return {
    who: arnRole(user.principal_arn) || user.identity_key || 'this role',
    scope: 'this role’s sessions',
  }
}

function HashLink({ to, children }) {
  return (
    <a href={'#' + to} className="text-[var(--accent)] text-sm hover:underline">
      {children}
    </a>
  )
}

function ErrorBox({ children }) {
  if (!children) return null
  return (
    <div className="bg-red-50 border border-red-300 text-red-800 px-3 py-2 rounded text-sm my-2">
      {children}
    </div>
  )
}

function SuccessBox({ children }) {
  if (!children) return null
  return (
    <div className="bg-emerald-50 border border-emerald-300 text-emerald-800 px-3 py-2 rounded text-sm my-2">
      {children}
    </div>
  )
}

function InfoBox({ children }) {
  return (
    <div className="bg-amber-50 border border-amber-300 text-amber-900 px-3 py-2 rounded text-sm my-2">
      {children}
    </div>
  )
}

function StatCard({ title, value, sub }) {
  return (
    <Card className="px-4 py-3 min-w-[8em]">
      <div className="text-xs uppercase tracking-wider text-[var(--ink-3)]">{title}</div>
      <div className="text-2xl font-semibold mt-1 leading-none">{value}</div>
      {sub && <div className="text-xs text-[var(--ink-4)] mt-1">{sub}</div>}
    </Card>
  )
}

// #642: the Access-card "Status" badge state. Extracted + exported
// so the regression (it must follow `governed`, not the #345
// `managed` heuristic) is unit-testable. Precedence: IDC (not
// governable) → governed (Governed) → principal-type bucket
// (service / service_linked / root / unknown) → ungoverned. Govern/
// Ungovern toggle `governed`, so the badge now flips with them —
// the reported bug was this badge reading `user.managed`, which
// the action never changes.
export function accessBadgeState(user) {
  if ((user.role_type || 'iam') === 'idc') return 'idc'
  if (user.governed) return 'governed'
  const t = user.principal_type
  if (t === 'service') return 'service'
  if (t === 'service_linked') return 'service_linked'
  if (t === 'root') return 'root'
  if (!t || !user.principal_arn) return 'unknown'
  return 'ungoverned'
}

// #345/#642: Access card status badge + one-liner explanation.
// #642 fix: the badge now reflects the deny-only `governed` flag
// (the thing Govern/Ungovern toggles) so the Status visibly flips
// after the action. Previously it read `user.managed` — the #345
// "reaches Bedrock via tg-consumer" heuristic — which Govern never
// changes, so the panel looked inert (the reported bug). IDC is
// surfaced explicitly (not governable). The `governed` state takes
// precedence over the principal-type buckets below.
function AccessBadge({ user, enforcement }) {
  const t = user.principal_type
  // IDC is governable. A governed IDC user's badge tells the TRUTH
  // about enforcement: green "✓ Governed" ONLY when tg has VERIFIED the
  // deny reaches a role the user uses (enforcement.enforced — the
  // idc-enforcement endpoint); otherwise an amber "◆ Governed · pending
  // enforcement" (the honest default — governed intent set, not yet
  // active). Never present unverified intent as enforced.
  if (isIdc(user)) {
    if (user.governed) {
      const verified = enforcement?.enforced === true
      if (verified) {
        return (
          <div>
            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-emerald-100 text-emerald-900 border border-emerald-300">✓ Governed · ◆ IDC</span>
            <p className="text-[12px] text-[var(--ink-4)] m-0 mt-1">
              {IDC_GOVERNED_ENFORCED_NOTE}
            </p>
          </div>
        )
      }
      return (
        <div>
          <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-900 border border-amber-300">◆ Governed · pending enforcement</span>
          <p className="text-[12px] text-[var(--ink-4)] m-0 mt-1">
            {IDC_GOVERNED_PENDING_NOTE}
          </p>
          <IdcRemediationDisclosure />
        </div>
      )
    }
    return (
      <div>
        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-900 border border-amber-300">◆ IDC — governable</span>
        <p className="text-[12px] text-[var(--ink-4)] m-0 mt-1">
          {IDC_GOVERN_NOTICE_BODY}
        </p>
      </div>
    )
  }
  if (user.governed) {
    return (
      <div>
        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-emerald-100 text-emerald-900 border border-emerald-300">✓ Governed</span>
        <p className="text-[12px] text-[var(--ink-4)] m-0 mt-1">
          tg-BedrockQuotaDeny attached; the governance job maintains its quota statement.
        </p>
      </div>
    )
  }
  if (t === 'service') {
    return (
      <div>
        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-sky-100 text-sky-900 border border-sky-300">Service</span>
        <p className="text-[12px] text-[var(--ink-4)] m-0 mt-1">
          Machine principal. Per-user caps don't apply; per-role budgets are tracked separately (#346).
        </p>
      </div>
    )
  }
  if (t === 'service_linked') {
    return (
      <div>
        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-sky-100 text-sky-900 border border-sky-300">Service-linked</span>
        <p className="text-[12px] text-[var(--ink-4)] m-0 mt-1">
          AWS-managed service-linked role. TG cannot enforce — visible for spend attribution only.
        </p>
      </div>
    )
  }
  if (t === 'root') {
    return (
      <div>
        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-900 border border-red-300">Root</span>
        <p className="text-[12px] text-[var(--ink-4)] m-0 mt-1">
          Account root invocations. Audit-flag-worthy and not enforceable. Investigate.
        </p>
      </div>
    )
  }
  if (!t || !user.principal_arn) {
    return (
      <div>
        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-700 border border-gray-300">Unknown</span>
        <p className="text-[12px] text-[var(--ink-4)] m-0 mt-1">
          Not yet observed invoking Bedrock.
        </p>
      </div>
    )
  }
  // assumed_role / iam_user / federated, but not governed.
  return (
    <div>
      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-900 border border-amber-300">Ungoverned</span>
      <p className="text-[12px] text-[var(--ink-4)] m-0 mt-1">
        TG records this user's spend but does not enforce caps for this principal.
      </p>
    </div>
  )
}

function ModalShell({ onClose, children }) {
  return (
    <div className="fixed inset-0 bg-black/45 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <form
        onClick={e => e.stopPropagation()}
        onSubmit={e => e.preventDefault()}
        className="bg-white rounded-xl shadow-xl p-6 max-w-md w-full max-h-[90vh] overflow-y-auto flex flex-col"
      >
        {children}
      </form>
    </div>
  )
}

// #346: per-role budget card for service principals.
// Renders in place of the per-user Cap card on service
// rows. Loads the cap (if any), shows current state, and
// links to a budget editor modal. Recent alerts feed in
// from /api/service-account-caps/alerts.
function ServiceAccountBudgetSection({ identityKey, principalArn }) {
  const [cap, setCap] = useState(null)
  const [alerts, setAlerts] = useState([])
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  function load() {
    setErr(null)
    api.getServiceAccountCap(identityKey)
      .then(setCap)
      .catch(e => {
        if (e.status === 404) {
          setCap(null)
        } else {
          setErr(String(e))
        }
      })
    api.listServiceAccountAlerts(identityKey, 10)
      .then(r => setAlerts(r.alerts || []))
      .catch(() => {})
  }
  useEffect(() => { load() }, [identityKey])

  async function handleUnblock() {
    setBusy(true); setErr(null)
    try {
      await api.unblockServiceAccount(identityKey)
      load()
    } catch (e) { setErr(String(e)) }
    finally { setBusy(false) }
  }

  return (
    <section className="mt-6">
      <div className="rounded-lg border border-[var(--border)] bg-white p-4">
        <div className="flex items-baseline justify-between mb-2">
          <h3 className="text-base font-semibold m-0">Budget</h3>
          <Button variant="secondary" size="sm" onClick={() => setEditing(true)}>
            {cap ? 'Edit budget' : 'Set budget'}
          </Button>
        </div>
        {err && <ErrorBox>{err}</ErrorBox>}
        {!cap && (
          <p className="text-[12px] text-[var(--ink-4)] m-0">
            No budget set. Service principals don't get
            per-user caps; set a per-role budget here to
            track spend and (optionally) deny invocations
            when the role's role exhausts its allowance.
          </p>
        )}
        {cap && (
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <div className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Budget</div>
              <div className="text-lg font-mono">${cap.budget_usd?.toFixed(2)} / {cap.period}</div>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Mode</div>
              <div className="font-mono text-[12px]">{cap.mode}</div>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Owner emails</div>
              <div className="font-mono text-[12px] break-all">{cap.owner_emails || '—'}</div>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Status</div>
              {cap.blocked ? (
                <div className="flex items-center gap-2">
                  <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-900 border border-red-300">
                    Blocked
                  </span>
                  <Button variant="secondary" size="sm" disabled={busy} onClick={handleUnblock}>
                    {busy ? '…' : 'Unblock'}
                  </Button>
                </div>
              ) : (
                <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-emerald-100 text-emerald-900 border border-emerald-300">
                  Active
                </span>
              )}
            </div>
          </div>
        )}
        {alerts.length > 0 && (
          <div className="mt-4 pt-3 border-t border-[var(--border)]">
            <div className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)] mb-1">Recent alerts</div>
            <ul className="text-[12px] space-y-1 m-0 p-0 list-none">
              {alerts.slice(0, 5).map(a => (
                <li key={a.id} className="font-mono">
                  <span className="text-[var(--ink-4)]">{a.fired_at?.slice(0, 16) || ''}</span>
                  {' '}<strong>{a.kind}</strong>
                  {' '}({a.pct_of_budget?.toFixed(1)}%)
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
      {editing && (
        <ServiceAccountBudgetEditor
          identityKey={identityKey}
          principalArn={principalArn}
          existing={cap}
          onClose={() => setEditing(false)}
          onSaved={() => { setEditing(false); load() }}
        />
      )}
    </section>
  )
}

function ServiceAccountBudgetEditor({
  identityKey, principalArn, existing, onClose, onSaved,
}) {
  const [form, setForm] = useState({
    budget_usd: existing?.budget_usd ?? 100,
    period: existing?.period ?? 'month',
    mode: existing?.mode ?? 'alert_only',
    alert_threshold_pct: existing?.alert_threshold_pct ?? 80,
    owner_emails: existing?.owner_emails ?? '',
    grace_pct: existing?.grace_pct ?? 0,
    auto_unblock: existing?.auto_unblock ?? true,
  })
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState(null)

  async function handleSave() {
    setSaving(true); setErr(null)
    try {
      await api.putServiceAccountCap(identityKey, {
        ...form,
        budget_usd: Number(form.budget_usd),
        alert_threshold_pct: Number(form.alert_threshold_pct),
        grace_pct: Number(form.grace_pct),
      })
      onSaved()
    } catch (e) { setErr(String(e)) }
    finally { setSaving(false) }
  }

  return (
    <ModalShell onClose={onClose}>
      <h3 className="m-0 text-lg font-bold mb-2">
        {existing ? 'Edit' : 'Set'} budget for{' '}
        <code className="text-[12px] font-mono">{identityKey}</code>
      </h3>
      <p className="text-[12px] text-[var(--ink-4)] m-0 mb-3 break-all">
        {principalArn}
      </p>
      <Field label="Budget (USD)">
        <input
          type="number"
          step="0.01"
          min="0"
          className="border border-[var(--border-2)] rounded px-2 py-1 text-sm"
          value={form.budget_usd}
          onChange={e => setForm({ ...form, budget_usd: e.target.value })}
        />
      </Field>
      <Field label="Period">
        <select
          className="border border-[var(--border-2)] rounded px-2 py-1 text-sm"
          value={form.period}
          onChange={e => setForm({ ...form, period: e.target.value })}
        >
          <option value="day">Day</option>
          <option value="week">Week</option>
          <option value="month">Month</option>
        </select>
      </Field>
      <Field label="Mode">
        <select
          className="border border-[var(--border-2)] rounded px-2 py-1 text-sm"
          value={form.mode}
          onChange={e => setForm({ ...form, mode: e.target.value })}
        >
          <option value="alert_only">Alert only</option>
          <option value="alert_and_block">Alert and block</option>
          <option value="disabled">Disabled (track only)</option>
        </select>
      </Field>
      <Field label="Owner emails (comma-separated)">
        <input
          type="text"
          className="border border-[var(--border-2)] rounded px-2 py-1 text-sm font-mono"
          value={form.owner_emails}
          onChange={e => setForm({ ...form, owner_emails: e.target.value })}
          placeholder="bedrock-team@example.com"
        />
      </Field>
      <Field label="Alert threshold %">
        <input
          type="number"
          min="0"
          max="100"
          className="border border-[var(--border-2)] rounded px-2 py-1 text-sm"
          value={form.alert_threshold_pct}
          onChange={e => setForm({ ...form, alert_threshold_pct: e.target.value })}
        />
      </Field>
      <Field label="Grace % over budget">
        <input
          type="number"
          min="0"
          max="100"
          className="border border-[var(--border-2)] rounded px-2 py-1 text-sm"
          value={form.grace_pct}
          onChange={e => setForm({ ...form, grace_pct: e.target.value })}
        />
      </Field>
      <label className="text-sm flex items-center gap-2 my-2">
        <input
          type="checkbox"
          checked={form.auto_unblock}
          onChange={e => setForm({ ...form, auto_unblock: e.target.checked })}
        />
        Auto-unblock when spend drops below cap or new period starts
      </label>
      <ErrorBox>{err}</ErrorBox>
      <div className="flex gap-2 justify-end mt-3">
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button variant="primary" onClick={handleSave} disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </Button>
      </div>
    </ModalShell>
  )
}

function Field({ label, children }) {
  return (
    <label className="flex flex-col gap-1 my-3 text-sm">
      <span className="text-[11px] font-bold uppercase tracking-wider text-[var(--ink-3)]">{label}</span>
      {children}
    </label>
  )
}

export default function UserDetail({ email: emailProp }) {
  const email = emailProp || decodeURIComponent(
    (window.location.hash.match(/^#\/users\/(.+)$/) || [, ''])[1]
  )
  const { persona, me } = useTeamScope()
  const isOrgAdmin = persona === 'org_admin'
  const [user, setUser] = useState(null)
  // Verified enforcement for a governed IDC user ({state, enforced});
  // null until fetched / not applicable.
  const [idcEnforcement, setIdcEnforcement] = useState(null)
  const [error, setError] = useState(null)
  // #549: the header showed the raw team_id ("team 1.1") while
  // the Set-team dropdown shows the team NAME ("team1.1 - VS
  // Code") — a confusing mismatch. Load teams so the header can
  // render the same display name the dropdown uses.
  const [teams, setTeams] = useState([])
  const [orgDefaultCap, setOrgDefaultCap] = useState(null)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)
  const [showForceBlock, setShowForceBlock] = useState(false)
  const [showDelete, setShowDelete] = useState(false)
  const [showCap, setShowCap] = useState(false)
  const [showTeam, setShowTeam] = useState(false)
  // #629: display-name inline edit + govern/ungovern confirm.
  const [editingName, setEditingName] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  // Bedrock API key (IAM-user name) inline edit — org-admin only.
  const [editingKey, setEditingKey] = useState(false)
  const [keyDraft, setKeyDraft] = useState('')
  const [showUnmanage, setShowUnmanage] = useState(false)
  // #642: Govern is a real IAM mutation (attaches the deny) — it
  // must confirm first, same as Ungovern. Was firing immediately.
  const [showManage, setShowManage] = useState(false)
  const [showEnableLogin, setShowEnableLogin] = useState(false)  // #927
  const [showAddArn, setShowAddArn] = useState(false)  // #946

  async function load() {
    try {
      setError(null)
      const u = await api.getUser(email)
      setUser(u)
      // For a governed IDC user, fetch the VERIFIED enforcement state
      // so the badge can honestly show "enforced" vs "pending".
      // Non-fatal — a read failure leaves the honest pending default
      // (never claims enforced on a failed read).
      if ((u.role_type || 'iam') === 'idc' && u.governed) {
        api.getIdcEnforcement(email)
          .then(setIdcEnforcement)
          .catch(() => setIdcEnforcement(null))
      } else {
        setIdcEnforcement(null)
      }
    } catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [email])

  // #549: resolve team_id → display name for the header. Tolerate
  // failure (team_admin without team-list read) — falls back to
  // the raw id below.
  useEffect(() => {
    getTeams().then(d => setTeams(d.teams || [])).catch(() => {})
  }, [])
  const teamLabel = (() => {
    if (!user?.team_id) return null
    const t = teams.find(x => x.team_id === user.team_id)
    return t?.name || user.team_id
  })()

  // Org default cap powers the "Use org default ($X)" label
  // in CapModal AND the "Org default — $X" sub-label on the
  // Cap stat card for users with no per-user override. (#277)
  // Fetch is independent of user load: tolerated null on error
  // (e.g. team_admin who can't read /admin/config) — UI
  // gracefully falls back to the legacy "Use default cap" copy.
  useEffect(() => {
    if (!isOrgAdmin) return
    api.getAdminConfig()
      .then(d => {
        const v = d?.org_default_quota_usd
        setOrgDefaultCap(typeof v === 'number' ? v : null)
      })
      .catch(() => setOrgDefaultCap(null))
  }, [isOrgAdmin])

  async function act(fn, successMsg) {
    setBusy(true); setError(null)
    try {
      const res = await fn()
      // #827: successMsg may be a function of the API result so the
      // toast can reflect response state (e.g. "No longer governed,
      // and unblocked" only when the principal was force_blocked).
      setToast(
        typeof successMsg === 'function' ? successMsg(res) : successMsg)
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
      setTimeout(() => setToast(null), 4000)
    }
  }

  // Build the toast from the synchronous apply result so the admin
  // sees the ACTUAL post-apply state (enforced / still-over-cap /
  // pending-IDC / failed), not a fixed "done" string. Falls back to
  // `fallback` when the response carries no apply block.
  function applyToast(fallback) {
    return (res) => {
      const lbl = applyLabel(res && res.apply)
      if (!lbl) return fallback
      return lbl.note ? `${lbl.text} — ${lbl.note}` : lbl.text
    }
  }

  if (error && !user) {
    return (
      <div className="p-8">
        <HashLink to="/users">← Back</HashLink>
        <h1 className="mt-4 text-2xl font-semibold">User not found</h1>
        <ErrorBox>{error}</ErrorBox>
      </div>
    )
  }
  if (!user) return <div className="p-8 text-[var(--ink-4)]">Loading…</div>

  const status = user.status
  const actions = actionsFor(status, user)
  // #650: 3-tier action gate. canAdmin → management/governance
  // controls; isSelf → self-service (edit name, link GitHub) even
  // for a plain member. A member viewing their own page sees ONLY
  // the self-service controls.
  // #837: use the tier's authoritative isOrgAdmin (honors
  // me.org_admin AND persona) for the delete gate — NOT the
  // component-level `isOrgAdmin = persona === 'org_admin'` (line ~574,
  // kept only for the org-default-cap config fetch). Aliased so the
  // two don't collide.
  const {
    isSelf, canAdmin, isOrgAdmin: tierIsOrgAdmin,
  } = userActionTier(me, user)
  const canSelfService = isSelf || canAdmin

  return (
    <div className="p-8">
      <HashLink to="/users">← Back to Users</HashLink>
      <header className="mt-3 flex justify-between items-center flex-wrap gap-2">
        <div>
          {/* #629: display_name (admin label) is the primary
              heading when set, with the ARN-derived caller (email /
              identity) beneath as the read-only ground truth. The
              ✎ edit affordance sets users.display_name only — it
              never mutates the caller. */}
          <h1 className="m-0 text-2xl font-semibold flex items-center gap-2">
            {user.display_name || user.email || user.identity_key}
            {canSelfService && !editingName && (
              <button
                type="button"
                onClick={() => {
                  setNameDraft(user.display_name || '')
                  setEditingName(true)
                }}
                title="Edit display name"
                aria-label="Edit display name"
                className="text-sm text-[var(--ink-4)] hover:text-[var(--accent)]"
              >
                ✎ edit name
              </button>
            )}
          </h1>
          {editingName && (
            <DisplayNameEditor
              user={user}
              draft={nameDraft}
              setDraft={setNameDraft}
              busy={busy}
              onCancel={() => setEditingName(false)}
              onSave={() => act(async () => {
                await api.setDisplayName(
                  user.identity_key || user.email,
                  nameDraft.trim() || null,
                  user.version,
                )
                setEditingName(false)
              }, 'Display name updated')}
            />
          )}
          {user.display_name && (
            <div className="text-[12px] text-[var(--ink-4)] mt-0.5 font-mono">
              {user.email || user.identity_key}
            </div>
          )}
          <p className="m-0 mt-1 text-sm text-[var(--ink-3)] flex items-center gap-2 flex-wrap">
            <StatusBadge status={status} />
            {user.team_id && <span>· Team: {teamLabel}</span>}
            {user.last_seen_at && (
              <span>· Last seen: {user.last_seen_at.slice(0, 16).replace('T', ' ')}</span>
            )}
            {status === 'force_blocked' && user.force_blocked_at && (
              <span>· Force blocked {user.force_blocked_at.slice(0, 16).replace('T', ' ')}</span>
            )}
          </p>
        </div>
      </header>

      {/* #821: over-cap discovery affordance. When a user is auto-
          blocked for going over their spend cap (status='blocked', NOT
          the manual force_blocked), co-locate the remedy with the
          problem: explain the two #750 reprieves (raise the cap / wait
          for the monthly reset) and route "raise cap" into the existing
          CapModal, whose willUnblock path does the actual unblock +
          confirmation. Shown only to an admin who can set the cap
          (canPerform('cap')); force_blocked keeps its own Unblock
          button in Actions, untouched. */}
      {showOverCapAffordance(
        user, { canAdmin, isOrgAdmin: tierIsOrgAdmin }) && (
        <div
          className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900"
          role="status"
        >
          <div className="font-semibold mb-1">Over spend cap — Bedrock invocation is blocked</div>
          <p className="m-0 mb-3 text-[13px] leading-snug">
            Raise this user's cap above their month-to-date spend to
            restore access now, or access resets automatically at the
            start of next month. (There is no temporary unblock — the
            block follows spend vs. cap.)
          </p>
          <Button variant="primary" disabled={busy}
            onClick={() => setShowCap(true)}>
            Raise cap to unblock
          </Button>
        </div>
      )}

      <SuccessBox>{toast}</SuccessBox>
      <ErrorBox>{error}</ErrorBox>

      {/* Bedrock API key (IAM-user name) — org-admin only. Maps
          this developer to their long-term key so mantle / Codex CUR
          spend (billed to the key's IAM user, not a person) attributes
          to them. tg records only the NON-SECRET IAM-user name. */}
      {tierIsOrgAdmin && (
        <BedrockKeySection
          user={user}
          editing={editingKey}
          draft={keyDraft}
          setDraft={setKeyDraft}
          busy={busy}
          onEdit={() => {
            setKeyDraft(user.bedrock_key_user || '')
            setEditingKey(true)
          }}
          onCancel={() => setEditingKey(false)}
          onSave={() => act(async () => {
            await api.setBedrockKeyUser(
              user.identity_key || user.email,
              keyDraft.trim() || null,
              user.version,
            )
            setEditingKey(false)
          }, 'Bedrock key updated')}
        />
      )}

      <section className="flex gap-4 mt-6 flex-wrap">
        {/* #629: the API returns mtd_spend_usd (mtd_cost_usd was a
            latent typo — always undefined → blank card). */}
        <StatCard title="MTD spend" value={fmtUsd(user.mtd_spend_usd)} />
        {user.is_service ? (
          <StatCard
            title="Cap"
            value="—"
            sub="not applicable to service principals"
          />
        ) : (
          <StatCard
            title="Cap"
            value={
              user.cap_usd != null
                ? fmtUsd(user.cap_usd)
                : (user.effective_quota_usd > 0
                    ? fmtUsd(user.effective_quota_usd)
                    : '—')
            }
            sub={
              user.cap_source === 'user_override' ? 'override'
                : user.cap_source === 'org_default'
                  ? `Org default — ${fmtUsd(user.effective_quota_usd)}`
                  : 'no cap'
            }
          />
        )}
        <StatCard title="Used" value={user.pct_used != null ? `${user.pct_used}%` : '—'} />
      </section>

      {/* Over cap but NOT enforced — the cap is set and spend is over
          it, yet no deny/warn applies (ungoverned, or enforcement off).
          Reinforces the existing ungoverned advisory with the concrete
          number so the gap is unmistakable. Display-only; never a
          block. Neutral (not red) — red would imply an active deny. */}
      {classifyOverCap(user) === 'not_enforced' && (
        <div
          className="mt-3 rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2 text-[13px] text-[var(--ink-2)]"
          title={notEnforcedTooltip(user, fmtUsd)}
        >
          <span className="font-semibold">over cap · not enforced</span>
          {' — '}
          {notEnforcedTooltip(user, fmtUsd)}
        </div>
      )}

      <SpendAsOf className="mt-2" />

      {/* #345 / #608 Access card — surfaces the FULL principal_arn
          plus the role + account broken out of it (the full ARN is
          the verbose identity that belongs on detail, not the list,
          where only the role-session-name shows). */}
      <section className="mt-6">
        <div className="rounded-lg border border-[var(--border)] bg-white p-4">
          <h3 className="text-base font-semibold m-0 mb-2">Access</h3>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
            <div className="md:col-span-3">
              <div className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Principal ARN</div>
              <code className="text-[12px] break-all">
                {user.principal_arn || '—'}
              </code>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Role</div>
              <div className="font-mono text-[12px] break-all">
                {arnRole(user.principal_arn) || '—'}
              </div>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Account</div>
              <div className="font-mono text-[12px]">
                {arnAccount(user.principal_arn) || '—'}
              </div>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Principal type</div>
              <div className="font-mono text-[12px]">
                {user.principal_type || '—'}
              </div>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Status</div>
              <AccessBadge user={user} enforcement={idcEnforcement} />
            </div>
          </div>
        </div>
      </section>

      {/* #837: Governance panel is now STATUS-ONLY (plain language +
          Technical details disclosure). The Govern/Ungovern action
          moved into the single Actions bar below — one place for
          every action. */}
      <GovernancePanel user={user} enforcement={idcEnforcement} />

      {user.is_service && (
        <ServiceAccountBudgetSection
          identityKey={user.identity_key}
          principalArn={user.principal_arn}
        />
      )}

      {/* #837: ONE consolidated Actions area — every management
          action lives here (governance toggle, cap/team, then the
          destructive group). #650/#837: gated by canAdmin (org_admin
          OR team_admin of this user's team) to MATCH the API authz;
          delete_user is the only org-admin-only action (see
          canPerform). A member viewing their own page sees none of
          these — only the self-service edit-name + Link GitHub. */}
      {canAdmin && (
      <section className="mt-8">
        <h3 className="text-base font-semibold m-0 mb-3">Actions</h3>
        {(() => {
          const g = governanceState(user)
          const gateReason = actionGateReason(user)   // cap/force-block no-op gate
          const manageGate = manageGateReason(user)    // no attachable role
          const tier = { canAdmin, isOrgAdmin: tierIsOrgAdmin }
          // Each entry: render only if the action applies to the
          // current status (actionsFor) AND the caller may perform it
          // (canPerform → matches API authz). `disabledReason` is a
          // legible inline string (not hover-only) when the action is
          // visible but currently a no-op.
          return (
            <>
              {/* #946: informational notice (NOT an error) while the
                  principal is awaiting an AWS principal — sets the
                  lifecycle expectation at the point of action, and
                  points to both ways out. Shares one copy string with
                  the Govern reason + the Users-row chip. (Not for IDC —
                  isAwaitingPrincipal is false for IDC, which needs no
                  role ARN.) */}
              {isAwaitingPrincipal(user) && (
                <div
                  className="mb-3 p-3 rounded border border-[var(--border-2)] bg-[var(--surface-2)] text-sm text-[var(--ink-3)]"
                  role="note"
                >
                  <span
                    className="inline-block font-medium mb-1"
                    title={AWAITING_PRINCIPAL_CHIP.title}
                  >
                    {AWAITING_PRINCIPAL_CHIP.label}
                  </span>
                  <div>{NO_ROLE_ARN_REASON}</div>
                </div>
              )}
              {/* #1011: IDC advisory precondition (NOT an error) — Govern
                  is allowed, but the deny is advisory until it reaches a
                  role the user uses (tg-consumer, or the #1010
                  permission-set reference). aria-describedby ties it to
                  the Govern button below. */}
              {isIdc(user) && (
                <div
                  id="idc-govern-advisory"
                  className="mb-3 p-3 rounded border border-amber-300 bg-amber-50 text-sm text-[var(--ink-3)]"
                  role="note"
                >
                  <span className="inline-block font-medium mb-1">
                    {IDC_GOVERN_NOTICE_TITLE}
                  </span>
                  <div>{IDC_GOVERN_NOTICE_BODY}</div>
                </div>
              )}
              {/* Governance toggle — primary action, first. #1011: IDC
                  users get Govern/Ungovern like any other (the IDC manage
                  path is attach-free; the advisory note above states the
                  precondition). */}
              <div className="flex gap-2 flex-wrap items-start">
                {g === 'governed' && canPerform('ungovern', tier) && (
                  <Button variant="secondary" disabled={busy}
                    onClick={() => setShowUnmanage(true)}>
                    Ungovern
                  </Button>
                )}
                {g === 'ungoverned' && canPerform('govern', tier) && (
                  <Button variant="primary"
                    disabled={busy || !!manageGate}
                    aria-describedby={isIdc(user)
                      ? 'idc-govern-advisory' : undefined}
                    title={manageGate
                      || 'Enroll this principal so tg enforces its '
                         + 'cap / blocked-models deny.'}
                    onClick={() => setShowManage(true)}>
                    Govern — attach deny policy
                  </Button>
                )}

                {/* #946: the new path out of the no-ARN gate — let an
                    admin record the role ARN directly (no waiting for
                    Bedrock spend). Only for a governable-once-it-has-
                    an-ARN principal that's still awaiting one. */}
                {isAwaitingPrincipal(user)
                  && canPerform('govern', tier) && (
                  <Button variant="secondary" disabled={busy}
                    title={'Record this principal’s IAM role ARN '
                      + 'so tg can attach the deny — no Bedrock spend '
                      + 'needed.'}
                    onClick={() => setShowAddArn(true)}>
                    Add IAM role ARN
                  </Button>
                )}

                {/* Cap / team — non-destructive metadata + enforcement. */}
                {actions.includes('cap') && canPerform('cap', tier) && (
                  <Button variant="secondary"
                    disabled={busy || !!gateReason}
                    title={gateReason || undefined}
                    onClick={() => setShowCap(true)}>
                    Set cap
                  </Button>
                )}
                {actions.includes('set_team') && canPerform('set_team', tier) && (
                  <Button variant="secondary" disabled={busy} onClick={() => setShowTeam(true)}>
                    Set team
                  </Button>
                )}
                {/* #927: Enable login — show only for a tg-manageable
                    human (has email, not an IDC permission-set
                    principal) who has NO login yet. Hidden once
                    login_enabled so you can't double-invite. */}
                {user.email && (user.role_type || 'iam') !== 'idc'
                  && !user.is_service && !user.login_enabled && (
                  <Button variant="secondary" disabled={busy}
                    onClick={() => setShowEnableLogin(true)}>
                    Enable login
                  </Button>
                )}
              </div>

              {/* Destructive group — visually separated, last. */}
              {((actions.includes('force_block') && canPerform('force_block', tier))
                || (actions.includes('unblock') && canPerform('unblock', tier))
                || (actions.includes('delete_user') && canPerform('delete_user', tier))) && (
                <div className="flex gap-2 flex-wrap items-start mt-3 pt-3 border-t border-[var(--border-2)]">
                  {actions.includes('unblock') && canPerform('unblock', tier) && (
                    <Button variant="primary" disabled={busy}
                      onClick={() => act(() => api.unblock(email, user.version), applyToast('Unblocked — cap governance restored'))}>
                      Unblock
                    </Button>
                  )}
                  {actions.includes('force_block') && canPerform('force_block', tier) && (
                    <Button variant="destructive"
                      disabled={busy || !!gateReason}
                      title={gateReason || undefined}
                      onClick={() => setShowForceBlock(true)}>
                      Force block
                    </Button>
                  )}
                  {actions.includes('delete_user') && canPerform('delete_user', tier) && (
                    <Button variant="destructive" disabled={busy} onClick={() => setShowDelete(true)}>
                      Delete user
                    </Button>
                  )}
                </div>
              )}

              {/* Apply-timing status: govern/block/unblock take effect via
                  the deny_reconciler (~5-min tick), not instantly — show
                  whether the user's current state is live yet, with a quiet
                  "apply now →" link to the Jobs page. Same shared component
                  + wording as Org Settings → Blocked models. Governed
                  principals only (an ungoverned one has no deny to enforce). */}
              {g === 'governed' && (
                <GovernanceApplyStatus
                  updatedAt={user.governance_updated_at}
                  className="mt-3"
                />
              )}

              {/* a11y / #837: disabled reasons shown as legible text,
                  not carried by the hover tooltip alone. */}
              {gateReason && (actions.includes('cap') || actions.includes('force_block')) && (
                <p className="text-[12px] text-[var(--ink-4)] mt-2 m-0">
                  Set cap and Force block are disabled here: {gateReason}
                </p>
              )}
              {g === 'ungoverned' && manageGate && (
                <p className="text-[12px] text-[var(--ink-4)] mt-2 m-0">
                  Govern is disabled here: {manageGate}
                </p>
              )}
            </>
          )
        })()}
      </section>
      )}

      <TypedConfirmModal
        open={showForceBlock}
        title={`Force block ${email}`}
        highlightText="⏳ Takes effect within a few minutes, on the next governance job run — not instantly."
        bodyText={`Blocks ${email} from invoking Bedrock, regardless of spend, until you Unblock. Other AWS access is unaffected. (Unblocking returns them to normal cap governance — if they're over cap they stay blocked until spend drops or you raise the cap.)`}
        matchString={email}
        confirmLabel="Force block"
        onCancel={() => setShowForceBlock(false)}
        onConfirm={() => {
          setShowForceBlock(false)
          act(() => api.forceBlock(email, email, user.version), applyToast('Force blocked'))
        }}
      />

      <TypedConfirmModal
        open={showDelete}
        title={`Delete ${email}`}
        bodyText={`This will permanently delete ${email} from the system. This cannot be undone. Only allowed because this user has never invoked Bedrock.`}
        matchString={email}
        confirmLabel="Delete"
        onCancel={() => setShowDelete(false)}
        onConfirm={async () => {
          setShowDelete(false)
          setBusy(true); setError(null)
          try {
            await api.deleteUser(email)
            window.location.hash = '/users'
          } catch (e) {
            setError(e.message)
            setBusy(false)
          }
        }}
      />

      {showCap && (
        <CapModal
          email={email}
          current={user.cap_usd}
          mtd={user.mtd_spend_usd}
          status={status}
          version={user.version}
          orgDefaultCap={orgDefaultCap}
          onClose={() => setShowCap(false)}
          onDone={(msg) => { setShowCap(false); setToast(msg); load() }}
        />
      )}

      {showTeam && (
        <TeamModal
          email={email}
          currentTeam={user.team_id}
          version={user.version}
          onClose={() => setShowTeam(false)}
          onDone={(msg) => { setShowTeam(false); setToast(msg); load() }}
        />
      )}

      {showEnableLogin && (
        <EnableLoginModal
          user={user}
          onClose={() => setShowEnableLogin(false)}
          onDone={(msg) => {
            setShowEnableLogin(false); setToast(msg); load()
          }}
        />
      )}

      {showAddArn && (
        <AddArnModal
          user={user}
          onClose={() => setShowAddArn(false)}
          onDone={(msg) => { setShowAddArn(false); setToast(msg); load() }}
        />
      )}

      {showManage && (
        <ManageModal
          user={user}
          busy={busy}
          onClose={() => setShowManage(false)}
          onConfirm={() => {
            setShowManage(false)
            act(
              () => api.govern(
                user.identity_key || user.email, user.version),
              // Enforcement is applied synchronously now — the toast
              // reflects the ACTUAL result (enforced, or pending for an
              // IDC user), not a fixed "~few minutes".
              applyToast('Now governed.'),
            )
          }}
        />
      )}

      {showUnmanage && (
        <UnmanageModal
          user={user}
          busy={busy}
          onClose={() => setShowUnmanage(false)}
          onConfirm={() => {
            setShowUnmanage(false)
            act(
              () => api.ungovern(
                user.identity_key || user.email, user.version),
              // #827: when ungovern also lifted a force-block, say so.
              (res) => res?.unblocked
                ? 'No longer governed, and unblocked — the force-block '
                  + 'was lifted. Any spend-cap block is removed within '
                  + 'a few minutes, on the next governance job run.'
                : 'No longer governed. Any spend-cap block is removed '
                  + 'within a few minutes, on the next governance job '
                  + 'run.',
            )
          }}
        />
      )}

      <LinkedAccountsSection email={email} canEdit={canSelfService} />
    </div>
  )
}

// #629: inline display-name editor (header). Sets
// users.display_name only — never the ARN-derived caller.
function DisplayNameEditor({ user, draft, setDraft, busy, onCancel, onSave }) {
  return (
    <div className="flex items-center gap-2 mt-1">
      <Input
        value={draft}
        onChange={e => setDraft(e.target.value)}
        placeholder="Friendly name (e.g. Acme nightly batch)"
        autoFocus
        className="max-w-[20em]"
        onKeyDown={e => {
          if (e.key === 'Enter') onSave()
          if (e.key === 'Escape') onCancel()
        }}
      />
      <Button variant="primary" size="sm" disabled={busy} onClick={onSave}>
        {busy ? 'Saving…' : 'Save'}
      </Button>
      <Button variant="secondary" size="sm" onClick={onCancel}>Cancel</Button>
      {draft && (
        <button
          type="button"
          onClick={() => setDraft('')}
          className="text-[12px] text-[var(--ink-4)] hover:text-[var(--red)]"
          title="Clear the display name (revert to the caller identity)"
        >
          clear
        </button>
      )}
    </div>
  )
}

// The Bedrock API key (IAM-user name) card. Org-admin only.
// Maps a developer to the long-term Bedrock API key that backs their
// mantle / Codex traffic, so that key's CUR spend (billed to the key's
// IAM user, not a person) attributes to them. States: empty (help +
// placeholder), populated (value + Clear), editing, saving/error
// (surfaced by the parent's toast/ErrorBox via act()).
export function BedrockKeySection({
  user, editing, draft, setDraft, busy, onEdit, onCancel, onSave,
}) {
  const mapped = user.bedrock_key_user
  return (
    <Card className="mt-4 px-4 py-3">
      <div className="text-sm font-semibold mb-1">
        Bedrock API key (IAM user name)
      </div>
      <p className="m-0 mb-2 text-[12px] leading-snug text-[var(--ink-3)]">
        The IAM user name of this developer&rsquo;s long-term Bedrock API
        key (e.g. <code>MantleApiKey-abc123</code>) — find it in the AWS
        console under the key you created. tg uses it to attribute
        bedrock-mantle / Codex spend to this user.{' '}
        <strong>tg never sees the key secret.</strong>
      </p>
      {!editing ? (
        <div className="flex items-center gap-2">
          {mapped ? (
            <span className="font-mono text-[13px]">{mapped}</span>
          ) : (
            <span className="text-[var(--ink-4)]">—</span>
          )}
          <button
            type="button"
            onClick={onEdit}
            className="text-sm text-[var(--ink-4)] hover:text-[var(--accent)]"
            aria-label="Edit Bedrock API key"
          >
            ✎ {mapped ? 'edit' : 'set key'}
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <Input
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder="MantleApiKey-abc123"
            aria-label="Bedrock API key IAM user name"
            autoFocus
            className="max-w-[20em] font-mono"
            onKeyDown={e => {
              if (e.key === 'Enter') onSave()
              if (e.key === 'Escape') onCancel()
            }}
          />
          <Button variant="primary" size="sm" disabled={busy} onClick={onSave}>
            {busy ? 'Saving…' : 'Save'}
          </Button>
          <Button variant="secondary" size="sm" onClick={onCancel}>
            Cancel
          </Button>
          {draft && (
            <button
              type="button"
              onClick={() => setDraft('')}
              className="text-[12px] text-[var(--ink-4)] hover:text-[var(--red)]"
              title="Clear the Bedrock key mapping"
            >
              clear
            </button>
          )}
        </div>
      )}
    </Card>
  )
}

// #837: the Governance panel is now STATUS-ONLY and plain-language.
// The Govern/Ungovern actions moved to the consolidated Actions bar.
// Leads with admin-friendly meaning ("Governed on role …", "Spend
// cap applies to: <who> (<scope>)") and a plain self-detach warning;
// the raw ARN / aws:userid|aws:PrincipalArn condition strings live in
// an optional "Technical details" <details> disclosure for those who
// want them.
export function GovernancePanel({ user, enforcement }) {
  const g = governanceState(user)
  const role = arnRole(user.principal_arn)
  const keying = quotaKeying(user)
  const applies = capAppliesTo(user)
  // A governed IDC user is "enforced" ONLY when tg has VERIFIED it (the
  // idc-enforcement endpoint). Absent/unverified → pending (the honest
  // default). Non-IDC users ignore this.
  const idcEnforced = enforcement?.enforced === true

  return (
    <section className="mt-6">
      <div className="rounded-lg border border-[var(--border)] bg-white p-4">
        <h3 className="text-base font-semibold m-0 mb-1">Governance</h3>

        {/* #1011: IDC is no longer a terminal "not governable" panel.
            An IDC user is governed/ungoverned like any other; the
            governed/ungoverned copy below carries an IDC-specific note
            for the advisory permission-set enforcement path. */}
        {(
          <>
            {g === 'governed' ? (
              <>
                {/* For a governed IDC user the badge + line must not
                    claim enforcement tg hasn't verified. Green + "tg is
                    enforcing" only when enforcement.enforced is true;
                    otherwise amber "pending enforcement" + the honest
                    note + the technical-remediation disclosure. */}
                {isIdc(user) && !idcEnforced ? (
                  <>
                    <p className="text-[13px] text-[var(--ink-2)] mt-1 mb-2 m-0">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-900 border border-amber-300 mr-2">
                        ◆ Governed · pending enforcement
                      </span>
                    </p>
                    <p className="text-[13px] text-[var(--ink-3)] mt-1 mb-1 m-0">
                      {IDC_GOVERNED_PENDING_NOTE}
                    </p>
                    <IdcRemediationDisclosure />
                  </>
                ) : (
                <>
                <p className="text-[13px] text-[var(--ink-2)] mt-1 mb-2 m-0">
                  <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-emerald-100 text-emerald-900 border border-emerald-300 mr-2">
                    {isIdc(user) ? '✓ Governed · ◆ IDC' : '✓ Governed'}
                  </span>
                  tg is enforcing this principal’s spend cap and the
                  org model block-list.
                </p>
                {isIdc(user) ? (
                  <p className="text-[13px] text-[var(--ink-3)] mt-1 mb-1 m-0">
                    {IDC_GOVERNED_ENFORCED_NOTE}
                  </p>
                ) : (
                <div className="text-[13px] text-[var(--ink-3)] space-y-1 mb-1">
                  <div>
                    <strong>Governed on role:</strong>{' '}
                    {role || '—'}
                    <span className="text-[var(--ink-4)]">
                      {' '}— tg’s restriction is attached to this role.
                    </span>
                  </div>
                  <div>
                    <strong>Spend cap applies to:</strong>{' '}
                    {applies.who}
                    <span className="text-[var(--ink-4)]">
                      {' '}({applies.scope}).
                    </span>
                  </div>
                </div>
                )}
                {/* plain-language self-detach advisory (#837); for IDC
                    the analogous caveat is the pending-enforcement note
                    shown in the amber branch above. */}
                {!isIdc(user) && (
                <p className="text-[12px] text-[var(--ink-4)] m-0 mt-2">
                  Heads up: a role with permission to change its own
                  policies can remove tg’s restriction, so enforcement
                  here is best-effort, not guaranteed.
                </p>
                )}
                </>
                )}
              </>
            ) : (
              <>
              <p className="text-[13px] text-[var(--ink-3)] mt-1 mb-1 m-0">
                <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-900 border border-amber-300 mr-2">
                  {isIdc(user) ? '○ Ungoverned · ◆ IDC' : '○ Ungoverned'}
                </span>
                tg records this principal’s spend but isn’t enforcing
                anything yet. Use <strong>Govern</strong> in Actions to
                turn on its spend cap and the model block-list.
              </p>
              {isIdc(user) && (
                <p className="text-[12px] text-[var(--ink-4)] mt-1 m-0">
                  {IDC_GOVERN_NOTICE_BODY}
                </p>
              )}
              </>
            )}

            {/* #837: raw IAM internals tucked behind a disclosure. */}
            <details className="mt-3">
              <summary className="text-[12px] text-[var(--ink-4)] cursor-pointer select-none hover:text-[var(--ink-2)]">
                Technical details
              </summary>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-[13px] mt-2">
                <div>
                  <div className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Attaches to role</div>
                  <div className="font-mono text-[12px] break-all">{role || '—'}</div>
                </div>
                <div>
                  <div className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Quota keying</div>
                  <div className="font-mono text-[12px] break-all">
                    {keying.kind} = {keying.value}
                  </div>
                </div>
              </div>
            </details>
          </>
        )}
      </div>
    </section>
  )
}

// #629: Ungovern confirm — copy reflects that the policy detaches
// only when no other governed principal remains on the role.
// #642: Govern confirm — a real IAM mutation (one-time
// AttachRolePolicy), so it must say what will happen before
// firing, same as Ungovern. Surfaces the role + the quota keying.
function ManageModal({ user, busy, onClose, onConfirm }) {
  const role = arnRole(user.principal_arn)
  const keying = quotaKeying(user)
  return (
    <ModalShell onClose={onClose}>
      <h3 className="m-0 text-lg font-bold mb-2">
        Govern {user.display_name || user.email || user.identity_key}?
      </h3>
      <p className="text-sm text-[var(--ink-3)] m-0 mb-3">
        tg will attach the
        <span className="font-mono text-[12px]"> tg-BedrockQuotaDeny</span>
        policy to role
        <span className="font-mono text-[12px]"> {role || '(its role)'}</span> (a
        one-time, deny-only <span className="font-mono text-[12px]">AttachRolePolicy</span> —
        worst case it over-restricts, it never grants access) and mark
        this principal <strong>Governed</strong>. Enforcement (its quota
        deny, keyed on
        <span className="font-mono text-[12px]"> {keying.kind} = {keying.value}</span>,
        plus the org model block-list) takes effect within a few
        minutes, on the next governance job run. You can Ungovern later
        to reverse it.
      </p>
      <ErrorBox>{null}</ErrorBox>
      <div className="flex gap-2 justify-end mt-3">
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={busy} onClick={onConfirm}>
          {busy ? 'Governing…' : 'Govern'}
        </Button>
      </div>
    </ModalShell>
  )
}

// #927: Enable login — authorize a person + (Cognito only) provision
// their user and send the invite. IdP-aware copy: Cognito says "we'll
// email them; they use Forgot password" (#921); an external IdP says
// "they sign in via <SSO>; no email". Role defaults to member.
export function EnableLoginModal({ user, onClose, onDone }) {
  const [role, setRole] = useState('member')
  const [providers, setProviders] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    api.authProviders().then(setProviders).catch(() => setProviders({}))
  }, [])
  // Auth branch + SSO name come from the shared inviteCopy helper (the
  // same one the Add-user modal uses) so the two surfaces never
  // diverge. Until providers load, treat as Cognito (the common
  // default) so the copy isn't misleading.
  const external = providers ? isExternalIdp(providers) : false
  const sso = ssoLabel(providers || {})
  const who = user.display_name || user.email
  // App sign-in URL = the ALB origin the admin's browser is on, never
  // hand-typed. Displayed + copyable so the admin can relay it.
  const origin = (typeof window !== 'undefined' && window.location)
    ? window.location.origin : ''
  const appUrl = origin ? origin.replace(/\/+$/, '') + '/login' : '/login'

  async function copyUrl() {
    try { await navigator.clipboard.writeText(appUrl) } catch { /* insecure ctx */ }
    setCopied(true)
  }

  async function submit() {
    setBusy(true); setErr(null)
    try {
      const res = await api.enableLogin(user.email, { role })
      // Toast repeats the next step (shown in the modal AND here).
      // Cognito → the invite email is self-sufficient (it carries the
      // sign-in link + a temporary password), so the admin has nothing
      // to relay; external IdP → no email is sent, so the admin must
      // grant access in the IdP and relay the URL. Key the branch off
      // the server's cognito_provisioned (authoritative for what
      // actually happened).
      const msg = res.cognito_provisioned
        ? `Login created for ${who} — invite emailed with their sign-in `
          + `link and a temporary password.`
        : `Login authorized for ${who} — they sign in via ${sso} (no `
          + `email sent). Grant them access to the tg app in your `
          + `identity provider (${sso}), then send them ${appUrl}.`
      onDone(msg)
    } catch (e) { setErr(e.message); setBusy(false) }
  }
  return (
    <ModalShell onClose={onClose}>
      <h3 className="m-0 text-lg font-bold mb-2">Enable login for {who}?</h3>
      {providers && (
        external ? (
          <div
            className="p-3 mb-3 rounded border border-amber-300 bg-amber-50 text-sm text-[var(--ink-3)]"
            role="note"
          >
            <div className="font-semibold mb-1">Two steps — both on your side</div>
            <ol className="m-0 pl-4 list-decimal">
              <li>Grant <span className="font-mono text-[12px]">{user.email}</span> access
                  to the tg app in your identity provider ({sso}) — without
                  that assignment the SSO login fails.</li>
              <li>Send them the sign-in URL below.</li>
            </ol>
          </div>
        ) : (
          <p className="text-sm text-[var(--ink-3)] m-0 mb-3" role="note">
            {helpText(providers)} The sign-in link below is theirs too —
            share it only as a backup if the emailed invite doesn’t
            arrive.
          </p>
        )
      )}
      <label className="block text-sm font-medium mb-1">Role</label>
      <select className="w-full border rounded px-2 py-1 mb-2 text-sm"
        value={role} onChange={e => setRole(e.target.value)} disabled={busy}>
        <option value="member">Member (no admin access)</option>
        <option value="team_admin">Team admin</option>
        <option value="org_admin">Org admin</option>
      </select>
      <label className="block text-sm font-medium mb-1">Sign-in URL</label>
      <div className="flex gap-2 mb-1">
        <input
          readOnly
          aria-label="Sign-in URL"
          value={appUrl}
          className="flex-1 border rounded px-2 py-1 text-sm font-mono bg-[var(--surface-2)]"
        />
        <Button type="button" variant="secondary" onClick={copyUrl}>Copy</Button>
      </div>
      <span role="status" aria-live="polite" className="text-[12px] text-[var(--ink-4)]">
        {copied ? 'Sign-in URL copied' : ''}
      </span>
      <p className="text-xs text-[var(--ink-3)] m-0 mt-2 mb-2">
        The member view is still in progress — a member can sign in but
        lands on a limited page for now.
      </p>
      <ErrorBox>{err}</ErrorBox>
      <div className="flex gap-2 justify-end mt-3">
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={busy} onClick={submit}>
          {busy
            ? (external ? 'Authorizing…' : 'Creating…')
            : (external ? 'Authorize & show invite'
                        : 'Create login & send invite')}
        </Button>
      </div>
    </ModalShell>
  )
}

// #946: record a principal's IAM role ARN on a pre-registered /
// ARN-less user, so Govern is attachable WITHOUT waiting for Bedrock
// spend. Validates the role-ARN shape client-side (mirrors the server
// _role_name_from_arn), rejects IDC permission-set roles (never
// governable), and warns — but doesn't block — on a cross-account
// ARN; the server is the final authority and only ever attaches the
// deny-only policy.
function AddArnModal({ user, onClose, onDone }) {
  const [arn, setArn] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const who = user.display_name || user.email || user.identity_key
  const trimmed = arn.trim()
  const deployAcct = arnAccount(user.principal_arn) // usually null here
  const enteredAcct = arnAccountOf(trimmed)
  const shapeOk = isRoleArn(trimmed)
  const idc = isIdcRoleArn(trimmed)
  // Cross-account warn only when we actually know the deployment
  // account from some existing ARN on the row (rare for ARN-less).
  const crossAcct = !!deployAcct && !!enteredAcct
    && deployAcct !== enteredAcct

  async function submit() {
    setBusy(true); setErr(null)
    try {
      await api.setPrincipalArn(
        user.identity_key || user.email, trimmed, user.version)
      onDone(
        `Recorded the IAM role ARN for ${who} — you can Govern now `
        + '(no Bedrock spend required).')
    } catch (e) { setErr(e.message); setBusy(false) }
  }

  return (
    <ModalShell onClose={onClose}>
      <h3 className="m-0 text-lg font-bold mb-2">
        Add IAM role ARN for {who}
      </h3>
      <p className="text-sm text-[var(--ink-3)] m-0 mb-3">
        tg attaches the spend cap / deny to this principal’s IAM role.
        Record the role ARN to make <strong>Govern</strong> available
        now — otherwise it’s filled in automatically once the
        principal is observed at Bedrock.
      </p>
      <label className="block text-sm font-medium mb-1">
        Role ARN
      </label>
      <Input
        value={arn}
        onChange={e => setArn(e.target.value)}
        placeholder="arn:aws:iam::123456789012:role/tg-consumer"
        disabled={busy}
        autoFocus
      />
      {trimmed && !shapeOk && (
        <p className="text-xs text-[var(--red)] m-0 mt-1">
          Must be an IAM role ARN
          (<span className="font-mono">arn:aws:iam::&lt;acct&gt;:role/&lt;name&gt;</span>)
          — not an IAM user or root.
        </p>
      )}
      {shapeOk && idc && (
        <p className="text-xs text-[var(--red)] m-0 mt-1">
          IDC permission-set roles (AWSReservedSSO_*) aren’t governable
          by tg — govern via the permission set policy or an SCP.
        </p>
      )}
      {shapeOk && !idc && crossAcct && (
        <p className="text-xs text-amber-700 m-0 mt-1">
          This ARN’s account ({enteredAcct}) differs from the
          principal’s ({deployAcct}). tg can only attach in the
          deployment account.
        </p>
      )}
      <ErrorBox>{err}</ErrorBox>
      <div className="flex gap-2 justify-end mt-3">
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button variant="primary"
          disabled={busy || !shapeOk || idc}
          onClick={submit}>
          {busy ? 'Saving…' : 'Save role ARN'}
        </Button>
      </div>
    </ModalShell>
  )
}

export function UnmanageModal({ user, busy, onClose, onConfirm }) {
  const role = arnRole(user.principal_arn)
  // #827: unmanage also lifts a manual force-block (else the
  // reconciler re-denies on its next tick). Warn the admin
  // explicitly when the target is force_blocked so the unblock
  // isn't a surprise.
  const isForceBlocked = user.status === 'force_blocked'
  return (
    <ModalShell onClose={onClose}>
      <h3 className="m-0 text-lg font-bold mb-2">
        Ungovern {user.display_name || user.email || user.identity_key}?
      </h3>
      <p className="text-sm text-[var(--ink-3)] m-0 mb-3">
        tg will stop maintaining this principal’s quota deny. The
        <span className="font-mono text-[12px]"> tg-BedrockQuotaDeny</span> policy
        is detached from role
        <span className="font-mono text-[12px]"> {role || '(its role)'}</span> only
        if no other governed principal still uses it — otherwise the
        policy stays attached (so the model block-list and other
        principals’ caps keep applying) and just this principal’s
        deny statement is dropped within a few minutes, on the next
        governance job run.
      </p>
      {isForceBlocked && (
        <p
          className="text-sm m-0 mb-3 p-2 rounded border border-amber-300 bg-amber-50 text-amber-900"
          role="alert"
        >
          This principal is <strong>force-blocked</strong>. Ungoverning
          will also <strong>unblock</strong> it — its Bedrock access
          will be restored (a force-block can’t persist on an
          ungoverned principal; otherwise the deny would silently
          reattach).
        </p>
      )}
      <ErrorBox>{null}</ErrorBox>
      <div className="flex gap-2 justify-end mt-3">
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button variant="destructive" disabled={busy} onClick={onConfirm}>
          {busy ? 'Ungoverning…' : 'Ungovern'}
        </Button>
      </div>
    </ModalShell>
  )
}

function LinkedAccountsSection({ email, canEdit }) {
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState(null)
  const [showAdd, setShowAdd] = useState(false)
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      setErr(null)
      const r = await api.getLinkedAccounts(email)
      setRows(Array.isArray(r) ? r : [])
    } catch (e) { setErr(e.message); setRows([]) }
  }
  useEffect(() => { load() }, [email])

  async function unlink(vendor) {
    if (!confirm(`Unlink ${vendor} account?`)) return
    setBusy(true)
    try {
      await api.deleteLinkedAccount(email, vendor)
      await load()
    } catch (e) { setErr(e.message) }
    finally { setBusy(false) }
  }

  if (rows == null) {
    return (
      <section className="mt-8">
        <h3 className="text-base font-semibold m-0 mb-3">Linked accounts</h3>
        <div className="text-sm text-[var(--ink-4)]">Loading…</div>
      </section>
    )
  }

  // Disable the Link GitHub action when a github row already
  // exists for this user — pre-#277 the button stayed live
  // and clicking it produced a "duplicate" error from the
  // API. (#277)
  const githubLinked = rows.some(r => r.vendor === 'github')

  return (
    <section className="mt-8">
      <div className="flex items-center gap-3 mb-3">
        {canEdit && (
          githubLinked ? (
            <Button
              variant="secondary"
              disabled
              title="GitHub account already linked — use the Unlink action in the row to remove it"
            >
              GitHub linked
            </Button>
          ) : (
            <Button
              variant="secondary"
              disabled={busy}
              onClick={() => setShowAdd(true)}
            >
              + Link GitHub
            </Button>
          )
        )}
        <h3 className="text-base font-semibold m-0">Linked accounts</h3>
      </div>
      <ErrorBox>{err}</ErrorBox>
      {rows.length === 0 && (
        <div className="text-sm text-[var(--ink-4)]">
          No linked accounts. {canEdit && 'Link a GitHub handle to attribute PRs.'}
        </div>
      )}
      {rows.length > 0 && (
        <Card className="p-0 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-[var(--ink-1)] text-[var(--ink-3)]">
              <tr>
                <th className="text-left px-3 py-2 font-medium">Vendor</th>
                <th className="text-left px-3 py-2 font-medium">Handle</th>
                <th className="text-right px-3 py-2 font-medium">PRs (30d)</th>
                <th className="text-left px-3 py-2 font-medium">Linked</th>
                {canEdit && <th className="px-3 py-2"></th>}
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.vendor + ':' + r.external_handle}
                    className="border-t border-[var(--border-2)]">
                  <td className="px-3 py-2 capitalize">{r.vendor}</td>
                  <td className="px-3 py-2 font-mono">{r.external_handle}</td>
                  <td className="px-3 py-2 text-right">
                    <span className={
                      'inline-block px-2 py-0.5 rounded text-xs font-mono ' +
                      (r.pr_count_30d > 0
                        ? 'bg-emerald-100 text-emerald-800'
                        : 'bg-[var(--ink-1)] text-[var(--ink-4)]')
                    }>
                      {r.pr_count_30d}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-[var(--ink-4)]">
                    {r.linked_at ? r.linked_at.slice(0, 10) : '—'}
                    {r.linked_by && r.linked_by !== 'auto' && (
                      <span className="ml-1">by {r.linked_by}</span>
                    )}
                  </td>
                  {canEdit && (
                    <td className="px-3 py-2 text-right">
                      <button
                        onClick={() => unlink(r.vendor)}
                        disabled={busy}
                        className="text-xs text-red-600 hover:underline"
                      >Unlink</button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {showAdd && (
        <LinkAccountModal
          email={email}
          onClose={() => setShowAdd(false)}
          onDone={() => { setShowAdd(false); load() }}
        />
      )}
    </section>
  )
}

function LinkAccountModal({ email, onClose, onDone }) {
  const [handle, setHandle] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  async function submit(e) {
    e.preventDefault()
    setBusy(true); setErr(null)
    try {
      await api.putLinkedAccount(email, 'github', {
        external_handle: handle.trim(),
      })
      onDone()
    } catch (e) { setErr(e.message); setBusy(false) }
  }

  return (
    <ModalShell onClose={onClose}>
      <h3 className="m-0 text-lg font-bold mb-2">
        Link GitHub account for {email}
      </h3>
      <Field label="GitHub handle">
        <Input
          value={handle}
          onChange={e => setHandle(e.target.value)}
          placeholder="octocat"
          required
          autoFocus
        />
      </Field>
      <p className="text-xs text-[var(--ink-4)]">
        PRs authored by this handle will be attributed to {email} for
        Velocity & Cost rollups.
      </p>
      <ErrorBox>{err}</ErrorBox>
      <div className="mt-3 flex justify-end gap-2">
        <Button type="button" variant="secondary" onClick={onClose}>
          Cancel
        </Button>
        <Button type="button" variant="primary"
          disabled={busy || !handle.trim()} onClick={submit}>
          {busy ? 'Linking…' : 'Link'}
        </Button>
      </div>
    </ModalShell>
  )
}

function actionsFor(status, user) {
  // #750: Force block (manual override) replaces Disable; Unblock
  // (cap-respecting) replaces Re-enable + the removed Temporary
  // unblock. An auto over-cap `blocked` user can be force-blocked
  // (hard override) — Unblock there just clears any override; the
  // cap still governs, so raising the cap is the way to let them
  // through (no temp-unblock action any more).
  switch (status) {
    case 'active':
      // Allow delete only if never logged in
      return user?.first_seen_at
        ? ['cap', 'set_team', 'force_block']
        : ['cap', 'set_team', 'force_block', 'delete_user']
    case 'blocked':
      return ['cap', 'set_team', 'force_block']
    case 'force_blocked': return ['unblock', 'set_team']
    default:              return []
  }
}

function CapModal({
  email, current, mtd, status, version,
  orgDefaultCap, onClose, onDone,
}) {
  const [cap, setCap] = useState(String(current ?? ''))
  const [useDefault, setUseDefault] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  const newCap = useDefault ? null : Number(cap)
  const willUnblock = status === 'blocked' && (newCap === null || newCap > mtd)
  // Show the actual org default value next to the checkbox
  // so the admin doesn't have to guess what they're inheriting.
  // Falls back to legacy copy if /admin/config wasn't reachable
  // (team_admin caller, or net error). (#277)
  const defaultLabel = (
    orgDefaultCap != null && orgDefaultCap > 0
      ? `Use org default (${fmtUsd(orgDefaultCap)} / month)`
      : 'Use default cap (inherit from default policy)'
  )

  async function submit(e) {
    e.preventDefault()
    setBusy(true); setErr(null)
    try {
      await api.setCap(email, newCap, version)
      onDone(willUnblock ? 'Cap updated — the user is unblocked within a few minutes, on the next governance job run' : 'Cap updated')
    } catch (e) { setErr(e.message); setBusy(false) }
  }

  return (
    <ModalShell onClose={onClose}>
      <h3 className="m-0 text-lg font-bold mb-2">Set cap for {email}</h3>
      <label className="flex items-center gap-2 my-3 text-sm">
        <input type="checkbox" checked={useDefault} onChange={e => setUseDefault(e.target.checked)} />
        <span>{defaultLabel}</span>
      </label>
      {!useDefault && (
        <Field label="Cap (USD/month)">
          <Input
            type="number"
            step="0.01"
            min="0"
            value={cap}
            onChange={e => setCap(e.target.value)}
            required
          />
        </Field>
      )}
      {willUnblock && (
        <InfoBox>
          ⚠ {email}'s MTD spend ({fmtUsd(mtd)}) is below this cap. They will be
          unblocked within ~5 minutes after you save.
        </InfoBox>
      )}
      <ErrorBox>{err}</ErrorBox>
      <div className="mt-3 flex justify-end gap-2">
        <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
        <Button type="button" variant="primary" disabled={busy} onClick={submit}>
          {busy ? 'Saving…' : 'Save'}
        </Button>
      </div>
    </ModalShell>
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

function TeamModal({ email, currentTeam, version, onClose, onDone }) {
  const [teamId, setTeamId] = useState(currentTeam || '')
  const [teams, setTeams] = useState([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    getTeams().then(d => setTeams(d.teams || [])).catch(() => {})
  }, [])

  const teamOptions = buildTeamOptions(teams)

  async function submit(e) {
    e.preventDefault()
    setBusy(true); setErr(null)
    try {
      await api.setTeam(email, teamId || null, version)
      onDone(teamId ? `Moved to team ${teamId}` : 'Removed from team')
    } catch (e) { setErr(e.message); setBusy(false) }
  }

  return (
    <ModalShell onClose={onClose}>
      <h3 className="m-0 text-lg font-bold mb-2">Set team for {email}</h3>
      <Field label="Team">
        <select
          value={teamId}
          onChange={e => setTeamId(e.target.value)}
          className="h-9 px-3 rounded border border-[var(--border-2)] text-sm bg-white"
        >
          <option value="">— no team —</option>
          {teamOptions.map(t => (
            <option key={t.team_id} value={t.team_id}>
              {'  '.repeat(t.depth)}{t.depth > 0 ? '└ ' : ''}{t.name}
            </option>
          ))}
        </select>
      </Field>
      <ErrorBox>{err}</ErrorBox>
      <div className="mt-3 flex justify-end gap-2">
        <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
        <Button type="button" variant="primary" disabled={busy} onClick={submit}>
          {busy ? 'Saving…' : 'Save'}
        </Button>
      </div>
    </ModalShell>
  )
}
