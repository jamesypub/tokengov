import React, { useEffect, useState, useCallback, useRef } from 'react'
import {
  Play, RefreshCw, ChevronDown, ChevronRight, PauseCircle,
} from 'lucide-react'
import { api } from '../api'
import { useTeamScope } from '../TeamScope'

const VC_JOBS = [
  { name: 'github_sync',    label: 'GitHub sync',
    requiresVc: true,
    desc: 'Pull merged PRs from configured repos.' },
  { name: 'pr_classify',    label: 'PR classify',
    requiresVc: true,
    desc: 'Classify PRs as story / bug / task.' },
  { name: 'pr_cost_rollup', label: 'PR cost rollup',
    requiresVc: true,
    desc: 'Rebuild team_daily / team_weekly metrics.' },
]
// #761: metrics_aggregator retired (#725 — CUR/cur_spend_sync is the
// spend source now); dropped from the run-now menu. cur_spend_sync is
// the manual CUR-sync trigger.
const QUOTA_JOBS = [
  { name: 'cur_spend_sync',     label: 'CUR spend sync',
    desc: 'Pull billed spend from CUR into cur_user_spend.' },
  { name: 'quota_monitor',      label: 'Quota monitor',
    desc: 'Detect over-cap users.' },
  { name: 'deny_reconciler',    label: 'Deny reconciler',
    desc: 'Sync IAM deny statements.' },
]
// The governance-drift sweep runs daily on the scheduler and is
// detect-only, but an admin needs to re-check on demand right after
// fixing drift — otherwise the Users banner shows the stale last-sweep
// result until the next daily run. The backend already accepts this
// job via /api/jobs/run; this exposes it as a run-now entry.
const GOVERNANCE_JOBS = [
  { name: 'governance_drift_check', label: 'Governance drift check',
    desc: 'Re-scan all principals for governance inconsistencies.' },
]

// #761: scheduler-only jobs (not in the run-now menu) still need a
// human label for the run-history table. Keep this in sync with the
// scheduled set in worker/main.py / jobs.py _LIVE_JOB_NAMES.
const SCHEDULED_ONLY_LABELS = {
  quota_reset_monthly:     'Monthly reset / retention prune',
  pg_backup:               'Postgres backup',
  jira_sync:               'Jira sync',
  service_account_monitor: 'Service-account monitor',
}

// Single source of truth for human-readable job labels in the
// run history table. Falls back to the raw job_name key when
// a job isn't in the map (e.g. legacy or scheduler-only jobs).
const JOB_LABELS = {
  ...Object.fromEntries(
    [...QUOTA_JOBS, ...VC_JOBS, ...GOVERNANCE_JOBS].map(j => [j.name, j.label])),
  ...SCHEDULED_ONLY_LABELS,
}
function jobLabel(name) {
  return JOB_LABELS[name] || name || '—'
}

function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function RunStatus({ status, detail }) {
  if (!status || status === 'running') {
    return (
      <span className="inline-flex items-center gap-1.5 text-yellow-500 text-sm">
        <RefreshCw size={12} className="animate-spin" />
        running…
      </span>
    )
  }
  if (status === 'skipped') {
    return (
      <span
        className="text-[var(--ink-4)] text-sm font-medium"
        title={detail || 'GitHub not configured'}
      >
        skipped
      </span>
    )
  }
  if (status === 'ok' || status === 'succeeded') {
    return <span className="text-green-600 text-sm font-medium">OK</span>
  }
  return <span className="text-red-500 text-sm font-medium">Error</span>
}

function Changes({ blocked, unblocked, error }) {
  const b = (blocked || []).length
  const u = (unblocked || []).length
  // For failed runs with no blocked/unblocked changes, show a
  // truncated error preview in this column instead of "no
  // changes" so the user can see what went wrong without
  // expanding the row. Full text is in the expanded panel +
  // available on hover via title=.
  if (!b && !u && error) {
    return (
      <span
        className="text-red-500/90 font-mono text-xs truncate block max-w-[24rem]"
        title={error}
      >
        {error}
      </span>
    )
  }
  if (!b && !u) return <span className="text-[var(--ink-4)]">no changes</span>
  const parts = []
  if (b) parts.push(<span key="b" className="text-red-600 font-medium">{b} blocked</span>)
  if (u) parts.push(<span key="u" className="text-green-600 font-medium">{u} unblocked</span>)
  return <span className="flex gap-2">{parts}</span>
}

