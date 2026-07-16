import React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

// Governance changes (blocked models, user block/unblock) are
// SAVED instantly but ENFORCED by the deny_reconciler (~5-min tick).
// The status must show both states distinctly and survive reload — it's
// derived from server timestamps (config saved-at vs last reconciler
// run), never a transient toast. These pin the pure state machine + the
// rendered pending/enforced/quiet-link behavior.

const getJobRuns = vi.fn()
vi.mock('../api', () => ({
  api: { getJobRuns: (...a) => getJobRuns(...a) },
}))

import GovernanceApplyStatus, {
  governanceApplyState, applyLabel,
} from './GovernanceApplyStatus'

beforeEach(() => { getJobRuns.mockReset() })

const run = (over = {}) => ({
  job_name: 'deny_reconciler', status: 'succeeded',
  finished_at: '2026-06-21T12:00:00Z', ...over,
})

describe('governanceApplyState', () => {
  it('unknown when nothing saved yet', () => {
    expect(governanceApplyState(null, []).phase).toBe('unknown')
    expect(governanceApplyState('bogus', []).phase).toBe('unknown')
  })

  it('pending when no successful reconciler run on record', () => {
    expect(governanceApplyState('2026-06-21T11:00:00Z', []).phase)
      .toBe('pending')
  })

  it('pending when the last run finished BEFORE the save', () => {
    const st = governanceApplyState('2026-06-21T12:30:00Z', [
      run({ finished_at: '2026-06-21T12:00:00Z' }),
    ])
    expect(st.phase).toBe('pending')
  })

  it('enforced when a run finished AT/AFTER the save', () => {
    const st = governanceApplyState('2026-06-21T12:00:00Z', [
      run({ finished_at: '2026-06-21T12:05:00Z' }),
    ])
    expect(st.phase).toBe('enforced')
    expect(st.enforcedAt).toBe('2026-06-21T12:05:00Z')
  })

  it('ignores non-deny_reconciler and failed/unfinished runs', () => {
    const st = governanceApplyState('2026-06-21T12:00:00Z', [
      run({ job_name: 'cur_spend_sync', finished_at: '2026-06-21T13:00:00Z' }),
      run({ status: 'failed', finished_at: '2026-06-21T13:00:00Z' }),
      run({ finished_at: null }),
    ])
    expect(st.phase).toBe('pending')
  })

  it('uses the LATEST successful run finish', () => {
    const st = governanceApplyState('2026-06-21T12:00:00Z', [
      run({ finished_at: '2026-06-21T11:00:00Z' }),
      run({ finished_at: '2026-06-21T12:30:00Z' }),
    ])
    expect(st.phase).toBe('enforced')
    expect(st.enforcedAt).toBe('2026-06-21T12:30:00Z')
  })
})

describe('GovernanceApplyStatus render', () => {
  it('renders nothing when nothing is saved (unknown)', async () => {
    getJobRuns.mockResolvedValue({ runs: [] })
    const { container } = render(<GovernanceApplyStatus updatedAt={null} />)
    await new Promise(r => setTimeout(r, 20))
    expect(container.textContent).toBe('')
  })

  it('shows Pending + a quiet apply-now link before enforcement', async () => {
    getJobRuns.mockResolvedValue({ runs: [] })
    render(<GovernanceApplyStatus updatedAt={'2026-06-21T12:00:00Z'} />)
    expect(await screen.findByText(/Pending/)).toBeTruthy()
    const link = screen.getByText(/apply now/i)
    expect(link.getAttribute('href')).toBe('#/jobs')
  })

  it('flips to Enforced once a reconciler run finished after the save',
    async () => {
      getJobRuns.mockResolvedValue({
        runs: [run({ finished_at: '2026-06-21T12:05:00Z' })],
      })
      render(<GovernanceApplyStatus updatedAt={'2026-06-21T12:00:00Z'} />)
      expect(await screen.findByText(/Enforced/)).toBeTruthy()
    })
})

// The real-time per-action path — an `apply` result renders the ACTUAL
// post-apply state immediately (no "~5 min", no "apply now" link), and
// never claims enforced when it isn't.
describe('applyLabel (real-time states)', () => {
  it('null when no apply block (tick-driven path)', () => {
    expect(applyLabel(null)).toBeNull()
    expect(applyLabel({})).toBeNull()
  })
  it('force-block enforced → green Blocked · enforced', () => {
    const l = applyLabel({ state: 'enforced', enforced: true, denied: true })
    expect(l.tone).toBe('green')
    expect(l.text).toMatch(/Blocked · enforced/)
  })
  it('govern enforced (no current deny) → green Governed · enforced', () => {
    const l = applyLabel({ state: 'enforced', enforced: true, denied: false })
    expect(l.text).toMatch(/Governed · enforced/)
  })
  it('unblock allowed → green Unblocked · active', () => {
    const l = applyLabel({ state: 'allowed', enforced: false, denied: false })
    expect(l.text).toMatch(/Unblocked · active/)
  })
  it('unblock still over cap → amber Still blocked · over cap (truthful)', () => {
    const l = applyLabel({ state: 'pending', enforced: false, denied: true })
    expect(l.tone).toBe('amber')
    expect(l.text).toMatch(/Still blocked · over cap/)
    expect(l.note).toMatch(/raise the cap/i)
  })
  it('IDC → amber pending, never enforced, plain language (no tg-*)', () => {
    const l = applyLabel({ state: 'pending_idc', enforced: false })
    expect(l.tone).toBe('amber')
    expect(l.text).toMatch(/pending enforcement/)
    expect(`${l.text} ${l.note}`).not.toMatch(/tg-|AWSReservedSSO|permission-set policy/)
  })
  it('failed → red Not applied with the real reason', () => {
    const l = applyLabel({ state: 'failed', enforced: false, detail: 'throttled' })
    expect(l.tone).toBe('red')
    expect(l.text).toMatch(/Not applied/)
  })
})

describe('GovernanceApplyStatus with an apply prop (real-time)', () => {
  it('renders the enforced state and NO apply-now link', () => {
    const { container } = render(
      <GovernanceApplyStatus apply={{ state: 'enforced', enforced: true, denied: true }} />)
    expect(container.textContent).toMatch(/Blocked · enforced/)
    expect(container.textContent).not.toMatch(/apply now/i)
    expect(container.textContent).not.toMatch(/~5 min/)
    // does not poll job runs on the real-time path
    expect(getJobRuns).not.toHaveBeenCalled()
  })
})
