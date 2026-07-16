import React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

// #447: the Jira tab + route are gated behind the runtime
// admin_config flag jira_enabled (read via getAdminConfig).
// These tests pin: tab hidden when flag off, shown when on, and
// a deep-link to #/velocity-cost/jira with the flag off falls
// back to Cost (no Jira panel).

const getAdminConfig = vi.fn()
const velocityLeaderboard = vi.fn()
const velocitySpeed = vi.fn()

vi.mock('../api', () => ({
  api: {
    getAdminConfig: () => getAdminConfig(),
    velocityLeaderboard: (...a) => velocityLeaderboard(...a),
    velocitySpeed: (...a) => velocitySpeed(...a),
  },
}))

// #703: the page gates the admin-only getAdminConfig fetch on the
// persona from useTeamScope, so the mock must expose a persona the
// tests can vary. Default org_admin (the Jira-flag tests need the
// fetch to fire); the #703 tests override to a non-admin.
let _persona = 'org_admin'
vi.mock('../TeamScope', () => ({
  useTeamScope: () => ({ selectedTeam: null, persona: _persona }),
}))

import VelocityCost from './VelocityCost'

beforeEach(() => {
  _persona = 'org_admin'
  getAdminConfig.mockReset()
  velocityLeaderboard.mockReset()
  velocitySpeed.mockReset()
  velocityLeaderboard.mockResolvedValue({ teams: [], org: {} })
  velocitySpeed.mockResolvedValue({ teams: [], org: {} })
  window.location.hash = '#/velocity-cost/cost'
})

describe('VelocityCost Jira tab gating (#447)', () => {
  it('hides the Jira tab when jira_enabled is false', async () => {
    getAdminConfig.mockResolvedValue({ jira_enabled: false })
    render(<VelocityCost />)
    // Cost + Speed always render.
    expect(await screen.findByText('Cost')).toBeInTheDocument()
    expect(screen.getByText('Speed')).toBeInTheDocument()
    // Jira tab must not appear.
    await waitFor(() =>
      expect(screen.queryByText('Jira')).toBeNull())
  })

  it('shows the Jira tab when jira_enabled is true', async () => {
    getAdminConfig.mockResolvedValue({ jira_enabled: true })
    render(<VelocityCost />)
    expect(await screen.findByText('Jira')).toBeInTheDocument()
    expect(screen.getByText('Sprint / Epic / $/SP')).toBeInTheDocument()
  })

  it('treats missing jira_enabled as off (default OFF)', async () => {
    getAdminConfig.mockResolvedValue({ org_default_quota_usd: 100 })
    render(<VelocityCost />)
    expect(await screen.findByText('Cost')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByText('Jira')).toBeNull())
  })

  it('falls back to Cost when deep-linking jira with flag off', async () => {
    window.location.hash = '#/velocity-cost/jira'
    getAdminConfig.mockResolvedValue({ jira_enabled: false })
    render(<VelocityCost />)
    // No Jira tab, and the Cost leaderboard fetch fires (the
    // effective tab fell back to Cost rather than the Jira view).
    await waitFor(() =>
      expect(velocityLeaderboard).toHaveBeenCalled())
    expect(screen.queryByText('Jira')).toBeNull()
  })

  it('treats a failed config fetch as flag off', async () => {
    window.location.hash = '#/velocity-cost/jira'
    getAdminConfig.mockRejectedValue(new Error('boom'))
    render(<VelocityCost />)
    await waitFor(() =>
      expect(velocityLeaderboard).toHaveBeenCalled())
    expect(screen.queryByText('Jira')).toBeNull()
  })
})

describe('VelocityCost admin-fetch gating (#703)', () => {
  it('does NOT call getAdminConfig for a non-admin persona', async () => {
    // team_admin / member can't read /admin/config (403). The page
    // is visible to them, so it must not fire the admin-only fetch
    // (that 403 was the console noise #703 is about).
    _persona = 'team_admin'
    getAdminConfig.mockResolvedValue({ jira_enabled: true })
    render(<VelocityCost />)
    // Page still renders its always-visible tabs…
    expect(await screen.findByText('Cost')).toBeInTheDocument()
    expect(screen.getByText('Speed')).toBeInTheDocument()
    // …and the leaderboard (scoped) data fetch still runs…
    await waitFor(() =>
      expect(velocityLeaderboard).toHaveBeenCalled())
    // …but the admin-only config fetch was never issued.
    expect(getAdminConfig).not.toHaveBeenCalled()
    // and the Jira tab stays hidden (flag defaults OFF for non-admins).
    expect(screen.queryByText('Jira')).toBeNull()
  })

  it('member persona also skips getAdminConfig', async () => {
    _persona = 'member'
    getAdminConfig.mockResolvedValue({ jira_enabled: true })
    render(<VelocityCost />)
    expect(await screen.findByText('Cost')).toBeInTheDocument()
    expect(getAdminConfig).not.toHaveBeenCalled()
  })

  it('DOES call getAdminConfig for org_admin', async () => {
    _persona = 'org_admin'
    getAdminConfig.mockResolvedValue({ jira_enabled: true })
    render(<VelocityCost />)
    await waitFor(() =>
      expect(getAdminConfig).toHaveBeenCalled())
    // org_admin still sees the flag-driven Jira tab.
    expect(await screen.findByText('Jira')).toBeInTheDocument()
  })
})