function noteFor(run) {
  // Returns { text, full, kind } describing what to render in
  // the Note column. Empty text → blank cell (clean success
  // runs are kept sparse on purpose).
  const isFailure = run.status === 'failed' || run.status === 'error'
  if (isFailure && run.error) {
    const full = String(run.error)
    return {
      text: full.length > 100 ? full.slice(0, 100) + '…' : full,
      full,
      kind: 'error',
    }
  }
  const b = (run.blocked || []).length
  const u = (run.unblocked || []).length
  if (b || u) {
    const parts = []
    if (b) parts.push(`blocked ${b} user${b === 1 ? '' : 's'}`)
    if (u) parts.push(`unblocked ${u} user${u === 1 ? '' : 's'}`)
    const text = parts.join(', ')
    return { text, full: text, kind: 'change' }
  }
  return { text: '', full: '', kind: 'blank' }
}

function Note({ run }) {
  const { text, full, kind } = noteFor(run)
  if (!text) return <span className="text-[var(--ink-4)]">—</span>
  if (kind === 'error') {
    return (
      <span
        className="text-red-500/90 font-mono text-xs truncate block max-w-[24rem]"
        title={full}
      >
        {text}
      </span>
    )
  }
  return (
    <span
      className="text-[var(--ink-2)] text-xs truncate block max-w-[24rem]"
      title={full}
    >
      {text}
    </span>
  )
}

