import React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

// The governance-drift sweep is detect-only and runs daily on the
// scheduler; without a "Run now" entry the Users banner shows the
// stale last-sweep result for ~24h after an admin fixes drift. These
// pin that the run-now menu exposes the job and triggers the existing
// /api/jobs/run endpoint (no backend change).

const whoami = vi.fn()
const curHealth = vi.fn()
const getAdminConfig = vi.fn()
const getJobRuns = vi.fn()
const runJob = vi.fn()

vi.mock('../api', () => ({
  api: {
    whoami: () => whoami(),
    curHealth: () => curHealth(),
    getAdminConfig: () => getAdminConfig(),
    getJobRuns: () => getJobRuns(),
    runJob: (...a) => runJob(...a),
  },
}))

vi.mock('../TeamScope', () => ({
  useTeamScope: () => ({ selectedTeam: null, persona: 'org_admin' }),
}))

import Jobs from './Jobs'

beforeEach(() => {
  whoami.mockReset(); curHealth.mockReset(); getAdminConfig.mockReset()
  getJobRuns.mockReset(); runJob.mockReset()
  whoami.mockResolvedValue({ email: 'admin@example.com' })
  curHealth.mockResolvedValue(null)
  getAdminConfig.mockResolvedValue({})
  getJobRuns.mockResolvedValue({ runs: [], pause_until: null })
  runJob.mockResolvedValue({ errors: [] })
})

describe('Jobs run-now menu — Governance drift check', () => {
  async function openMenu() {
    render(<Jobs />)
    // wait for the initial loads to settle
    await waitFor(() => expect(getJobRuns).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: /Run now/i }))
  }

  it('shows Governance drift check in the run-now menu', async () => {
    await openMenu()
    // a Governance group header + the job entry
    expect(screen.getByText('Governance')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /Governance drift check/i }),
    ).toBeInTheDocument()
  })

  it('clicking it POSTs runJob("governance_drift_check")', async () => {
    await openMenu()
    fireEvent.click(
      screen.getByRole('button', { name: /Governance drift check/i }))
    await waitFor(() =>
      expect(runJob).toHaveBeenCalledWith('governance_drift_check'))
    // success toast (existing runOne path)
    expect(
      await screen.findByText(/Governance drift check — done/i),
    ).toBeInTheDocument()
  })
})
