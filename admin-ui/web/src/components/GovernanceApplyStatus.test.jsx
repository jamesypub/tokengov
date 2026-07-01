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
  governanceApplyState,
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