function RunRow({ run, me }) {
  // Auto-open failed rows so the error is visible without an
  // extra click. (#124)
  const [open, setOpen] = useState(run.status === 'failed')
  const hasDetail = (run.blocked?.length || run.unblocked?.length || run.error || run.detail)
  const triggeredBy = run.triggered_by === me ? 'you' : (run.triggered_by || 'scheduler')
  const trigger = run.triggered_by && run.triggered_by !== 'scheduler' ? 'manual' : 'auto'

  return (
    <>
      <tr
        className={[
          'border-b border-[var(--border)]',
          hasDetail ? 'cursor-pointer hover:bg-[var(--surface-2)]' : '',
        ].join(' ')}
        onClick={() => hasDetail && setOpen(o => !o)}
      >
        <td className="p-3 text-sm whitespace-nowrap font-medium"
            title={run.job_name}>
          {jobLabel(run.job_name)}
        </td>
        <td className="p-3 text-sm whitespace-nowrap" title={run.started_at}>
          <span className="inline-flex items-center gap-1">
            {hasDetail
              ? (open ? <ChevronDown size={13} /> : <ChevronRight size={13} />)
              : <span className="inline-block w-[13px]" />
            }
            {fmtTime(run.started_at)}
          </span>
        </td>
        <td className="p-3 text-sm">{triggeredBy}</td>
        <td className="p-3 text-sm text-[var(--ink-4)]">{trigger}</td>
        <td className="p-3 text-sm"><RunStatus status={run.status} detail={run.detail} /></td>
        <td className="p-3 text-sm">
          <Changes
            blocked={run.blocked}
            unblocked={run.unblocked}
            error={run.error}
          />
        </td>
        <td className="p-3 text-sm"><Note run={run} /></td>
      </tr>
      {open && hasDetail && (
        <tr className="bg-[var(--surface)]">
          <td colSpan={7} className="px-8 py-3 text-sm">
            <div className="mb-2 text-xs text-[var(--ink-4)]">
              <span className="font-medium">Job:</span> {run.job_name}
              {run.duration_ms != null && (
                <> · <span className="font-medium">Duration:</span>{' '}
                {(run.duration_ms / 1000).toFixed(2)}s</>
              )}
            </div>
            {run.error && (
              <div className="mb-2">
                <div className="font-medium text-red-600 mb-1">Error</div>
                <pre className="text-red-600 font-mono text-xs whitespace-pre-wrap break-all bg-red-500/5 border border-red-500/20 rounded px-3 py-2">
                  {run.error}
                </pre>
              </div>
            )}
            {!!run.blocked?.length && (
              <div className="mb-1">
                <span className="font-medium text-red-600">Blocked: </span>
                {run.blocked.join(', ')}
              </div>
            )}
            {!!run.unblocked?.length && (
              <div>
                <span className="font-medium text-green-600">Unblocked: </span>
                {run.unblocked.join(', ')}
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  )
}

function fmtCountdown(ms) {
  if (ms <= 0) return 'expired'
  const total = Math.floor(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

const PAUSE_PRESETS = [
  { label: '15 min', minutes: 15 },
  { label: '30 min', minutes: 30 },
  { label: '1 hr',   minutes: 60 },
  { label: '2 hr',   minutes: 120 },
]

export default function Jobs() {
  const { persona } = useTeamScope()
  const [runs, setRuns] = useState(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState(null)
  const [toast, setToast] = useState(null)
  const [me, setMe] = useState(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [pauseUntil, setPauseUntil] = useState(null)
  const [pauseOpen, setPauseOpen] = useState(false)
  const [pausing, setPausing] = useState(false)
  const [customMinutes, setCustomMinutes] = useState('')
  const [customUnit, setCustomUnit] = useState('min')
  const [now, setNow] = useState(Date.now())
  // Read-only CUR spend-source health surfaced alongside the jobs
  // view — the same data the Org Settings diagnostics section shows
  // (cur_spend_sync feeds quota enforcement, so its freshness belongs
  // here). curHealth.status + detail come from api.curHealth();
  // source freshness + newly-seen models ride on admin_config.
  const [curHealth, setCurHealth] = useState(null)
  const [curSource, setCurSource] = useState(null)
  const [curNewModels, setCurNewModels] = useState([])
  const menuRef = useRef(null)
  const pauseRef = useRef(null)

  useEffect(() => {
    api.whoami().then(d => setMe(d.email)).catch(() => {})
    // Load the read-only CUR health surface (no new endpoint —
    // reuses the same api methods Org Settings calls).
    api.curHealth().then(d => setCurHealth(d)).catch(() => setCurHealth(null))
    api.getAdminConfig()
      .then(d => {
        setCurSource(d?.cur_source || null)
        setCurNewModels(d?.cur_new_models || [])
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    function onClick(e) {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        setMenuOpen(false)
      }
      if (pauseRef.current && !pauseRef.current.contains(e.target)) {
        setPauseOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [])

  // Tick the clock once a second so the pause-countdown
  // refreshes live and the banner clears the moment the
  // pause expires (without waiting for the next /api/jobs
  // poll).
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  const load = useCallback(async () => {
    try {
      const d = await api.getJobRuns()
      setRuns(d.runs || [])
      setPauseUntil(d.pause_until || null)
    } catch { /* silent — stale table is better than a broken page */ }
  }, [])

  useEffect(() => { load() }, [load])

  async function pause(minutes) {
    if (!minutes || minutes <= 0) return
    setPausing(true); setError(null); setToast(null)
    try {
      const r = await api.pauseJobs(minutes)
      setPauseUntil(r.pause_until || null)
      setPauseOpen(false)
      setCustomMinutes('')
      setToast(`Jobs paused for ${minutes} min`)
      setTimeout(() => setToast(null), 6000)
    } catch (e) {
      setError(e.message || 'Pause failed')
    } finally {
      setPausing(false)
    }
  }

  async function resume() {
    setPausing(true); setError(null); setToast(null)
    try {
      await api.resumeJobs()
      setPauseUntil(null)
      setToast('Jobs resumed')
      setTimeout(() => setToast(null), 6000)
    } catch (e) {
      setError(e.message || 'Resume failed')
    } finally {
      setPausing(false)
    }
  }

  async function runOne(jobName, label) {
    setRunning(true)
    setMenuOpen(false)
    setError(null)
    setToast(null)
    try {
      const result = await api.runJob(jobName)
      const errs = result.errors || []
      if (errs.length) {
        setError(
          errs.map(e => `${e.job}: ${e.error}`).join('\n')
        )
      } else {
        setToast(`${label} — done`)
        setTimeout(() => setToast(null), 6000)
      }
      await load()
    } catch (e) {
      setError(e.message || `${label} failed`)
      await load()
    } finally {
      setRunning(false)
    }
  }

  async function handleRun() {
    setRunning(true)
    setError(null)
    setToast(null)
    try {
      const result = await api.runQuotaSync()
      // The /api/jobs/run endpoint returns 200 with errors:[]
      // even when individual jobs fail (so partial results are
      // preserved). Surface those as a banner so the user
      // doesn't only see a green "Done" toast.
      const errs = result.errors || []
      if (errs.length) {
        setError(
          errs.map(e => `${e.job}: ${e.error}`).join('\n')
        )
      } else {
        const b = result.blocked?.length || 0
        const u = result.unblocked?.length || 0
        const summary = b || u
          ? [b && `${b} blocked`, u && `${u} unblocked`].filter(Boolean).join(', ')
          : 'no changes'
        setToast(`Done — ${summary}`)
        setTimeout(() => setToast(null), 6000)
      }
      await load()
    } catch (e) {
      setError(e.message || 'Sync failed')
      await load()
    } finally {
      setRunning(false)
    }
  }

  const pauseUntilMs = pauseUntil ? Date.parse(pauseUntil) : 0
  const pauseActive = pauseUntilMs > now
  const pauseRemainingMs = pauseActive ? pauseUntilMs - now : 0

  return (
    // #562: the run-history table has 7 columns (two up-to-24rem
    // truncating cells), which overflowed the old max-w-4xl page
    // and got clipped by the card's overflow-hidden — the last
    // "Note" column ran off the edge. Widen to max-w-6xl so it
    // fits at normal widths; the table card also scrolls (below)
    // as a safety net for narrow viewports.
    <div className="p-8 max-w-6xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-semibold text-[var(--ink)]">Jobs</h1>
          <p className="text-sm text-[var(--ink-4)] mt-0.5">
            Manually trigger the quota enforcement pipeline.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative" ref={pauseRef}>
            <button
              onClick={() => !pauseActive && setPauseOpen(o => !o)}
              disabled={pausing || pauseActive}
              title={pauseActive ? 'Already paused — use Resume Now' : 'Pause all scheduled jobs'}
              className={[
                'flex items-center gap-1.5 px-3 py-2 rounded-md text-sm font-medium border transition-colors',
                pauseActive
                  ? 'border-amber-300 bg-amber-50 text-amber-800 cursor-not-allowed'
                  : 'border-red-300 text-red-700 hover:bg-red-50',
              ].join(' ')}
            >
              <PauseCircle size={14} />
              {pauseActive ? 'Paused' : 'Pause All Jobs'}
            </button>
            {pauseOpen && !pauseActive && (
              <div className="absolute right-0 mt-1 w-72 bg-[var(--surface)] border border-[var(--border)] rounded-md shadow-lg z-30 p-3">
                <div className="text-[10px] uppercase tracking-wider text-[var(--ink-4)] mb-2">
                  Pause for
                </div>
                <div className="grid grid-cols-2 gap-2 mb-3">
                  {PAUSE_PRESETS.map(p => (
                    <button
                      key={p.label}
                      onClick={() => pause(p.minutes)}
                      disabled={pausing}
                      className="px-3 py-1.5 rounded border border-[var(--border)] text-sm hover:bg-[var(--surface-2)]"
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
                <div className="text-[10px] uppercase tracking-wider text-[var(--ink-4)] mb-2">
                  Custom
                </div>
                <div className="flex items-center gap-2">
                  <input
                    type="number"
                    min="1"
                    value={customMinutes}
                    onChange={e => setCustomMinutes(e.target.value)}
                    placeholder="N"
                    className="w-20 px-2 py-1.5 rounded border border-[var(--border)] text-sm"
                  />
                  <select
                    value={customUnit}
                    onChange={e => setCustomUnit(e.target.value)}
                    className="px-2 py-1.5 rounded border border-[var(--border)] text-sm"
                  >
                    <option value="min">min</option>
                    <option value="hr">hr</option>
                  </select>
                  <button
                    onClick={() => {
                      const n = Number(customMinutes)
                      if (!Number.isFinite(n) || n <= 0) return
                      const mins = customUnit === 'hr' ? n * 60 : n
                      pause(mins)
                    }}
                    disabled={pausing || !customMinutes}
                    className="ml-auto px-3 py-1.5 rounded bg-red-600 text-white text-sm font-medium disabled:opacity-50"
                  >
                    Pause for {customMinutes
                      ? `${customMinutes} ${customUnit}`
                      : '…'}
                  </button>
                </div>
              </div>
            )}
          </div>
          <div className="relative" ref={menuRef}>
            <button
              onClick={() => setMenuOpen(o => !o)}
              disabled={running}
              className={[
                'flex items-center gap-1.5 px-3 py-2 rounded-md text-sm font-medium border transition-colors',
                running
                  ? 'border-[var(--border)] text-[var(--ink-4)] cursor-not-allowed'
                  : 'border-[var(--border)] text-[var(--ink)] hover:bg-[var(--surface-2)]',
              ].join(' ')}
            >
              + Run now
              <ChevronDown size={13} />
            </button>
            {menuOpen && (
              <div className="absolute right-0 mt-1 w-72 bg-[var(--surface)] border border-[var(--border)] rounded-md shadow-lg z-30">
                <div className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wider text-[var(--ink-4)] flex items-center justify-between">
                  <span>Quota</span>
                  {pauseActive && (
                    <span className="text-[10px] normal-case font-normal px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 border border-amber-200">
                      paused
                    </span>
                  )}
                </div>
                {QUOTA_JOBS.map(j => (
                  <button
                    key={j.name}
                    onClick={() => runOne(j.name, j.label)}
                    className="block w-full text-left px-3 py-2 text-sm hover:bg-[var(--surface-2)]"
                  >
                    <div className="font-medium flex items-center justify-between gap-2">
                      <span>{j.label}</span>
                      {pauseActive && (
                        <span className="text-[9px] uppercase tracking-wider px-1 py-0.5 rounded bg-amber-100 text-amber-800 border border-amber-200">
                          paused
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-[var(--ink-4)]">{j.desc}</div>
                  </button>
                ))}
                <div className="px-3 pt-2 pb-1 mt-1 border-t border-[var(--border)] text-[10px] uppercase tracking-wider text-[var(--ink-4)]">
                  Velocity &amp; Cost
                </div>
                {VC_JOBS.map(j => (
                  <button
                    key={j.name}
                    onClick={() => runOne(j.name, j.label)}
                    className="block w-full text-left px-3 py-2 text-sm hover:bg-[var(--surface-2)]"
                  >
                    <div className="font-medium flex items-center justify-between gap-2">
                      <span>{j.label}</span>
                      {pauseActive && (
                        <span className="text-[9px] uppercase tracking-wider px-1 py-0.5 rounded bg-amber-100 text-amber-800 border border-amber-200">
                          paused
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-[var(--ink-4)]">{j.desc}</div>
                  </button>
                ))}
                <div className="px-3 pt-2 pb-1 mt-1 border-t border-[var(--border)] text-[10px] uppercase tracking-wider text-[var(--ink-4)]">
                  Governance
                </div>
                {GOVERNANCE_JOBS.map(j => (
                  <button
                    key={j.name}
                    onClick={() => runOne(j.name, j.label)}
                    className="block w-full text-left px-3 py-2 text-sm hover:bg-[var(--surface-2)]"
                  >
                    <div className="font-medium flex items-center justify-between gap-2">
                      <span>{j.label}</span>
                      {pauseActive && (
                        <span className="text-[9px] uppercase tracking-wider px-1 py-0.5 rounded bg-amber-100 text-amber-800 border border-amber-200">
                          paused
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-[var(--ink-4)]">{j.desc}</div>
                  </button>
                ))}
              </div>
            )}
          </div>
          <button
            onClick={handleRun}
            disabled={running}
            className={[
              'flex items-center gap-2 px-4 py-2 rounded-md text-sm font-medium transition-colors',
              running
                ? 'bg-[var(--accent)]/40 text-white cursor-not-allowed'
                : 'bg-[var(--accent)] text-white hover:opacity-90',
            ].join(' ')}
          >
            {running ? <RefreshCw size={14} className="animate-spin" /> : <Play size={14} />}
            {running ? 'Running…' : 'Check & enforce limits'}
          </button>
        </div>
      </div>

      {pauseActive && (
        <div className="mb-4 px-4 py-3 rounded-md bg-amber-50 border border-amber-300 text-amber-900 text-sm flex items-center justify-between gap-3">
          <span className="flex items-center gap-2">
            <PauseCircle size={16} className="text-amber-700" />
            <strong>Jobs paused</strong>
            <span className="text-amber-800">
              · resumes in {fmtCountdown(pauseRemainingMs)}
            </span>
          </span>
          <button
            onClick={resume}
            disabled={pausing}
            className="px-3 py-1 rounded bg-white border border-amber-400 text-amber-900 text-xs font-semibold hover:bg-amber-100 disabled:opacity-50"
          >
            Resume Now
          </button>
        </div>
      )}
      {toast && (
        <div className="mb-4 px-4 py-3 rounded-md bg-emerald-500/10 border border-emerald-500/30 text-emerald-700 text-sm">
          {toast}
        </div>
      )}
      {error && (
        <div className="mb-4 px-4 py-3 rounded-md bg-red-500/10 border border-red-500/30 text-red-500 text-sm whitespace-pre-line font-mono text-xs">
          {error}
        </div>
      )}

      {/* Read-only CUR spend-source health. cur_spend_sync (in the
          run-now menu) pulls from CUR, so its freshness is relevant
          right here. Display only — no controls. */}
      {(curHealth || curSource) && (
        <div className="mb-4 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-5 py-3">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-xs font-bold uppercase tracking-wider text-[var(--ink-4)]">
              CUR spend source
            </span>
            {curHealth?.status && (
              <span
                className={
                  'text-[11px] px-1.5 py-0.5 rounded font-medium ' +
                  (curHealth.status === 'healthy'
                    ? 'bg-green-50 text-green-700 border border-green-200'
                    : 'bg-amber-50 text-amber-800 border border-amber-300')
                }
                title={curHealth.detail || ''}
              >
                {curHealth.status === 'healthy'
                  ? 'healthy' : 'attention needed'}
              </span>
            )}
          </div>
          <div className="text-[13px] text-[var(--ink-4)] flex flex-wrap gap-x-6 gap-y-1">
            {curSource?.data_through && (
              <span>
                Data through{' '}
                <span className="font-mono text-[var(--ink-2)]">
                  {curSource.data_through}
                </span>
              </span>
            )}
            {curNewModels.length > 0 && (
              <span>
                Newly-seen models{' '}
                <span className="font-mono text-[var(--ink-2)]">
                  {curNewModels.length}
                </span>
              </span>
            )}
            {curHealth?.detail && (
              <span className="text-[var(--ink-4)]">{curHealth.detail}</span>
            )}
          </div>
        </div>
      )}

      <div className="rounded-lg border border-[var(--border)] bg-[var(--surface)] overflow-hidden">
        <div className="px-5 py-3 text-xs font-bold uppercase tracking-wider text-[var(--ink-4)] border-b border-[var(--border)]">
          Enforcement history
          <span className="ml-2 font-normal normal-case">(last 20 runs)</span>
        </div>
        {runs == null ? (
          <div className="px-5 py-4 text-sm text-[var(--ink-4)]">Loading…</div>
        ) : runs.length === 0 ? (
          <div className="px-5 py-4 text-sm text-[var(--ink-4)]">No runs recorded yet.</div>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-[var(--border)] bg-[var(--surface)]">
                <th className="p-3 text-left text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Job</th>
                <th className="p-3 text-left text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Time</th>
                <th className="p-3 text-left text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">By</th>
                <th className="p-3 text-left text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Trigger</th>
                <th className="p-3 text-left text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Status</th>
                <th className="p-3 text-left text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Changes</th>
                <th className="p-3 text-left text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)]">Note</th>
              </tr>
            </thead>
            <tbody>
              {runs.map(r => <RunRow key={r.pk} run={r} me={me} />)}
            </tbody>
          </table>
          </div>
        )}
      </div>

      <p className="mt-4 text-xs text-[var(--ink-4)]">
        Runs metrics-aggregator → quota-monitor → deny-reconciler in sequence.
        The scheduled pipeline continues to run every 5 minutes regardless.
      </p>
    </div>
  )
}
