import React, { useEffect, useState } from 'react'
import { api } from '../api'

// Shared apply-status indicator for governance changes (blocked models,
// user block/unblock). Saving records intent INSTANTLY, but the
// deny_reconciler job (~5-min tick) is what actually writes the IAM
// statement — so a bare "Saved" that looks done conflates saved with
// enforced. This shows BOTH states distinctly, and survives reload
// because it's derived from server-side timestamps (the saved-at time vs
// the last deny_reconciler run), never a transient toast.
//
// Author once, reuse on every governance surface so they read identically.

// governanceApplyState(updatedAt, jobRuns) — pure, unit-tested.
//   updatedAt : ISO string when the governed config was last saved
//               (e.g. blocked_models admin_config.updated_at), or null.
//   jobRuns   : the GET /api/jobs `runs` array (each: job_name, status,
//               finished_at).
// Returns { phase, enforcedAt }:
//   'pending'  — saved, but no successful deny_reconciler run has
//                finished AT/AFTER the save → not yet enforced.
//   'enforced' — a deny_reconciler run succeeded at/after the save;
//                enforcedAt is that run's finished_at.
//   'unknown'  — nothing saved yet, or no reconciler history to compare
//                (render nothing; the page's own state covers it).
export function governanceApplyState(updatedAt, jobRuns) {
  if (!updatedAt) return { phase: 'unknown', enforcedAt: null }
  const savedMs = Date.parse(updatedAt)
  if (Number.isNaN(savedMs)) return { phase: 'unknown', enforcedAt: null }

  // The most recent SUCCEEDED deny_reconciler run that has a finish time.
  let lastFinishMs = null
  let lastFinishIso = null
  for (const r of jobRuns || []) {
    if (!r || r.job_name !== 'deny_reconciler') continue
    if (r.status !== 'succeeded') continue
    if (!r.finished_at) continue
    const ms = Date.parse(r.finished_at)
    if (Number.isNaN(ms)) continue
    if (lastFinishMs === null || ms > lastFinishMs) {
      lastFinishMs = ms
      lastFinishIso = r.finished_at
    }
  }
  if (lastFinishMs === null) {
    // Saved, but no successful reconciler run on record → pending.
    return { phase: 'pending', enforcedAt: null }
  }
  // A reconciler run that finished at/after the save applied it. (A run
  // that started before the save but finished after still re-reads the
  // current config at its own read point; for the user-facing "is my
  // save live" question, finished-at-or-after-save is the safe signal.)
  if (lastFinishMs >= savedMs) {
    return { phase: 'enforced', enforcedAt: lastFinishIso }
  }
  return { phase: 'pending', enforcedAt: null }
}

function _agoLabel(iso) {
  const ms = Date.parse(iso)
  if (Number.isNaN(ms)) return ''
  const mins = Math.max(0, Math.round((Date.now() - ms) / 60000))
  if (mins < 1) return 'just now'
  if (mins === 1) return '1 min ago'
  if (mins < 60) return `${mins} min ago`
  const hrs = Math.round(mins / 60)
  return hrs === 1 ? '1 hr ago' : `${hrs} hr ago`
}

// The per-user actions (govern/block/unblock/set-cap) now apply the
// IAM change SYNCHRONOUSLY and return an `apply` result — so those
// surfaces render the real post-apply state immediately (no "~5 min",
// no "apply now" link). applyLabel maps that result to honest copy.
// The blocked-models (Org Settings) path is still tick-driven and
// keeps the timestamp-derived behavior below (apply prop absent).
//
// apply : { state, enforced, denied?, detail } from a mutating user
//         action, or null/undefined for the tick-driven path.
export function applyLabel(apply) {
  if (!apply || !apply.state) return null
  switch (apply.state) {
    case 'enforced':
      // Distinguish a block from a govern-with-no-current-deny by the
      // `denied` intent the reconcile applied.
      return {
        tone: 'green',
        text: apply.denied
          ? '✓ Blocked · enforced'
          : '✓ Governed · enforced',
      }
    case 'allowed':
      return { tone: 'green', text: '✓ Unblocked · active' }
    case 'pending_idc':
      // IDC — tg can't attach directly; honest amber pending (mirrors
      // the Identity Center badge). Plain language, no internals.
      return {
        tone: 'amber',
        text: '◆ Governed · pending enforcement',
        note: 'Not yet active — an identity administrator must apply '
          + 'the governance policy to this user’s access.',
      }
    case 'failed':
      return {
        tone: 'red',
        text: '⚠ Not applied',
        note: apply.detail || 'The change could not be applied; the '
          + 'background reconciler will retry.',
      }
    case 'pending':
    default:
      // A denied-but-unconfirmed apply, or a timeout. If the reconcile
      // re-denied an over-cap user on unblock, say so truthfully.
      if (apply.denied) {
        return {
          tone: 'amber',
          text: '◆ Still blocked · over cap',
          note: 'Unblock cleared the manual block, but this user is '
            + 'over their cap so the limit still applies — raise the '
            + 'cap to allow.',
        }
      }
      return {
        tone: 'amber',
        text: '⏳ Applying…',
        note: 'Finishing shortly; the background reconciler will '
          + 'complete it.',
      }
  }
}

// GovernanceApplyStatus — either the immediate per-action `apply`
// result (real-time path) OR, when `apply` is absent, the tick-driven
// timestamp indicator + quiet "apply now →" link for the still-async
// blocked-models surface.
//
// Props:
//   apply     : per-action synchronous apply result, or null.
//   updatedAt : the save timestamp (ISO) of the governed config
//               (tick-driven path only).
//   className : optional wrapper classes.
//   jobsHref  : link target for the Jobs page (default '#/jobs').
export default function GovernanceApplyStatus({
  apply = null, updatedAt, className = '', jobsHref = '#/jobs',
}) {
  const [runs, setRuns] = useState([])
  useEffect(() => {
    let alive = true
    if (apply) return             // real-time path: no job-run poll
    if (typeof api.getJobRuns !== 'function') return
    api.getJobRuns()
      .then(d => { if (alive) setRuns(d?.runs || []) })
      .catch(() => {})
    return () => { alive = false }
  }, [updatedAt, apply])

  // Real-time path: render the actual post-apply state, no "~5 min",
  // no "apply now" link (it's automatic).
  const lbl = applyLabel(apply)
  if (lbl) {
    const toneCls = lbl.tone === 'green' ? 'text-[var(--green)]'
      : lbl.tone === 'red' ? 'text-[var(--red,#b42318)]'
      : 'text-amber-700'
    return (
      <div
        role="status" aria-live="polite"
        className={`text-xs flex flex-col gap-0.5 ${className}`}
      >
        <span className={toneCls}>{lbl.text}</span>
        {lbl.note && (
          <span className="text-[var(--ink-4)]">{lbl.note}</span>
        )}
      </div>
    )
  }

  const { phase, enforcedAt } = governanceApplyState(updatedAt, runs)
  if (phase === 'unknown') return null

  return (
    <div
      role="status"
      className={`text-xs flex items-center flex-wrap gap-x-2 gap-y-1 ${className}`}
    >
      {phase === 'pending' ? (
        <span className="text-[var(--ink-3)]">
          ⏳ Pending — saved, enforcing within ~5 min.
        </span>
      ) : (
        <span className="text-[var(--green)]">
          ✓ Enforced (as of {_agoLabel(enforcedAt)}).
        </span>
      )}
      <a
        href={jobsHref}
        className="text-[var(--ink-4)] underline hover:text-[var(--ink-2)]"
      >
        apply now →
      </a>
    </div>
  )
}
