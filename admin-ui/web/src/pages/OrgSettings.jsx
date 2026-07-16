import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Card } from '../ui/Card'
import { Button } from '../ui/Button'
import { Badge } from '../ui/Badge'
import { Trash2, ChevronDown, ChevronRight } from 'lucide-react'
import { denyPreviewDocument } from '../lib/denyPreview'
import GovernanceApplyStatus from '../components/GovernanceApplyStatus'
import InvocationLogsSection from '../components/InvocationLogsSection'

// #744: derive the governance notices that back the collapsible
// notification area at the top of the page. Pure (testable like
// Users.jsx's governanceState) — every input is data the page
// already loads, so no new endpoint. Returns an ordered list of
// notices; the badge count is simply notices.length.
//
//   STATIC  — always present informational notes (grant-is-outside-tg).
//   DYNAMIC — appear only when their condition holds (IDC principals
//             present, model block-list posture, CUR unhealthy,
//             newly-seen models). tone: 'info' | 'warn'.
export function buildOrgNotices({
  users = [], newModels = [], blockedModels = [], curHealth = null,
} = {}) {
  const notices = []

  // STATIC — the governance precondition, condensed from the old
  // always-on "⚠ Before tg can govern" block.
  notices.push({
    id: 'grant-outside-tg',
    kind: 'static',
    tone: 'info',
    title: 'Bedrock access is granted outside tg',
    body:
      'Granting Bedrock invoke access happens in IAM or IAM Identity ' +
      'Center — tg never grants, it governs by subtracting (a deny ' +
      'that blocks listed models and enforces per-person spend ' +
      'caps). Pin the role-session-name to the user’s email on ' +
      'assume-role: per-person quota denies key on ' +
      'aws:userid = *:<email>, and CUR per-user attribution depends ' +
      'on it. A principal appears under Users once it has invoked ' +
      'Bedrock — Govern it there to attach the deny.',
  })

  // DYNAMIC — IDC permission-set principals are surfaced but not
  // governable (folded in from the old "◆ Not governable (IDC)"
  // section). role_type mirrors Users.jsx governanceState. The
  // notice `id` stays 'idc-not-manageable' (a stable internal key;
  // renaming it would churn callers/tests for no user benefit).
  const idcCount = users.filter(u => (u.role_type || 'iam') === 'idc').length
  if (idcCount > 0) {
    notices.push({
      id: 'idc-not-manageable',
      kind: 'dynamic',
      tone: 'warn',
      title:
        `${idcCount} IDC principal${idcCount === 1 ? '' : 's'} ` +
        'not governable',
      body:
        'Principals on IAM Identity Center permission-set roles ' +
        '(AWSReservedSSO_*) are surfaced but not governable in tg — ' +
        'a deny attached directly to such a role is wiped on the next ' +
        'IDC re-provision. Govern them via the permission-set policy ' +
        'or an SCP. They show the ◆ icon on Users and are ' +
        'filterable via the IDC filter.',
    })
  }

  // DYNAMIC — model denylist posture (#746). Empty block-list is
  // the intended default (allow every model, fail-open), so this is
  // an INFO note, not a warning — it just makes the fail-open
  // posture explicit so an admin isn't surprised that new models
  // invoke freely. Once a block-list is set we don't nag.
  if ((blockedModels || []).length === 0) {
    notices.push({
      id: 'no-blocked-models',
      kind: 'dynamic',
      tone: 'info',
      title: 'All models allowed (no block-list set)',
      body:
        'No models are blocked, so every Bedrock model — including ' +
        'ones AWS ships in the future — is allowed to invoke ' +
        '(per-person spend caps still apply). Add models under ' +
        'Blocked models to deny them for all governed principals.',
    })
  }

  // DYNAMIC — CUR spend source reporting unhealthy.
  if (curHealth && curHealth.status && curHealth.status !== 'healthy') {
    notices.push({
      id: 'cur-attention',
      kind: 'dynamic',
      tone: 'warn',
      title: 'CUR spend source needs attention',
      body:
        curHealth.detail ||
        'The CUR spend source is not reporting healthy. Per-user ' +
        'spend may be stale until CUR delivery resumes.',
    })
  }

  // DYNAMIC — models first observed in CUR recently. The full list
  // still renders in its own section below; this is the badge hook.
  const nm = (newModels || []).length
  if (nm > 0) {
    notices.push({
      id: 'newly-seen-models',
      kind: 'dynamic',
      tone: 'info',
      title: `${nm} newly-seen model${nm === 1 ? '' : 's'} in CUR`,
      body:
        'Models first observed in CUR recently. Informational — ' +
        'spend for these is already billed via CUR. See the ' +
        'Newly-seen models section below.',
    })
  }

  return notices
}

// Catalog (GET /api/models/catalog) backs the blocked-models
// toggle list.

// Pure decision for the save-gated Authentication picker: given the
// selected method (`selected` = true for SAML) and what's persisted
// (`saved`), is there an unsaved change, and is it the destructive
// direction (removing SSO → needs a confirm)? Exported so it's unit
// testable without rendering the heavy OrgSettings tree.
export function authMethodSaveState(selected, saved) {
  const dirty = selected !== saved
  // Destructive = turning SSO OFF (was configured, now choosing
  // password). Enabling SSO is benign (no confirm).
  const destructive = dirty && saved === true && selected === false
  return { dirty, destructive }
}

// Build an internal Cognito Provider name (stable id, never user-facing
// — tg owns it, admins never type or read it in Settings). Cognito's
// ProviderName is 1-32 chars, no spaces; the `tg-cognito-saml` prefix
// makes it self-evidently tg-owned if it ever appears in the Cognito
// console. A short deterministic suffix (no Math.random — unavailable
// here) keeps it stable across re-renders and distinct per label; a
// same-pool collision surfaces as a Cognito dup-name save error, which
// is acceptable. Exported so it can be unit-tested as a pure function.
export function makeProviderName(label) {
  const seed = ((label || '').length * 13 + 137)
    .toString(36)
    .slice(-4)
    .padStart(4, '0')
  return `tg-cognito-saml-${seed}`.slice(0, 32)
}

const sectionLabel = 'text-[11px] font-bold uppercase tracking-wider text-[var(--ink-3)]'
const numberInput = 'h-8 w-24 px-2 rounded border text-sm text-right font-mono bg-white'

