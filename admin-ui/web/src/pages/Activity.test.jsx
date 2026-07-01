import React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

// #895: a member opening Activity must see their OWN usage/activity
// (the 200 endpoints /api/summary + /api/usage), even though the
// secondary getTeams() call (/api/teams) is org-admin / team-admin
// only and 403s a plain member ("Insufficient permissions"). The bug:
// getTeams was inside the page's fatal Promise.all, so its 403
// rejected the whole chain and blanked the page — discarding the
// member's entitled data. teamMap is decorative (team-name labels),
// so a 403 there must degrade to an empty map, never blank the page.

const getSummary = vi.fn()
const getUsage = vi.fn()
const getTeams = vi.fn()

vi.mock('../api', () => ({
  getSummary: (...a) => getSummary(...a),
  getUsage: (...a) => getUsage(...a),
  getTeams: (...a) => getTeams(...a),
  // pass-through formatters the page imports.
  fmtUsd: (v) => `$${Number(v || 0).toFixed(2)}`,
  fmtTokens: (v) => String(v || 0),
  // SpendAsOf imports `api`; leave curDataThrough absent so it no-ops.
  api: {},
}))

vi.mock('../TeamScope', () => ({
  useTeamScope: () => ({ selectedTeam: null, persona: 'member' }),
}))

import Activity from './Activity'

// #1192: the summary cards moved to Users; /api/summary is reshaped.
// Activity still calls it (the table reads `month`), so the mock keeps
// a `month`; the card-count fields no longer matter here.
const _summary = { month: '2026-06', active_users: 1,
  approaching_cap_count: 0, blocked_count: 0, total_spend_usd: 0 }
const _usage = { month: '2026-06', rows: [
  { email: 'team-1.1-member-1@example.com', model: 'us.anthropic.claude-haiku',
    spend_usd: 1.23, input_tokens: 100, output_tokens: 50,
    cache_read_tokens: 700, cache_write_tokens: 300, team_id: 't1',
    // status + cap are present in the row but must NOT render
    // as columns (they moved to the Users page).
    status: 'active', cap_usd: 99.0 },
] }

beforeEach(() => {
  getSummary.mockReset(); getUsage.mockReset(); getTeams.mockReset()
})

describe('Activity — secondary getTeams 403 is non-fatal (#895)', () => {
  it('renders the member\'s own activity when getTeams 403s', async () => {
    getSummary.mockResolvedValue(_summary)
    getUsage.mockResolvedValue(_usage)
    // the member's own data is 200; the admin-only teams call 403s.
    getTeams.mockRejectedValue(new Error('Insufficient permissions'))

    render(<Activity />)

    // The page renders (header present), NOT the full-page error.
    expect(await screen.findByRole('heading', { name: 'Activity' }))
      .toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByText('Failed to load activity')).toBeNull())
    // The member's own row is shown despite the teams 403.
    expect(
      screen.getByText('team-1.1-member-1@example.com')).toBeInTheDocument()
  })

  it('still blanks (fatal) when the member\'s OWN data fails', async () => {
    // If summary/usage themselves fail, that IS fatal — the member has
    // no data to show; the error page is correct there.
    getSummary.mockRejectedValue(new Error('boom'))
    getUsage.mockResolvedValue(_usage)
    getTeams.mockResolvedValue({ teams: [] })

    render(<Activity />)
    expect(await screen.findByText('Failed to load activity'))
      .toBeInTheDocument()
  })

  it('uses team-name labels when getTeams succeeds (admin path)', async () => {
    getSummary.mockResolvedValue(_summary)
    getUsage.mockResolvedValue(_usage)
    getTeams.mockResolvedValue({ teams: [{ team_id: 't1', name: 'Team One' }] })

    render(<Activity />)
    expect(await screen.findByRole('heading', { name: 'Activity' }))
      .toBeInTheDocument()
    // the decorative label resolves from teamMap when available
    expect(await screen.findByText('Team One')).toBeInTheDocument()
  })

  it('#1192: Activity is table-only — no summary header cards', async () => {
    getSummary.mockResolvedValue(_summary)
    getUsage.mockResolvedValue(_usage)
    getTeams.mockResolvedValue({ teams: [] })

    render(<Activity />)
    expect(await screen.findByRole('heading', { name: 'Activity' }))
      .toBeInTheDocument()
    // the relocated cards must NOT render on Activity anymore
    expect(screen.queryByText('Active users')).toBeNull()
    expect(screen.queryByText('Quota alerts')).toBeNull()
    expect(screen.queryByText('Cache hit rate')).toBeNull()
    // the usage table still renders
    expect(
      screen.getByText('team-1.1-member-1@example.com')).toBeInTheDocument()
  })
})

// The Activity table is now a pure spend/usage view (companion to the
// summary-cards relocation): columns are User · Team · Model · Spend ·
// Input tokens · Output tokens · Cache read tokens · Cache write tokens.
// No Status, no Cap (those moved to the Users page), and the Team
// column shows the team NAME (the stale-closure fix: teamMap in the
// columns useMemo deps).
describe('Activity table columns', () => {
  function headers() {
    return screen.getAllByRole('columnheader').map(h => h.textContent)
  }

  it('renders exactly the spend/usage columns — no Status, no Cap', async () => {
    getSummary.mockResolvedValue(_summary)
    getUsage.mockResolvedValue(_usage)
    getTeams.mockResolvedValue({ teams: [{ team_id: 't1', name: 'Team One' }] })

    render(<Activity />)
    await screen.findByText('team-1.1-member-1@example.com')

    const hs = headers().join(' | ')
    expect(hs).toContain('Input tokens')
    expect(hs).toContain('Output tokens')
    expect(hs).toContain('Cache read tokens')
    expect(hs).toContain('Cache write tokens')
    // dropped columns
    expect(hs).not.toContain('Status')
    expect(hs).not.toContain('Cap')
    // the bare 'Input'/'Output' headers were renamed (now "… tokens")
    expect(headers().some(h => h === 'Input')).toBe(false)
    expect(headers().some(h => h === 'Output')).toBe(false)
  })

  it('Team column shows the team NAME once teams load (stale-closure fix)', async () => {
    getSummary.mockResolvedValue(_summary)
    getUsage.mockResolvedValue(_usage)
    getTeams.mockResolvedValue({ teams: [{ team_id: 't1', name: 'Team One' }] })

    render(<Activity />)
    // The name renders (not the raw 't1' id) — proves teamMap is a
    // live dep of the columns memo, not the initial empty capture.
    expect(await screen.findByText('Team One')).toBeInTheDocument()
    expect(screen.queryByText('t1')).toBeNull()
  })

  it('cache token counts render via fmtTokens (no E-notation)', async () => {
    getSummary.mockResolvedValue(_summary)
    getUsage.mockResolvedValue(_usage)
    getTeams.mockResolvedValue({ teams: [] })

    render(<Activity />)
    await screen.findByText('team-1.1-member-1@example.com')
    // the mocked fmtTokens passthrough renders the raw count string
    expect(screen.getByText('700')).toBeInTheDocument()
    expect(screen.getByText('300')).toBeInTheDocument()
  })
})