// #744: the conditional notification area. Always present (the
// static note keeps count >= 1) but collapsed to a one-line badge +
// summary to de-noise; expand to read the full notices. Replaces the
// old always-on "⚠ Before tg can govern" wall of text + the IDC
// section. a11y: a real <button aria-expanded>, region role="status"
// (non-blocking — these are notices, not a hard alert).
function NoticeArea({ notices }) {
  const [open, setOpen] = useState(false)
  if (!notices || notices.length === 0) return null
  const count = notices.length
  const hasWarn = notices.some(n => n.tone === 'warn')
  return (
    <Card className="px-5 py-4">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="w-full flex items-center gap-2 text-left"
      >
        {open
          ? <ChevronDown size={16} className="text-[var(--ink-4)] flex-shrink-0" />
          : <ChevronRight size={16} className="text-[var(--ink-4)] flex-shrink-0" />}
        <span className={sectionLabel}>Governance notices</span>
        <Badge
          variant={hasWarn ? 'warning' : 'default'}
          aria-label={`${count} governance notice${count === 1 ? '' : 's'}`}
        >
          {count}
        </Badge>
        {!open && (
          <span className="text-sm text-[var(--ink-4)] truncate">
            {notices[0].title}
            {count > 1 ? ` · +${count - 1} more` : ''}
          </span>
        )}
      </button>
      {open && (
        <div role="status" className="mt-3 flex flex-col gap-3">
          {notices.map(n => (
            <div
              key={n.id}
              className={
                'px-3 py-2 rounded border ' +
                (n.tone === 'warn'
                  ? 'bg-amber-50 border-amber-300 text-amber-900'
                  : 'bg-[var(--surface)] border-[var(--border-2)] text-[var(--ink-3)]')
              }
            >
              <div className="text-sm font-semibold mb-0.5">{n.title}</div>
              <div className="text-[13px] leading-snug">{n.body}</div>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

// Heading element paired with the existing uppercase section label.
// Keeps the `sectionLabel` styling for visual continuity but renders
// a real <h2> so the section is reachable/announceable for a11y and
// the in-page index can scroll to it.
function SectionHeading({ children }) {
  return (
    <h2 className={sectionLabel + ' m-0'}>{children}</h2>
  )
}

// Wraps a settings card in a <section> with a stable anchor id and a
// ref registered into refMap (for the scroll-spy IntersectionObserver
// + smooth-scroll on index click). Inner card content is unchanged.
function Section({ id, refMap, children }) {
  return (
    <section
      id={id}
      ref={el => { if (refMap) refMap.current[id] = el }}
      // scroll-margin keeps the heading clear of any sticky chrome
      // when an index entry scrolls the section into view.
      className="scroll-mt-6"
    >
      {children}
    </section>
  )
}

// The navigable sections, grouped by the four settings groups, in the
// order they render. `id` matches the <section> anchor; Governance
// notices is intentionally NOT listed (it's a dynamic banner, not a
// section). Spend source & newly-seen models merge into one read-only
// Diagnostics section.
// Exported so the section-order test can assert it (the render order
// and the "On this page" index both derive from this single list).
export const PAGE_GROUPS = [
  {
    group: 'Governance',
    items: [
      // Blocked models sits directly under Org default quota — both
      // are core governance controls and belong adjacent. Spend
      // estimate/alerts and Notifications (delivery config) follow.
      { id: 'sec-quota', label: 'Org default quota' },
      { id: 'sec-blocked-models', label: 'Blocked models' },
      { id: 'sec-invocation-logs', label: 'Invocation logs' },
      { id: 'sec-spend-estimate', label: 'Spend estimate' },
      { id: 'sec-spend-alerts', label: 'Spend alerts' },
      { id: 'sec-notifications', label: 'Notifications' },
      { id: 'sec-org-admins', label: 'Org admins' },
    ],
  },
  {
    group: 'Identity',
    items: [
      { id: 'sec-authentication', label: 'Authentication' },
    ],
  },
  {
    group: 'Advanced',
    items: [
      { id: 'sec-experimental', label: 'Experimental features' },
    ],
  },
]

// Sticky "On this page" index with scroll-spy highlighting. On narrow
// viewports it collapses (via responsive classes on the wrapping
// layout) to a top anchor list; the same <nav> markup stays
// keyboard-reachable either way. Clicking an entry smooth-scrolls to
// the section.
function OnThisPageNav({ activeSection, onJump }) {
  return (
    <nav
      role="navigation"
      aria-label="On this page"
      className="text-sm"
    >
      <div className={sectionLabel + ' mb-3'}>On this page</div>
      <div className="flex flex-col gap-3">
        {PAGE_GROUPS.map(g => (
          <div key={g.group}>
            <div className="text-[11px] font-semibold text-[var(--ink-3)] mb-1">
              {g.group}
            </div>
            <ul className="flex flex-col gap-0.5 list-none m-0 p-0">
              {g.items.map(it => {
                const active = activeSection === it.id
                return (
                  <li key={it.id}>
                    <a
                      href={`#${it.id}`}
                      aria-current={active ? 'true' : undefined}
                      onClick={e => { e.preventDefault(); onJump(it.id) }}
                      className={
                        'block rounded px-2 py-1 transition-colors ' +
                        (active
                          ? 'bg-[var(--accent-bg,#f5f8ff)] text-[var(--accent)] font-medium'
                          : 'text-[var(--ink-4)] hover:text-[var(--ink-2)] hover:bg-[var(--surface-2)]')
                      }
                    >
                      {it.label}
                    </a>
                  </li>
                )
              })}
            </ul>
          </div>
        ))}
      </div>
    </nav>
  )
}

// Ordered list of the section ids the scroll-spy observes (flattened
// from PAGE_GROUPS so the observer and the index stay in lock-step).
const SECTION_IDS = PAGE_GROUPS.flatMap(g => g.items.map(i => i.id))

export default function OrgSettings() {
  // #726: pricing-table state (rows/edits/savingId) retired.
  // `catalog` stays — the blocked-models section uses it.
  const [catalog, setCatalog] = useState([])
  // CUR health + newly-seen models still feed the top governance
  // notices (buildOrgNotices); the spend-source detail card moved to
  // the Diagnostics page.
  const [curHealth, setCurHealth] = useState(null)
  const [newModels, setNewModels] = useState([])
  // `ready` gates the loading guard (was `rows !== null`).
  const [ready, setReady] = useState(false)
  const [err, setErr] = useState(null)
  const [okMsg, setOkMsg] = useState(null)
  const [defaultCap, setDefaultCap] = useState(null)
  const [defaultCapInput, setDefaultCapInput] = useState('')
  const [savingDefault, setSavingDefault] = useState(false)
  const [orgAdmins, setOrgAdmins] = useState(null)
  const [allUsers, setAllUsers] = useState([])
  const [grantEmail, setGrantEmail] = useState('')
  const [granting, setGranting] = useState(false)
  // #357: when the deployment runs TG_AUTH_PROVIDER=cognito,
  // offer to also create the Cognito user (sends invite
  // email). Checked by default in that mode, hidden
  // otherwise. cognitoOk gates the whole affordance.
  const [cognitoOk, setCognitoOk] = useState(false)
  const [provisionCognito, setProvisionCognito] = useState(false)
  const [revoking, setRevoking] = useState(null)
  const [rolesErr, setRolesErr] = useState(null)
  const [revokeTarget, setRevokeTarget] = useState(null)
  // #447: runtime "Experimental features" flag — Jira
  // integration, default OFF. Read/persisted via admin_config
  // (jira_enabled); flips the V&C Jira tab + jira_* worker
  // jobs with no redeploy.
  const [jiraEnabled, setJiraEnabled] = useState(false)
  const [savingJira, setSavingJira] = useState(false)
  // #1056: runtime "Experimental features" flag — Velocity & Cost
  // page, default OFF. Same admin_config mechanism as Jira
  // (vc_enabled); hides the V&C nav item + route when off.
  const [vcEnabled, setVcEnabled] = useState(false)
  const [savingVc, setSavingVc] = useState(false)
  // Spend-estimate config: estimator strategy (average|p90|peak) +
  // enforcement mode (off|warn|enforce). Default average + off.
  const [estStrategy, setEstStrategy] = useState('average')
  const [estEnforcement, setEstEnforcement] = useState('off')
  const [savingEst, setSavingEst] = useState(false)
  // Spend-cap email alerts: warn at __% of cap (default 80) + an
  // on/off toggle for the over-cap email (default on). Delivery needs
  // an SES sender; a failed test-send surfaces the soft hint.
  const [alertWarnPct, setAlertWarnPct] = useState(80)
  const [alertWarnInput, setAlertWarnInput] = useState('80')
  const [alertExceeded, setAlertExceeded] = useState(true)
  const [savingAlerts, setSavingAlerts] = useState(false)
  const [alertTest, setAlertTest] = useState(null)
  const [testingAlert, setTestingAlert] = useState(false)
  // Notifications: SMTP transport + optional Slack/webhook. The
  // secrets (password, webhook URL) are write-only — the GET returns
  // *_configured booleans, never the values. `smtp` holds the form
  // inputs; blank password/webhook on save = keep-existing.
  const [notif, setNotif] = useState(null)        // server snapshot
  const [smtp, setSmtp] = useState({
    smtp_host: '', smtp_port: 587, smtp_username: '',
    smtp_password: '', smtp_from: '', smtp_tls: 'starttls',
  })
  const [webhookInput, setWebhookInput] = useState('')
  // Explicit opt-in to remove a stored SMTP password on save (blank
  // alone keeps it). Reset after each save.
  const [clearSmtpPw, setClearSmtpPw] = useState(false)
  const [savingNotif, setSavingNotif] = useState(false)
  const [notifMsg, setNotifMsg] = useState(null)
  const [emailTest, setEmailTest] = useState(null)
  const [webhookTest, setWebhookTest] = useState(null)
  const [testingEmail, setTestingEmail] = useState(false)
  const [testingWebhook, setTestingWebhook] = useState(false)
  const [smtpHelpOpen, setSmtpHelpOpen] = useState(false)
  const [slackHelpOpen, setSlackHelpOpen] = useState(false)
  // Runtime SSO login config (SAML IdP + editable button label),
  // applied to the live Cognito pool with no redeploy. `saml` holds the
  // GET payload (config + status + registration values); the inputs are
  // separate so an edit doesn't fight the live state.
  const [saml, setSaml] = useState(null)
  const [labelInput, setLabelInput] = useState('')
  const [methodSaml, setMethodSaml] = useState(false)
  const [metaUrlInput, setMetaUrlInput] = useState('')
  const [emailAttrInput, setEmailAttrInput] = useState('email')
  const [signoutInput, setSignoutInput] = useState(false)
  const [savingLabel, setSavingLabel] = useState(false)
  const [savingSaml, setSavingSaml] = useState(false)
  const [samlErr, setSamlErr] = useState(null)
  const [showConnection, setShowConnection] = useState(false)
  const [showRegistration, setShowRegistration] = useState(false)
  // The persisted login method (true = SAML configured), distinct from
  // the radio selection `methodSaml`. The radio is SAVE-GATED — picking
  // it changes only the selection; nothing applies until Save. The two
  // diverge ⇒ an unsaved-changes save bar shows.
  const [savedMethodSaml, setSavedMethodSaml] = useState(false)
  // The remove-SSO confirm dialog (the destructive save direction only).
  const [confirmRemoveSso, setConfirmRemoveSso] = useState(false)
  const [copied, setCopied] = useState(null)
  // #746: org-wide blocked-model list (catalog model_ids).
  // blockedSet holds the model_ids toggled in the catalog;
  // savedBlocked is the last persisted list so we can detect a
  // dirty edit. (Reverses #630's approved/allow-list.)
  const [blockedSet, setBlockedSet] = useState(null)  // Set<model_id>
  const [savedBlocked, setSavedBlocked] = useState([])  // model_id[]
  const [savingBlocked, setSavingBlocked] = useState(false)
  // When blocked_models was last saved (server admin_config.updated_at);
  // GovernanceApplyStatus compares it to the last deny_reconciler run to
  // show pending vs enforced. Survives reload (server-derived).
  const [blockedUpdatedAt, setBlockedUpdatedAt] = useState(null)
  // In-page "On this page" index: refs to each <section> drive the
  // scroll-spy IntersectionObserver; activeSection highlights the
  // matching index entry.
  const sectionRefs = useRef({})
  const [activeSection, setActiveSection] = useState(SECTION_IDS[0])

  // Smooth-scroll to a section when its index entry is clicked.
  function jumpToSection(id) {
    const el = sectionRefs.current[id] || document.getElementById(id)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
      setActiveSection(id)
    }
  }

  // Scroll-spy: observe the sections and highlight whichever is
  // closest to the top of the viewport. Re-runs after `ready` flips so
  // the section elements exist in the DOM. Cleans up on unmount.
  useEffect(() => {
    if (!ready) return
    const els = SECTION_IDS
      .map(id => sectionRefs.current[id])
      .filter(Boolean)
    if (els.length === 0) return
    const observer = new IntersectionObserver(
      entries => {
        // Pick the topmost section currently intersecting the
        // viewport (smallest boundingClientRect.top among visible).
        const visible = entries
          .filter(e => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
        if (visible.length > 0 && visible[0].target.id) {
          setActiveSection(visible[0].target.id)
        }
      },
      // A negative bottom margin biases "active" toward the section
      // whose heading has crossed the upper third of the viewport.
      { rootMargin: '0px 0px -65% 0px', threshold: 0 }
    )
    els.forEach(el => observer.observe(el))
    return () => observer.disconnect()
  }, [ready])

  // ARIA radio-group keyboard handler for the Authentication method
  // picker. Arrow keys move + select per the W3C radio pattern
  // (Down/Right → next, Up/Left → prev), wrapping at the ends. The
  // two options map to methodSaml: false = username&password,
  // true = SAML.
  function onAuthRadioKeyDown(e) {
    const k = e.key
    if (k === 'ArrowDown' || k === 'ArrowRight') {
      e.preventDefault(); setMethodSaml(true)
    } else if (k === 'ArrowUp' || k === 'ArrowLeft') {
      e.preventDefault(); setMethodSaml(false)
    }
  }

  async function loadOrgAdmins() {
    try {
      const data = await api.listRoles()
      setOrgAdmins((data.roles || []).filter(r => r.role === 'org_admin'))
    } catch (e) {
      setRolesErr(String(e))
    }
  }

  function load() {
    setErr(null)
    // #726: pricing table retired — load CUR health for the
    // visibility card instead. CUR source/freshness + new-models
    // ride on admin_config (cur_source) written by the installer
    // (720c); both render only when present.
    api.curHealth()
      .then(d => setCurHealth(d))
      .catch(() => setCurHealth(null))
    api.listModelCatalog()
      .then(d => setCatalog(d.models || []))
      .catch(() => setCatalog([]))
    api.getAdminConfig()
      .then(d => {
        const cap = d?.org_default_quota_usd ?? 0
        setDefaultCap(cap)
        setDefaultCapInput(String(cap))
        setJiraEnabled(!!d?.jira_enabled)
        setVcEnabled(!!d?.vc_enabled)
        setEstStrategy(d?.spend_estimate_strategy || 'average')
        setEstEnforcement(d?.spend_estimate_enforcement || 'off')
        // New-models ride on admin_config (written by the installer);
        // feeds the governance notices. The full cur_source detail now
        // renders on the Diagnostics page.
        setNewModels(d?.cur_new_models || [])
        setReady(true)
      })
      .catch(() => setReady(true))
    api.listUsers({})
      .then(d => setAllUsers(d.users || []))
      .catch(() => {})
    // #357: discover whether Cognito provisioning is on.
    // authProviders() is the public /auth/providers endpoint;
    // cognito_provisioning is true only when
    // TG_AUTH_PROVIDER=cognito. Default the checkbox to
    // checked in that mode.
    api.authProviders()
      .then(d => {
        const on = !!d?.cognito_provisioning
        setCognitoOk(on)
        setProvisionCognito(on)
      })
      .catch(() => { setCognitoOk(false) })
    // #746: load the blocked-model list (catalog model_ids).
    api.getBlockedModels()
      .then(d => {
        const ids = d?.blocked_models || []
        setSavedBlocked(ids)
        setBlockedSet(new Set(ids))
        setBlockedUpdatedAt(d?.updated_at || null)
      })
      .catch(() => {
        setSavedBlocked([])
        setBlockedSet(new Set())
      })
    api.getSpendAlerts()
      .then(d => {
        const pct = d?.warn_pct ?? 80
        setAlertWarnPct(pct)
        setAlertWarnInput(String(pct))
        setAlertExceeded(d?.exceeded !== false)
      })
      .catch(() => {})
    api.getNotifications()
      .then(d => {
        setNotif(d || {})
        setSmtp({
          smtp_host: d?.smtp_host || '',
          smtp_port: d?.smtp_port || 587,
          smtp_username: d?.smtp_username || '',
          smtp_password: '',
          smtp_from: d?.smtp_from || '',
          smtp_tls: d?.smtp_tls || 'starttls',
        })
        setWebhookInput('')
      })
      .catch(() => setNotif({}))
    loadSaml()
    loadOrgAdmins()
  }
  useEffect(load, [])

  // Load the runtime SSO config + seed the edit inputs.
  function loadSaml() {
    api.getSamlSettings()
      .then(d => {
        setSaml(d)
        setLabelInput(d?.sso_button_label || '')
        setMethodSaml(!!d?.configured)
        setSavedMethodSaml(!!d?.configured)
        setMetaUrlInput(d?.metadata_url || '')
        setEmailAttrInput(d?.email_attribute || 'email')
        setSignoutInput(!!d?.idp_signout)
      })
      .catch(() => setSaml(null))
  }

  function toggleBlocked(modelId) {
    setBlockedSet(prev => {
      const next = new Set(prev || [])
      if (next.has(modelId)) next.delete(modelId)
      else next.add(modelId)
      return next
    })
  }

  async function saveBlocked() {
    // #746: persist the toggled set as catalog model_ids verbatim,
    // preserving any saved id not in the catalog (so a hand-added
    // block entry isn't dropped on save). The reconciler reduces
    // each id to a region/profile-agnostic token.
    const ids = [...(blockedSet || [])]
    const known = new Set(ids)
    const preserved = savedBlocked.filter(id => !known.has(id))
    const out = [...ids, ...preserved]
    setSavingBlocked(true); setErr(null); setOkMsg(null)
    try {
      const res = await api.setBlockedModels(out)
      const saved = res?.blocked_models || out
      setSavedBlocked(saved)
      setBlockedSet(new Set(saved))
      // A fresh save is pending until the next deny_reconciler run; stamp
      // the save time so GovernanceApplyStatus shows "pending" until a
      // reconciler run finishes after it. (The server admin_config
      // updated_at is the durable value re-read on the next page load.)
      setBlockedUpdatedAt(res?.updated_at || new Date().toISOString())
      setOkMsg(`Blocked models saved (${saved.length}).`)
    } catch (e) {
      setErr(String(e))
    } finally {
      setSavingBlocked(false)
    }
  }

  async function handleGrant() {
    if (!grantEmail) { setRolesErr('Select a user'); return }
    setGranting(true); setRolesErr(null); setOkMsg(null)
    // #357: only send the flag when the affordance is live;
    // a stray flag is a server-side no-op but no point.
    const doInvite = cognitoOk && provisionCognito
    try {
      const res = await api.grantRole({
        email: grantEmail,
        role: 'org_admin',
        ...(doInvite ? { provision_cognito: true } : {}),
      })
      if (res?.cognito_provisioned) {
        setOkMsg(
          `Both email and Cognito user created for ` +
          `${grantEmail}. They'll receive an invitation ` +
          `email shortly.`)
      }
      setGrantEmail('')
      await loadOrgAdmins()
    } catch (e) {
      setRolesErr(String(e))
    } finally {
      setGranting(false)
    }
  }

  async function handleRevoke(email) {
    setRevoking(email); setRolesErr(null)
    try {
      await api.revokeRole(email, null, 'org_admin')
      setRevokeTarget(null)
      await loadOrgAdmins()
    } catch (e) {
      setRolesErr(String(e))
    } finally {
      setRevoking(null)
    }
  }

  async function saveDefaultCap() {
    const n = parseFloat(defaultCapInput)
    if (!Number.isFinite(n) || n < 0) {
      setErr('Default cap must be a non-negative number')
      return
    }
    setSavingDefault(true); setErr(null); setOkMsg(null)
    try {
      await api.setAdminConfig({ org_default_quota_usd: n })
      setDefaultCap(n)
      setOkMsg(`Org default quota saved: $${n.toFixed(2)}/month`)
    } catch (e) {
      setErr(String(e))
    } finally {
      setSavingDefault(false)
    }
  }

  async function toggleJira(next) {
    // #447: persist the runtime Jira flag. Optimistic flip with
    // rollback on error so the checkbox reflects the saved value.
    setSavingJira(true); setErr(null); setOkMsg(null)
    setJiraEnabled(next)
    try {
      const res = await api.setAdminConfig({ jira_enabled: next })
      setJiraEnabled(!!res?.jira_enabled)
      setOkMsg(
        `Jira integration ${next ? 'enabled' : 'disabled'}.`)
    } catch (e) {
      setJiraEnabled(!next)
      setErr(String(e))
    } finally {
      setSavingJira(false)
    }
  }

  async function toggleVc(next) {
    // #1056: persist the runtime Velocity & Cost flag (same
    // optimistic-flip-with-rollback shape as toggleJira).
    setSavingVc(true); setErr(null); setOkMsg(null)
    setVcEnabled(next)
    try {
      const res = await api.setAdminConfig({ vc_enabled: next })
      setVcEnabled(!!res?.vc_enabled)
      setOkMsg(
        `Velocity & Cost ${next ? 'enabled' : 'disabled'}.`)
    } catch (e) {
      setVcEnabled(!next)
      setErr(String(e))
    } finally {
      setSavingVc(false)
    }
  }

  // SSO login config handlers.
  async function saveLabel() {
    // Label-only change → no Cognito call (server skips it when no
    // connection fields are present).
    setSavingLabel(true); setSamlErr(null); setOkMsg(null)
    try {
      const res = await api.setSamlSettings(
        { sso_button_label: labelInput })
      setSaml(res)
      setLabelInput(res?.sso_button_label || '')
      setOkMsg('Login button label saved.')
    } catch (e) {
      setSamlErr(String(e))
    } finally {
      setSavingLabel(false)
    }
  }

  async function saveSaml() {
    setSavingSaml(true); setSamlErr(null); setOkMsg(null)
    try {
      // Provider name is internal + tg-owned — never an admin input.
      // Always auto-generate it here; the admin never sees or sets it
      // (the generated value is readable in Diagnostics for ops).
      const providerName = makeProviderName(labelInput)
      const body = {
        provider_name: providerName,
        email_attribute: emailAttrInput.trim() || 'email',
        idp_signout: signoutInput,
        sso_button_label: labelInput,
      }
      if (metaUrlInput.trim()) body.metadata_url = metaUrlInput.trim()
      const res = await api.setSamlSettings(body)
      setSaml(res)
      setSavedMethodSaml(!!res?.configured)
      setMethodSaml(!!res?.configured)
      setOkMsg('SSO connection saved and applied to Cognito.')
    } catch (e) {
      // The server surfaces a bad metadata URL etc. as the Cognito
      // reason (400), not a 500 — show it verbatim.
      setSamlErr(String(e))
    } finally {
      setSavingSaml(false)
    }
  }

  async function disableSaml() {
    setSavingSaml(true); setSamlErr(null); setOkMsg(null)
    try {
      const res = await api.deleteSamlSettings()
      setSaml(res)
      setMethodSaml(false)
      setSavedMethodSaml(false)
      setConfirmRemoveSso(false)
      setMetaUrlInput('')
      setOkMsg('Reverted to username & password login. The org-admin '
        + 'recovery login stays available.')
    } catch (e) {
      setSamlErr(String(e))
    } finally {
      setSavingSaml(false)
    }
  }

  // The radio selection differs from what's persisted → an explicit
  // Save is required (the picker is NOT an instant toggle).
  const { dirty: methodDirty, destructive: methodDestructive } =
    authMethodSaveState(methodSaml, savedMethodSaml)

  // Discard the unsaved method change — snap the selection back to the
  // persisted state (and the inputs that go with it).
  function discardMethodChange() {
    setMethodSaml(savedMethodSaml)
    setConfirmRemoveSso(false)
    setSamlErr(null)
  }

  // Save the method change. Switching ON (→ SAML) is benign: it just
  // reveals the connection form (the actual apply is "Save & apply
  // connection", which validates the IdP). Switching OFF (→ password)
  // is destructive (removes the live IdP) → confirm first.
  function saveMethodChange() {
    if (!methodDirty) return
    if (methodDestructive) {
      // → password: destructive, name the consequence + reassure.
      setConfirmRemoveSso(true)
      return
    }
    // → SAML enable: nothing to persist yet; the connection form below
    // does the apply. Just guide the admin there.
    setShowConnection(true)
    setSamlErr(null)
    setOkMsg('Fill in the IdP connection below, then '
      + '“Save & apply connection”.')
  }

  function copyValue(key, value) {
    if (!value) return
    try {
      navigator.clipboard?.writeText(value)
      setCopied(key)
      setTimeout(() => setCopied(null), 1500)
    } catch { /* clipboard unavailable — no-op */ }
  }

  async function saveEstStrategy(next) {
    setSavingEst(true); setErr(null); setOkMsg(null)
    const prev = estStrategy
    setEstStrategy(next)
    try {
      const res = await api.setAdminConfig(
        { spend_estimate_strategy: next })
      setEstStrategy(res?.spend_estimate_strategy || next)
      setOkMsg('Spend-estimate strategy saved.')
    } catch (e) {
      setEstStrategy(prev)
      setErr(String(e))
    } finally {
      setSavingEst(false)
    }
  }

  async function saveEstEnforcement(next) {
    setSavingEst(true); setErr(null); setOkMsg(null)
    const prev = estEnforcement
    setEstEnforcement(next)
    try {
      const res = await api.setAdminConfig(
        { spend_estimate_enforcement: next })
      setEstEnforcement(res?.spend_estimate_enforcement || next)
      setOkMsg('Spend-estimate enforcement saved.')
    } catch (e) {
      setEstEnforcement(prev)
      setErr(String(e))
    } finally {
      setSavingEst(false)
    }
  }

  async function saveSpendAlerts() {
    const n = parseInt(alertWarnInput, 10)
    if (!Number.isInteger(n) || n < 1 || n > 100) {
      setErr('Warn % must be a whole number between 1 and 100')
      return
    }
    setSavingAlerts(true); setErr(null); setOkMsg(null)
    try {
      const res = await api.setSpendAlerts({
        warn_pct: n, exceeded: alertExceeded,
      })
      const pct = res?.warn_pct ?? n
      setAlertWarnPct(pct)
      setAlertWarnInput(String(pct))
      setAlertExceeded(res?.exceeded !== false)
      setOkMsg('Spend alerts saved.')
    } catch (e) {
      setErr(String(e))
    } finally {
      setSavingAlerts(false)
    }
  }

  async function toggleAlertExceeded(next) {
    // Optimistic flip with rollback (same shape as toggleJira),
    // persisting alongside the current warn %.
    setSavingAlerts(true); setErr(null); setOkMsg(null)
    setAlertExceeded(next)
    try {
      const res = await api.setSpendAlerts({ exceeded: next })
      setAlertExceeded(res?.exceeded !== false)
      setOkMsg(
        `Over-cap email ${next ? 'enabled' : 'disabled'}.`)
    } catch (e) {
      setAlertExceeded(!next)
      setErr(String(e))
    } finally {
      setSavingAlerts(false)
    }
  }

  async function sendTestAlert() {
    // Doubles as the "is SES configured?" probe — the endpoint
    // returns {sent:false, reason} (200) when no sender is set, so
    // we render a soft hint rather than an error.
    setTestingAlert(true); setAlertTest(null)
    try {
      const res = await api.testAlert()
      setAlertTest(res)
    } catch (e) {
      setAlertTest({ sent: false, reason: String(e) })
    } finally {
      setTestingAlert(false)
    }
  }

  // --- Notifications (SMTP + webhook) ---
  async function saveNotifications() {
    setSavingNotif(true); setNotifMsg(null)
    try {
      const body = {
        smtp_host: smtp.smtp_host,
        smtp_port: smtp.smtp_port,
        smtp_username: smtp.smtp_username,
        smtp_from: smtp.smtp_from,
        smtp_tls: smtp.smtp_tls,
      }
      // Blank password/webhook = keep-existing (don't clobber a secret).
      // An explicit clear removes the stored password (wins over blank).
      if (clearSmtpPw) body.clear_smtp_password = true
      else if (smtp.smtp_password) body.smtp_password = smtp.smtp_password
      if (webhookInput) body.alert_webhook_url = webhookInput
      const res = await api.setNotifications(body)
      setNotif(res || {})
      setSmtp(s => ({ ...s, smtp_password: '' }))
      setClearSmtpPw(false)
      setWebhookInput('')
      setNotifMsg({ ok: true, text: 'Notification settings saved.' })
    } catch (e) {
      setNotifMsg({ ok: false, text: String(e) })
    } finally {
      setSavingNotif(false)
    }
  }

  async function sendTestEmail() {
    setTestingEmail(true); setEmailTest(null)
    try {
      setEmailTest(await api.testAlert('email'))
    } catch (e) {
      setEmailTest({ sent: false, reason: String(e) })
    } finally {
      setTestingEmail(false)
    }
  }

  async function sendTestWebhook() {
    setTestingWebhook(true); setWebhookTest(null)
    try {
      setWebhookTest(await api.testAlert('webhook'))
    } catch (e) {
      setWebhookTest({ sent: false, reason: String(e) })
    } finally {
      setTestingWebhook(false)
    }
  }

  // #726: the model-pricing edit/save/delete/add/reset handlers
  // are retired with the pricing table.

  if (err && !ready) return (
    <div className="p-8 text-[var(--red)]">Failed to load settings: {err}</div>
  )
  if (!ready) return (
    <div className="p-8 text-[var(--ink-4)]">Loading…</div>
  )

  // #744: notices for the conditional area at the top. All derived
  // from data the page already loaded — no new endpoint.
  const notices = buildOrgNotices({
    users: allUsers, newModels, blockedModels: savedBlocked, curHealth,
  })

  return (
    <div className="p-8 tg-settings">
      <div className="border-b border-[var(--border)] pb-3 mb-5">
        <h1 className="m-0 text-2xl font-semibold">Org Settings</h1>
        <p className="m-0 mt-1 text-sm text-[var(--ink-4)]">
          Default monthly budget, blocked models, and admins
        </p>
      </div>

      {err && <div className="text-sm text-[var(--red)] mb-3">{err}</div>}
      {okMsg && <div className="text-sm text-[var(--green)] mb-3">{okMsg}</div>}

      {/* #744: conditional governance notices (collapsed behind a
          count badge). Folds in the old precondition block + the IDC
          "not governable" notice. Stays a full-width dynamic banner
          above the two-column layout — not an index entry. */}
      <div className="mb-5">
        <NoticeArea notices={notices} />
      </div>

      {/* Two-column layout: a sticky "On this page" index on the left
          + the grouped settings sections on the right. The index
          collapses to a top anchor list on narrow viewports (flex
          stacks; the index loses its sticky positioning) while staying
          keyboard-reachable. The settings are split into four labeled
          groups — Governance, Identity, Diagnostics & help, Advanced. */}
      <div className="flex flex-col lg:flex-row gap-6 items-start">
        <aside className="w-full lg:w-56 lg:flex-shrink-0 lg:sticky lg:top-6">
          <OnThisPageNav
            activeSection={activeSection}
            onJump={jumpToSection}
          />
        </aside>

        <div className="flex-1 min-w-0 flex flex-col gap-5 w-full">
          {/* ───────── Governance ───────── */}
          <div className="text-sm font-semibold text-[var(--ink-2)] pb-1 border-b border-[var(--border)]">
            Governance
          </div>

          {/* Org default quota — FIRST in Governance: the everyday
              default an admin reaches for. */}
          <Section id="sec-quota" refMap={sectionRefs}>
            <Card className="px-5 py-4">
              <div className="flex items-center gap-2">
                <SectionHeading>Org Default Quota</SectionHeading>
              </div>
              <div className="text-sm text-[var(--ink-4)] mt-1 mb-3">
                Monthly Bedrock spend cap (USD) applied to any user
                without an explicit per-user policy. New users are
                covered automatically — no admin action required.
              </div>
              <div className="flex items-center gap-3 flex-wrap">
                <label className="text-sm font-semibold text-[var(--ink-3)]">Cap (USD)</label>
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  value={defaultCapInput}
                  onChange={e => setDefaultCapInput(e.target.value)}
                  disabled={defaultCap === null}
                  className={numberInput + ' border-[var(--border-2)]'}
                />
                <Button
                  variant={parseFloat(defaultCapInput) !== defaultCap ? 'primary' : 'secondary'}
                  size="sm"
                  onClick={saveDefaultCap}
                  disabled={savingDefault || defaultCap === null
                    || parseFloat(defaultCapInput) === defaultCap}
                >
                  {savingDefault ? 'Saving…' : 'Save'}
                </Button>
                {defaultCap !== null && (
                  <span className="text-xs text-[var(--ink-4)]">
                    Currently: ${Number(defaultCap).toFixed(2)}/month
                  </span>
                )}
              </div>
            </Card>
          </Section>

          {/* Blocked models (global) with a live denylist deny
              preview — a primary editable governance control. */}
          <Section id="sec-blocked-models" refMap={sectionRefs}>
            <Card className="px-5 py-4">
        <div className="flex items-center gap-2">
          <SectionHeading>Blocked models (global)</SectionHeading>
        </div>
        <div className="text-sm text-[var(--ink-4)] mt-1 mb-3">
          The models governed principals are <em>not</em> allowed to
          invoke. tg compiles your selection into a
          <span className="font-mono text-[12px]"> Deny</span> on
          those models'
          (<span className="font-mono text-[12px]">Resource</span>)
          ARNs, attached as part of <span className="font-mono text-[12px]">tg-BedrockQuotaDeny</span>.
          <strong> Allow-by-default (fail-open):</strong> leave empty
          and every model — including ones AWS ships later — is
          allowed (per-person quota still applies). Region- and
          profile-agnostic: blocking a model blocks it under
          <span className="font-mono text-[12px]"> us.*</span> /
          <span className="font-mono text-[12px]"> global.*</span> and
          every region.
        </div>
        {blockedSet === null ? (
          <div className="text-sm text-[var(--ink-4)]">Loading…</div>
        ) : (
          <>
            <div className="flex flex-col gap-1.5 mb-4">
              {(() => {
                // Catalog model_ids + any saved id not in the
                // catalog (so a hand-added block entry still shows).
                const catIds = catalog.map(c => c.model_id)
                // Discovered-only entries (observed in CUR, not in the
                // static catalog) get a "seen in usage" tag.
                const discoveredIds = new Set(
                  catalog.filter(c => c.discovered).map(c => c.model_id))
                const extraIds = savedBlocked
                  .filter(id => !catIds.includes(id))
                const allIds = [...catIds, ...extraIds]
                if (allIds.length === 0) {
                  return (
                    <div className="text-sm text-[var(--ink-4)] italic">
                      No models in the catalog yet.
                    </div>
                  )
                }
                return allIds.map(id => (
                  <label key={id} className="flex items-center gap-2 text-sm cursor-pointer">
                    <input
                      type="checkbox"
                      checked={blockedSet.has(id)}
                      onChange={() => toggleBlocked(id)}
                    />
                    <span className="font-mono text-[12px]">{id}</span>
                    {discoveredIds.has(id) && (
                      <span className="text-[10px] text-[var(--ink-4)] italic">
                        · seen in usage
                      </span>
                    )}
                  </label>
                ))
              })()}
            </div>
            <div className="flex items-center gap-3 mb-4">
              <Button
                variant="primary"
                size="sm"
                onClick={saveBlocked}
                disabled={savingBlocked}
              >
                {savingBlocked ? 'Saving…' : 'Save blocked models'}
              </Button>
              <span className="text-xs text-[var(--ink-4)]">
                {blockedSet.size} blocked · {savedBlocked.length} saved
              </span>
            </div>
            {/* Persistent pending→enforced status + a quiet "apply now →"
                link to the Jobs page (deny_reconciler Run-now). Saving
                records intent; the reconciler (~5 min) enforces it. */}
            <GovernanceApplyStatus
              updatedAt={blockedUpdatedAt}
              className="mb-4"
            />
            {/* Live denylist deny preview. */}
            <div>
              <div className="text-[11px] font-bold uppercase tracking-wider text-[var(--ink-3)] mb-1">
                Deny preview (compiles into tg-BedrockQuotaDeny)
              </div>
              {(() => {
                const doc = denyPreviewDocument([...blockedSet])
                if (!doc) {
                  return (
                    <div className="text-sm text-[var(--ink-4)] italic px-3 py-2 rounded bg-[var(--surface)] border border-[var(--border-2)]">
                      No models blocked → no model-restriction deny is
                      emitted; every model is allowed (fail-open).
                      Per-person quota deny still applies.
                    </div>
                  )
                }
                return (
                  <pre className="text-[11px] font-mono bg-[var(--surface)] border border-[var(--border-2)] rounded p-3 overflow-x-auto whitespace-pre">
{JSON.stringify(doc, null, 2)}
                  </pre>
                )
              })()}
              <div className="text-xs text-[var(--ink-4)] mt-1">
                Note the <span className="font-mono">Converse</span> /
                <span className="font-mono"> ConverseStream</span> actions
                — they aren't auto-blocked by an
                <span className="font-mono"> InvokeModel</span> deny, so
                tg lists them explicitly. Preview is illustrative; the
                reconciler is the source of truth.
              </div>
            </div>
          </>
        )}
            </Card>
          </Section>

          {/* Invocation logs — the analytics capture stream (separate
              from CUR spend). Self-contained: loads/saves its own region
              catalog via the settings API. */}
          <Section id="sec-invocation-logs" refMap={sectionRefs}>
            <InvocationLogsSection />
          </Section>

          {/* Spend estimate — billed CUR + estimated unbilled projection.
              The admin picks the estimator strategy + how the estimate is
              used. Copy explains each choice by its dollar OUTCOME, not by
              the statistic name. */}
          <Section id="sec-spend-estimate" refMap={sectionRefs}>
            <Card className="px-5 py-4">
              <SectionHeading>Spend estimate</SectionHeading>
              <div className="text-sm text-[var(--ink-4)] mt-1 mb-4">
                AWS bills Bedrock spend with up to ~24h lag, so the spend you
                see is hours behind real usage. tg adds an{' '}
                <strong>estimated</strong> figure for that unbilled window —{' '}
                <span className="font-mono">billed + estimated = projected</span>{' '}
                — projected from each user’s own recent billed rate. The
                estimate <strong>shrinks to $0 as AWS delivers the bill</strong>;
                billed always stays the authoritative number.
              </div>

              {/* Strategy — 3 options, each with a worked dollar example over
                  a 12h unbilled window at an illustrative $12/hr rate. The
                  real per-user numbers show on Users / Activity. */}
              <div className="mb-4">
                <div className="text-sm font-medium text-[var(--ink-2)] mb-2">
                  Estimator strategy
                </div>
                <div className="flex flex-col gap-2">
                  {[
                    ['average', 'Average', 'Recommended',
                      'Realistic for steady usage — the mean of recent billed '
                      + 'hours. e.g. $12/hr × 12h ≈ +$144.'],
                    ['p90', 'High (p90)', 'Conservative',
                      'A heavy-hour rate (90th percentile) — bigger safety '
                      + 'margin without chasing one freak hour. e.g. '
                      + '$21/hr × 12h ≈ +$252.'],
                    ['peak', 'Peak', 'Worst-case',
                      'The single busiest billed hour — over-projects; for a '
                      + 'hard ceiling only. e.g. $23/hr × 12h ≈ +$276.'],
                  ].map(([val, label, badge, help]) => (
                    <label
                      key={val}
                      className={
                        'flex items-start gap-3 text-sm cursor-pointer rounded-lg '
                        + 'border px-3 py-2 '
                        + (estStrategy === val
                          ? 'border-[var(--accent)] bg-[var(--accent-bg,#f5f8ff)]'
                          : 'border-[var(--line)]')
                      }
                    >
                      <input
                        type="radio"
                        name="est-strategy"
                        className="mt-0.5"
                        checked={estStrategy === val}
                        disabled={savingEst}
                        onChange={() => saveEstStrategy(val)}
                      />
                      <span>
                        <span className="font-medium text-[var(--ink-2)]">
                          {label}
                        </span>
                        <span className="ml-2 text-[11px] px-1.5 py-0.5 rounded bg-[var(--surface-2,#f0f0f0)] text-[var(--ink-4)]">
                          {badge}
                        </span>
                        <span className="block text-[var(--ink-4)]">{help}</span>
                      </span>
                    </label>
                  ))}
                </div>
              </div>

              {/* Enforcement — off / warn / enforce. Color encodes risk;
                  enforce reveals the false-block warning. */}
              <div>
                <div className="text-sm font-medium text-[var(--ink-2)] mb-2">
                  How the estimate is used
                </div>
                <div className="flex flex-col gap-2">
                  {[
                    ['off', 'Off', 'safe',
                      'Display only. Caps enforce on billed spend exactly as '
                      + 'today; the estimate is shown but never blocks.'],
                    ['warn', 'Warn', 'safe',
                      'Shows an “approaching/over cap” warning when the '
                      + 'projection crosses the cap — still no block from the '
                      + 'estimate.'],
                    ['enforce', 'Enforce', 'risk',
                      'A projection can trigger a real block before AWS bills. '
                      + 'Blocks faster, but may block on an over-estimate — '
                      + 'pair with a conservative strategy.'],
                  ].map(([val, label, tone, help]) => (
                    <label
                      key={val}
                      className={
                        'flex items-start gap-3 text-sm cursor-pointer rounded-lg '
                        + 'border px-3 py-2 '
                        + (estEnforcement === val
                          ? (tone === 'risk'
                            ? 'border-[var(--red)] bg-red-50'
                            : 'border-[var(--accent)] bg-[var(--accent-bg,#f5f8ff)]')
                          : 'border-[var(--line)]')
                      }
                    >
                      <input
                        type="radio"
                        name="est-enforcement"
                        className="mt-0.5"
                        checked={estEnforcement === val}
                        disabled={savingEst}
                        onChange={() => saveEstEnforcement(val)}
                      />
                      <span>
                        <span className="font-medium text-[var(--ink-2)]">
                          {label}
                        </span>
                        <span
                          className={
                            'ml-2 text-[11px] px-1.5 py-0.5 rounded '
                            + (tone === 'risk'
                              ? 'bg-red-100 text-[var(--red)]'
                              : 'bg-green-100 text-green-800')
                          }
                        >
                          {tone === 'risk'
                            ? 'may block on an over-estimate' : 'safe'}
                        </span>
                        <span className="block text-[var(--ink-4)]">{help}</span>
                      </span>
                    </label>
                  ))}
                </div>
                {estEnforcement === 'enforce' && (
                  <div className="mt-2 text-sm text-[var(--red)] bg-red-50 border border-red-200 rounded px-3 py-2">
                    Enforce uses a <strong>projection</strong> to drive a real
                    IAM deny — a user can be blocked before AWS bills the
                    spend. Validate against your real per-user data first, and
                    prefer a conservative strategy (High/p90) to limit
                    false-blocks. Every estimate-driven block is audit-logged.
                  </div>
                )}
                <div className="mt-2 text-[11px] text-[var(--ink-4)]">
                  Refreshes every ~5 min; the per-user rate updates hourly;
                  users with thin billing history fall back to Average.
                </div>
              </div>
            </Card>
          </Section>

          {/* Spend alerts — email the user + their admin when they
              approach (warn %) or exceed their cap. Delivery needs an
              SES sender; the test-send doubles as the "is SES set?"
              probe and surfaces a soft hint when it isn't. */}
          <Section id="sec-spend-alerts" refMap={sectionRefs}>
            <Card className="px-5 py-4">
              <SectionHeading>Spend alerts</SectionHeading>
              <div className="text-sm text-[var(--ink-4)] mt-1 mb-4">
                Email a user (and their team admin) when they approach
                or exceed their monthly Bedrock spend cap. Each email
                fires once per transition — a warn when spend first
                crosses the threshold, and an over-cap notice when
                access is paused. Delivery uses your configured alert
                sender.
              </div>

              {/* Warn threshold — number input, % of cap. */}
              <div className="flex items-center gap-3 flex-wrap mb-4">
                <label className="text-sm font-semibold text-[var(--ink-3)]">
                  Warn at
                </label>
                <input
                  type="number"
                  step="1"
                  min="1"
                  max="100"
                  value={alertWarnInput}
                  onChange={e => setAlertWarnInput(e.target.value)}
                  className={numberInput + ' border-[var(--border-2)] w-20'}
                />
                <span className="text-sm text-[var(--ink-3)]">% of cap</span>
                <Button
                  variant={
                    parseInt(alertWarnInput, 10) !== alertWarnPct
                      ? 'primary' : 'secondary'}
                  size="sm"
                  onClick={saveSpendAlerts}
                  disabled={savingAlerts
                    || parseInt(alertWarnInput, 10) === alertWarnPct}
                >
                  {savingAlerts ? 'Saving…' : 'Save'}
                </Button>
                <span className="text-xs text-[var(--ink-4)]">
                  Currently: {alertWarnPct}%
                </span>
              </div>

              {/* Over-cap email toggle. */}
              <label className="flex items-start gap-3 text-sm text-[var(--ink-3)] cursor-pointer mb-4">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={alertExceeded}
                  disabled={savingAlerts}
                  onChange={e => toggleAlertExceeded(e.target.checked)}
                />
                <span>
                  <span className="font-medium text-[var(--ink-2)]">
                    Email when a user exceeds their cap
                  </span>
                  <span className="block text-[var(--ink-4)]">
                    Sends a one-time notice to the user and their admin
                    when access is paused at the cap.
                  </span>
                </span>
              </label>

              {/* SES test-send / configured hint. */}
              <div className="flex items-center gap-3 flex-wrap">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={sendTestAlert}
                  disabled={testingAlert}
                >
                  {testingAlert ? 'Sending…' : 'Send test alert'}
                </Button>
                {alertTest && (
                  alertTest.sent ? (
                    <span className="text-xs text-[var(--green)]">
                      Test alert sent to {alertTest.to}.
                    </span>
                  ) : (
                    <span className="text-xs text-amber-700">
                      Set an alert sender to enable
                      {alertTest.reason ? ` — ${alertTest.reason}` : ''}.
                    </span>
                  )
                )}
              </div>
            </Card>
          </Section>

          {/* Notifications: generic SMTP transport + optional
              Slack/webhook announcement. Two self-explaining cards so
              a first-time admin configures either without leaving the
              page. Secrets (password, webhook URL) are write-only. */}
          <Section id="sec-notifications" refMap={sectionRefs}>
            <Card className="px-5 py-4">
              <SectionHeading>Notifications</SectionHeading>
              <div className="text-sm text-[var(--ink-4)] mt-1 mb-4">
                Spend-cap alerts can be <strong>emailed</strong> to the
                affected user and their admins, and/or
                <strong> announced</strong> to a team chat channel.
                Email reaches the individual; a webhook posts to a
                shared channel. Configure either, both, or neither —
                they're independent.
              </div>

              {/* Email (SMTP) card */}
              <div className="rounded border border-[var(--border-2)] p-4 mb-4">
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-semibold text-[var(--ink-2)]">
                    Email (SMTP)
                  </span>
                  {notif && (notif.smtp_host
                    ? <Badge variant="success">Configured</Badge>
                    : <Badge>Not set up</Badge>)}
                </div>
                <div className="text-xs text-[var(--ink-4)] mb-3">
                  {notif && !notif.smtp_host
                    ? 'Email not set up — the user and their admins '
                      + "won't be emailed on a spend event."
                    : 'Sends per-recipient email over any SMTP relay.'}
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <label className="text-sm">
                    <span className="block text-[var(--ink-3)] mb-1">Host</span>
                    <input
                      type="text"
                      value={smtp.smtp_host}
                      onChange={e => setSmtp(s => ({ ...s, smtp_host: e.target.value }))}
                      placeholder="smtp.example.com"
                      className="h-8 w-full px-2 rounded border text-sm bg-white"
                    />
                  </label>
                  <label className="text-sm">
                    <span className="block text-[var(--ink-3)] mb-1">Port</span>
                    <input
                      type="number"
                      value={smtp.smtp_port}
                      onChange={e => setSmtp(s => ({ ...s, smtp_port: e.target.value }))}
                      className="h-8 w-full px-2 rounded border text-sm bg-white"
                    />
                  </label>
                  <label className="text-sm">
                    <span className="block text-[var(--ink-3)] mb-1">Username</span>
                    <input
                      type="text"
                      value={smtp.smtp_username}
                      onChange={e => setSmtp(s => ({ ...s, smtp_username: e.target.value }))}
                      className="h-8 w-full px-2 rounded border text-sm bg-white"
                    />
                  </label>
                  <label className="text-sm">
                    <span className="block text-[var(--ink-3)] mb-1">
                      Password
                      {notif?.smtp_password_configured && (
                        <span className="text-[var(--ink-4)] font-normal">
                          {' '}— ••••• configured (blank = keep)
                        </span>
                      )}
                    </span>
                    <input
                      type="password"
                      value={clearSmtpPw ? '' : smtp.smtp_password}
                      disabled={clearSmtpPw}
                      onChange={e => setSmtp(s => ({ ...s, smtp_password: e.target.value }))}
                      placeholder={
                        clearSmtpPw
                          ? 'will be cleared on save'
                          : (notif?.smtp_password_configured ? '••••• configured' : '')
                      }
                      className="h-8 w-full px-2 rounded border text-sm bg-white disabled:bg-[var(--surface-2)] disabled:text-[var(--ink-4)]"
                    />
                    {notif?.smtp_password_configured && (
                      <label className="mt-1 flex items-center gap-1.5 text-xs text-[var(--ink-4)] font-normal">
                        <input
                          type="checkbox"
                          checked={clearSmtpPw}
                          onChange={e => setClearSmtpPw(e.target.checked)}
                        />
                        Clear stored credential on save
                      </label>
                    )}
                  </label>
                  <label className="text-sm">
                    <span className="block text-[var(--ink-3)] mb-1">From address</span>
                    <input
                      type="text"
                      value={smtp.smtp_from}
                      onChange={e => setSmtp(s => ({ ...s, smtp_from: e.target.value }))}
                      placeholder="alerts@example.com"
                      className="h-8 w-full px-2 rounded border text-sm bg-white"
                    />
                  </label>
                  <label className="text-sm">
                    <span className="block text-[var(--ink-3)] mb-1">TLS mode</span>
                    <select
                      value={smtp.smtp_tls}
                      onChange={e => setSmtp(s => ({ ...s, smtp_tls: e.target.value }))}
                      className="h-8 w-full px-2 rounded border text-sm bg-white"
                    >
                      <option value="none">None</option>
                      <option value="starttls">STARTTLS</option>
                      <option value="tls">TLS</option>
                    </select>
                  </label>
                </div>

                {/* Provider examples — ready-to-paste host/port. */}
                <button
                  type="button"
                  className="mt-3 text-xs text-[var(--ink-3)] underline"
                  onClick={() => setSmtpHelpOpen(o => !o)}
                >
                  {smtpHelpOpen ? 'Hide provider examples' : 'Using a provider?'}
                </button>
                {smtpHelpOpen && (
                  <ul className="mt-2 text-xs text-[var(--ink-4)] list-disc pl-5 space-y-1">
                    <li><strong>Gmail / Workspace:</strong> <span className="font-mono">smtp.gmail.com</span>:587, app password.</li>
                    <li><strong>Office 365:</strong> <span className="font-mono">smtp.office365.com</span>:587.</li>
                    <li><strong>SendGrid:</strong> <span className="font-mono">smtp.sendgrid.net</span>:587, username <span className="font-mono">apikey</span>.</li>
                    <li><strong>Amazon SES:</strong> <span className="font-mono">email-smtp.&lt;region&gt;.amazonaws.com</span>:587, IAM SMTP creds — SES users paste the SES SMTP endpoint here; no SDK or identity setup in this app.</li>
                  </ul>
                )}

                <div className="flex items-center gap-3 flex-wrap mt-3">
                  <Button variant="secondary" size="sm"
                          onClick={sendTestEmail} disabled={testingEmail}>
                    {testingEmail ? 'Sending…' : 'Send test email'}
                  </Button>
                  {emailTest && (
                    <span
                      aria-live="polite"
                      className={'text-xs ' + (emailTest.sent
                        ? 'text-[var(--green)]' : 'text-amber-700')}
                    >
                      {emailTest.sent
                        ? `Test email sent to ${emailTest.to}.`
                        : `Not sent${emailTest.reason ? ` — ${emailTest.reason}` : ''}.`}
                    </span>
                  )}
                </div>
              </div>

              {/* Slack / Webhook card */}
              <div className="rounded border border-[var(--border-2)] p-4 mb-4">
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-semibold text-[var(--ink-2)]">
                    Slack / Webhook
                  </span>
                  {notif && (notif.webhook_configured
                    ? <Badge variant="success">Configured</Badge>
                    : <Badge>Not set up</Badge>)}
                </div>
                <div className="text-xs text-[var(--ink-4)] mb-3">
                  Posts a one-line announcement of each spend event to a
                  chat channel. This goes to a <strong>channel, not a
                  person</strong> — to notify the affected user
                  directly, use Email above.
                </div>

                <label className="text-sm block">
                  <span className="block text-[var(--ink-3)] mb-1">
                    Webhook URL
                    {notif?.webhook_configured && (
                      <span className="text-[var(--ink-4)] font-normal">
                        {' '}— ••••• configured (blank = keep)
                      </span>
                    )}
                  </span>
                  <input
                    type="password"
                    value={webhookInput}
                    onChange={e => setWebhookInput(e.target.value)}
                    placeholder={notif?.webhook_configured
                      ? '••••• configured'
                      : 'https://hooks.slack.com/services/…'}
                    className="h-8 w-full px-2 rounded border text-sm bg-white"
                  />
                </label>

                <button
                  type="button"
                  className="mt-3 text-xs text-[var(--ink-3)] underline"
                  onClick={() => setSlackHelpOpen(o => !o)}
                >
                  {slackHelpOpen ? 'Hide steps' : 'How to get a Slack webhook URL'}
                </button>
                {slackHelpOpen && (
                  <ol className="mt-2 text-xs text-[var(--ink-4)] list-decimal pl-5 space-y-1">
                    <li>In Slack: create or pick an app → enable <strong>Incoming Webhooks</strong>.</li>
                    <li><strong>Add New Webhook to Workspace</strong> → choose the channel to post to.</li>
                    <li>Copy the <span className="font-mono">https://hooks.slack.com/services/…</span> URL → paste here.</li>
                    <li className="text-[var(--ink-4)]">The channel is fixed when you create the URL — you pick it in Slack, not here. Keep this URL secret.</li>
                  </ol>
                )}

                <div className="flex items-center gap-3 flex-wrap mt-3">
                  <Button variant="secondary" size="sm"
                          onClick={sendTestWebhook} disabled={testingWebhook}>
                    {testingWebhook ? 'Sending…' : 'Send test message'}
                  </Button>
                  {webhookTest && (
                    <span
                      aria-live="polite"
                      className={'text-xs ' + (webhookTest.sent
                        ? 'text-[var(--green)]' : 'text-amber-700')}
                    >
                      {webhookTest.sent
                        ? 'Test message delivered.'
                        : `Not sent${webhookTest.reason ? ` — ${webhookTest.reason}` : ''}.`}
                    </span>
                  )}
                </div>
              </div>

              <div className="flex items-center gap-3 flex-wrap">
                <Button variant="primary" size="sm"
                        onClick={saveNotifications} disabled={savingNotif}>
                  {savingNotif ? 'Saving…' : 'Save notification settings'}
                </Button>
                {notifMsg && (
                  <span
                    aria-live="polite"
                    className={'text-xs ' + (notifMsg.ok
                      ? 'text-[var(--green)]' : 'text-amber-700')}
                  >
                    {notifMsg.text}
                  </span>
                )}
              </div>
            </Card>
          </Section>

          {/* Org Admins — a primary governance control. */}
          <Section id="sec-org-admins" refMap={sectionRefs}>
            <Card className="px-5 py-4">
        <div className="flex items-center gap-2">
          <SectionHeading>Org Admins</SectionHeading>
        </div>
        <div className="text-sm text-[var(--ink-4)] mt-1 mb-4">
          Users with full org-wide access. At least one org admin must remain.
          Team admin roles are managed from the Teams page → Members.
        </div>

        {rolesErr && (
          <div className="mb-3 px-3 py-2 rounded bg-red-50 border border-red-200 text-sm text-[var(--red)]">
            {rolesErr}
          </div>
        )}
        {okMsg && (
          <div className="mb-3 px-3 py-2 rounded bg-green-50 border border-green-200 text-sm text-[var(--green)]">
            {okMsg}
          </div>
        )}

        {/* #357: Cognito provider but only the bootstrap
            admin so far — guide the operator to add more via
            email + invite. */}
        {cognitoOk && (orgAdmins || []).length <= 1 && (
          <div className="mb-4 px-3 py-2 rounded bg-[var(--accent-tint,#eef2ff)] border border-[var(--border-2)] text-sm text-[var(--ink-3)]">
            Add admins via email + Cognito invite — they
            receive a link to set their password and land on
            TG.
          </div>
        )}

        {/* Grant form — pick from existing users */}
        <div className="flex flex-wrap gap-2 items-end mb-5">
          <div className="flex flex-col gap-1">
            <label className="text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">
              User
            </label>
            <select
              value={grantEmail}
              onChange={e => setGrantEmail(e.target.value)}
              className="h-8 px-2 rounded border border-[var(--border-2)] text-sm bg-white min-w-[260px]"
            >
              <option value="">— select existing user —</option>
              {allUsers
                .filter(u => !(orgAdmins || []).some(a => a.email === u.email))
                .sort((a, b) => a.email.localeCompare(b.email))
                .map(u => (
                  <option key={u.email} value={u.email}>{u.email}</option>
                ))
              }
            </select>
          </div>
          <Button
            variant="primary"
            size="sm"
            onClick={handleGrant}
            disabled={granting || !grantEmail}
          >
            {granting ? 'Granting…' : 'Grant org_admin'}
          </Button>
        </div>

        {/* #357: Cognito invite checkbox — rendered only when
            TG_AUTH_PROVIDER=cognito (cognitoOk). Hidden on
            Okta / desktop installs, so the desktop binary —
            which never sets cognito_provisioning — never
            shows it. Checked by default in cognito mode. */}
        {cognitoOk && (
          <label className="flex items-center gap-2 -mt-3 mb-5 text-sm text-[var(--ink-3)] cursor-pointer">
            <input
              type="checkbox"
              checked={provisionCognito}
              onChange={e => setProvisionCognito(e.target.checked)}
            />
            Also create Cognito user (sends invite email)
          </label>
        )}

        {/* Org admins table */}
        {orgAdmins === null ? (
          <div className="text-sm text-[var(--ink-4)]">Loading…</div>
        ) : orgAdmins.length === 0 ? (
          <div className="text-sm text-[var(--ink-4)]">No org admins found.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="bg-[var(--surface)] border-b-2 border-[var(--border)]">
                  {['Email', 'Granted By', 'Granted At', ''].map(h => (
                    <th key={h} className="px-3 py-2 text-left text-[11px] font-bold uppercase tracking-wider text-[var(--ink-3)] whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {orgAdmins.map((r, i) => {
                  const isLast = orgAdmins.length === 1
                  return (
                    <tr key={r.email}
                      className={'border-b border-[var(--border)] ' + (i % 2 === 1 ? 'bg-[var(--surface-2)]' : '')}>
                      <td className="px-3 py-2 font-semibold">{r.email}</td>
                      <td className="px-3 py-2 text-[var(--ink-4)]">{r.granted_by || '—'}</td>
                      <td className="px-3 py-2 text-[var(--ink-4)] whitespace-nowrap">
                        {r.granted_at ? r.granted_at.slice(0, 10) : '—'}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <button
                          onClick={() => !isLast && setRevokeTarget(r)}
                          disabled={revoking === r.email || isLast}
                          title={isLast ? 'Cannot remove the last org admin' : 'Revoke org_admin'}
                          className="text-[var(--ink-4)] hover:text-[var(--red)] transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                        >
                          <Trash2 size={14} />
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
            </Card>
          </Section>

          {/* ───────── Identity ───────── */}
          <div className="text-sm font-semibold text-[var(--ink-2)] pb-1 border-b border-[var(--border)] mt-2">
            Identity
          </div>

          {/* Authentication — runtime SSO login config. Pick the
              sign-in method; for Company login (SAML) set the IdP
              connection + a custom button label, applied to the live
              Cognito pool with no redeploy. */}
          <Section id="sec-authentication" refMap={sectionRefs}>
            <Card className="px-5 py-4">
        <SectionHeading>Authentication</SectionHeading>
        <div className="text-sm text-[var(--ink-4)] mt-1 mb-4">
          How people sign in to the admin UI. Changes apply to the
          live login with no redeploy. A username &amp; password
          (Cognito) admin path always stays available as a recovery
          login, even when company SSO is on.
        </div>

        {samlErr && (
          <div className="mb-3 text-sm text-[var(--red)]">
            {samlErr}
          </div>
        )}

        {/* Login-method picker as an ARIA radio group. Two options
            back the boolean methodSaml (false = username&password,
            true = SAML). Roving tabindex + arrow-key navigation per
            the W3C radio pattern; selection is shown by a dot
            indicator + accent styling (not color alone). */}
        <fieldset
          role="radiogroup"
          aria-label="Login method"
          className="flex gap-3 mb-4 border-0 p-0 m-0"
          onKeyDown={onAuthRadioKeyDown}
        >
          <div
            role="radio"
            aria-checked={!methodSaml}
            tabIndex={!methodSaml ? 0 : -1}
            onClick={() => setMethodSaml(false)}
            className={
              'flex-1 text-left rounded-lg border px-4 py-3 cursor-pointer ' +
              'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] ' +
              (!methodSaml
                ? 'border-[var(--accent)] bg-[var(--accent-bg,#f5f8ff)]'
                : 'border-[var(--line)]')
            }
          >
            <div className="flex items-center gap-2 font-medium text-[var(--ink-2)]">
              {/* Selection indicator — a filled dot, so the choice is
                  not signalled by color/border alone. */}
              <span
                aria-hidden="true"
                className={
                  'inline-flex h-3.5 w-3.5 items-center justify-center ' +
                  'rounded-full border ' +
                  (!methodSaml
                    ? 'border-[var(--accent)]'
                    : 'border-[var(--ink-4)]')
                }
              >
                {!methodSaml && (
                  <span className="h-1.5 w-1.5 rounded-full bg-[var(--accent)]" />
                )}
              </span>
              Username &amp; password
            </div>
            <div className="text-xs text-[var(--ink-4)] mt-0.5">
              Cognito user pool. Admins are invited by email.
            </div>
          </div>
          <div
            role="radio"
            aria-checked={methodSaml}
            tabIndex={methodSaml ? 0 : -1}
            onClick={() => setMethodSaml(true)}
            className={
              'flex-1 text-left rounded-lg border px-4 py-3 cursor-pointer ' +
              'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] ' +
              (methodSaml
                ? 'border-[var(--accent)] bg-[var(--accent-bg,#f5f8ff)]'
                : 'border-[var(--line)]')
            }
          >
            <div className="flex items-center gap-2 font-medium text-[var(--ink-2)]">
              <span
                aria-hidden="true"
                className={
                  'inline-flex h-3.5 w-3.5 items-center justify-center ' +
                  'rounded-full border ' +
                  (methodSaml
                    ? 'border-[var(--accent)]'
                    : 'border-[var(--ink-4)]')
                }
              >
                {methodSaml && (
                  <span className="h-1.5 w-1.5 rounded-full bg-[var(--accent)]" />
                )}
              </span>
              Company login (SAML)
            </div>
            <div className="text-xs text-[var(--ink-4)] mt-0.5">
              Federate to your IdP — e.g. Okta via AWS IAM Identity
              Center. No new IdP app needed.
            </div>
          </div>
        </fieldset>

        {/* Unsaved-changes save bar — appears only when the radio
            selection differs from what's persisted. The picker is
            save-gated: the live login changes on Save, not on click. */}
        {methodDirty && (
          <div
            role="region"
            aria-label="Unsaved authentication changes"
            className="mb-4 flex items-center justify-between gap-3 rounded-lg border border-[var(--accent)] bg-[var(--accent-bg,#f5f8ff)] px-3 py-2"
          >
            <span className="text-sm text-[var(--ink-2)]">
              Unsaved change —{' '}
              {methodSaml
                ? 'switching to Company login (SAML).'
                : 'switching to username & password (removes SSO).'}
            </span>
            <div className="flex gap-2">
              <Button
                variant="secondary" size="sm"
                disabled={savingSaml}
                onClick={discardMethodChange}
              >Discard</Button>
              <Button
                size="sm"
                disabled={savingSaml}
                onClick={saveMethodChange}
              >Save</Button>
            </div>
          </div>
        )}

        {/* Remove-SSO confirm — only the destructive direction. Names
            the consequence + reassures no lockout; outcome-named
            buttons, no default-Yes. */}
        {confirmRemoveSso && (
          <div
            role="alertdialog"
            aria-label="Remove company SSO?"
            className="mb-4 rounded-lg border border-[var(--red)] bg-white px-4 py-3"
          >
            <div className="text-sm font-medium text-[var(--ink-2)] mb-1">
              Remove company SSO?
            </div>
            <div className="text-sm text-[var(--ink-4)] mb-3">
              This removes the live IdP connection
              {saml?.provider_name && (
                <> (<span className="font-mono">{saml.provider_name}</span>)</>
              )}{' '}
              and switches everyone to username &amp; password sign-in.
              You won’t be locked out: the org-admin recovery login
              (username &amp; password) stays available.
            </div>
            <div className="flex gap-2">
              <Button
                variant="secondary" size="sm"
                disabled={savingSaml}
                onClick={() => setConfirmRemoveSso(false)}
              >Keep SSO</Button>
              <Button
                variant="destructive" size="sm"
                disabled={savingSaml}
                onClick={disableSaml}
              >
                {savingSaml ? 'Removing…' : 'Remove SSO'}
              </Button>
            </div>
          </div>
        )}

        {methodSaml && (
          <div className="space-y-4">
            {/* 1. Button label (drives the live login button) — the
                ONLY user-facing SSO string. */}
            <div>
              <label className="block text-sm font-medium text-[var(--ink-2)] mb-1">
                Login button label
                <span className="ml-2 text-[10px] font-normal uppercase tracking-wider text-[var(--ink-4)]">
                  shown to users
                </span>
              </label>
              <div className="flex gap-2 items-center">
                <input
                  className="h-8 px-2 rounded border text-sm bg-white flex-1 max-w-xs"
                  value={labelInput}
                  placeholder="Login with Your SSO"
                  onChange={e => setLabelInput(e.target.value)}
                />
                <Button
                  variant="secondary" size="sm"
                  disabled={savingLabel}
                  onClick={saveLabel}
                >
                  {savingLabel ? 'Saving…' : 'Save label'}
                </Button>
              </div>
              <div className="text-xs text-[var(--ink-4)] mt-1">
                Shown on the federated sign-in button. Defaults to
                “Login with Your SSO”. Saving the label alone does not
                touch your IdP connection.
              </div>
            </div>

            {/* 2. Connection (collapsible). */}
            <div className="rounded-lg border border-[var(--line)]">
              <button
                type="button"
                className="w-full flex items-center gap-2 px-3 py-2 text-sm font-medium text-[var(--ink-2)]"
                onClick={() => setShowConnection(v => !v)}
              >
                {showConnection
                  ? <ChevronDown size={16} />
                  : <ChevronRight size={16} />}
                IdP connection
              </button>
              {showConnection && (
                <div className="px-3 pb-3 space-y-3">
                  <div>
                    <label className="block text-xs text-[var(--ink-4)] mb-1">
                      IdP metadata URL (preferred — auto-refreshes)
                    </label>
                    <input
                      className="h-8 px-2 rounded border text-sm bg-white w-full"
                      value={metaUrlInput}
                      placeholder="https://your-idp/saml/metadata"
                      onChange={e => setMetaUrlInput(e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-[var(--ink-4)] mb-1">
                      Email attribute (mapped to the verified email
                      tg matches users on)
                    </label>
                    <input
                      className="h-8 px-2 rounded border text-sm bg-white w-full max-w-xs font-mono"
                      value={emailAttrInput}
                      onChange={e => setEmailAttrInput(e.target.value)}
                    />
                  </div>
                  <label className="flex items-center gap-2 text-sm text-[var(--ink-3)] cursor-pointer">
                    <input
                      type="checkbox"
                      checked={signoutInput}
                      onChange={e => setSignoutInput(e.target.checked)}
                    />
                    Enable IdP single-logout
                  </label>

                  {/* The Cognito Provider name is an INTERNAL routing id
                      (never shown to end users) and is effectively
                      unrenamable (changing it recreates the IdP), so it's
                      not an admin-facing setting: tg auto-generates and
                      owns it on save. Ops can read the generated value
                      read-only in Diagnostics for console troubleshooting. */}

                  <div>
                    <Button
                      size="sm"
                      disabled={savingSaml || !metaUrlInput.trim()}
                      onClick={saveSaml}
                    >
                      {savingSaml ? 'Applying…'
                        : 'Save & apply connection'}
                    </Button>
                  </div>
                  {saml?.status && (
                    <div className="text-xs text-[var(--ink-4)]">
                      {saml.status.error
                        ? <span className="text-[var(--red)]">
                            Last apply error: {saml.status.error}
                          </span>
                        : (saml.configured
                            ? `IdP present on pool: ${
                                saml.status.present ? 'yes' : 'no'}`
                            : 'No IdP configured yet.')}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* 3. Values to register in IAM Identity Center. */}
            <div className="rounded-lg border border-[var(--line)]">
              <button
                type="button"
                className="w-full flex items-center gap-2 px-3 py-2 text-sm font-medium text-[var(--ink-2)]"
                onClick={() => setShowRegistration(v => !v)}
              >
                {showRegistration
                  ? <ChevronDown size={16} />
                  : <ChevronRight size={16} />}
                Values to register in your IdP / IAM Identity Center
              </button>
              {showRegistration && (
                <div className="px-3 pb-3 space-y-2 text-sm">
                  <div className="text-xs text-[var(--ink-4)] mb-1">
                    The SAML service provider is Cognito (tg is an
                    OIDC client of it). Register these Cognito values
                    on the IdP side — Cognito publishes no SP metadata,
                    so enter them manually.
                  </div>
                  {[
                    ['ACS URL (assertion consumer)',
                      saml?.registration?.acs_url],
                    ['SP entity ID',
                      saml?.registration?.sp_entity_id],
                    ['Required attribute',
                      saml?.registration?.email_attribute],
                  ].map(([k, v]) => (
                    <div key={k} className="flex items-center gap-2">
                      <div className="text-xs text-[var(--ink-4)] w-44 shrink-0">
                        {k}
                      </div>
                      <code className="flex-1 text-xs bg-[var(--surface-2,#f6f6f6)] px-2 py-1 rounded break-all">
                        {v || '—'}
                      </code>
                      <Button
                        variant="secondary" size="sm"
                        disabled={!v}
                        onClick={() => copyValue(k, v)}
                      >
                        {copied === k ? 'Copied' : 'Copy'}
                      </Button>
                    </div>
                  ))}
                  {saml?.registration?.acs_url_error && (
                    <div className="text-xs text-[var(--red)]">
                      {saml.registration.acs_url_error}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
            </Card>
          </Section>

          {/* ───────── Advanced ───────── */}
          <div className="text-sm font-semibold text-[var(--ink-2)] pb-1 border-b border-[var(--border)] mt-2">
            Advanced
          </div>

          {/* Experimental features — runtime feature flags stored in
              admin_config, flipped with no redeploy. */}
          <Section id="sec-experimental" refMap={sectionRefs}>
            <Card className="px-5 py-4">
        <SectionHeading>Experimental features</SectionHeading>
        <div className="text-sm text-[var(--ink-4)] mt-1 mb-4">
          Opt-in features still in development. Off by default;
          enable to preview. May change or be removed.
        </div>
        {/* #1056: Velocity & Cost toggle, ABOVE Jira. */}
        <label className="flex items-start gap-3 text-sm text-[var(--ink-3)] cursor-pointer mb-3">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={vcEnabled}
            disabled={savingVc}
            onChange={e => toggleVc(e.target.checked)}
          />
          <span>
            <span className="font-medium text-[var(--ink-2)]">
              Velocity &amp; Cost
            </span>
            <span className="block text-[var(--ink-4)]">
              Show the Velocity &amp; Cost page (DORA-style velocity +
              cost views). In development — leave off unless you’re
              previewing it.
            </span>
          </span>
        </label>
        <label className="flex items-start gap-3 text-sm text-[var(--ink-3)] cursor-pointer">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={jiraEnabled}
            disabled={savingJira}
            onChange={e => toggleJira(e.target.checked)}
          />
          <span>
            <span className="font-medium text-[var(--ink-2)]">
              Jira integration
            </span>
            <span className="block text-[var(--ink-4)]">
              Show the Velocity &amp; Cost “Jira” tab and run the
              Jira sync jobs. Data work is deferred to a later
              release — leave off unless you’re previewing it.
            </span>
          </span>
        </label>
            </Card>
          </Section>
        </div>
      </div>

      {/* #726: delete-pricing modal retired with the table. */}

      {/* Revoke confirm modal */}
      {revokeTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-xl shadow-xl p-6 max-w-sm w-full mx-4">
            <div className="text-base font-semibold mb-2">Revoke org_admin?</div>
            <p className="text-sm text-[var(--ink-4)] mb-4">
              Remove org admin access from <strong>{revokeTarget.email}</strong>?
              They will lose all administrative access immediately.
            </p>
            <div className="flex gap-2 justify-end">
              <Button variant="secondary" size="sm" onClick={() => setRevokeTarget(null)}>Cancel</Button>
              <Button
                variant="destructive"
                size="sm"
                disabled={revoking === revokeTarget.email}
                onClick={() => handleRevoke(revokeTarget.email)}
              >
                {revoking === revokeTarget.email ? 'Revoking…' : 'Revoke'}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
