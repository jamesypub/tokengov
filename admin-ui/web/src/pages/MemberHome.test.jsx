import React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

// #942: an authenticated-but-unprovisioned principal (persona=member,
// no users row) hits MemberHome, whose self getUser 404s. That must
// render a graceful "not set up yet" empty-state, NOT the raw
// "User <email> not found" error string (#9 recognize/recover, #1
// system status). A genuine non-404 failure still shows the banner;
// a provisioned member still sees their usage + profile.

const getUser = vi.fn()
const getLinkedAccounts = vi.fn()

vi.mock('../api', () => ({
  api: {
    getUser: (...a) => getUser(...a),
    getLinkedAccounts: (...a) => getLinkedAccounts(...a),
  },
  fmtUsd: (v) => `$${Number(v || 0).toFixed(2)}`,
}))

import MemberHome from './MemberHome'

const ME = { email: 'tg-org-admin@example.com', persona: 'member' }

function _err(status, msg) {
  const e = new Error(msg)
  e.status = status
  return e
}

beforeEach(() => {
  getUser.mockReset()
  getLinkedAccounts.mockReset()
  getLinkedAccounts.mockResolvedValue([])
})

describe('MemberHome — unprovisioned principal (#942)', () => {
  it('renders the not-set-up empty-state on a 404, not the raw error', async () => {
    getUser.mockRejectedValue(_err(404, 'User tg-org-admin@example.com not found'))
    render(<MemberHome me={ME} />)
    await waitFor(() =>
      expect(screen.getByText(/usage isn.t set up yet/i)).toBeTruthy())
    // the raw "not found" error string must NOT appear
    expect(screen.queryByText(/not found/i)).toBeNull()
    // guidance mentions the ~24h delay / ask-your-admin recovery
    expect(screen.getByText(/~24 hours/)).toBeTruthy()
    expect(screen.getByText(/ask your admin/i)).toBeTruthy()
  })

  it('renders usage + profile for a provisioned member', async () => {
    getUser.mockResolvedValue({
      email: ME.email, display_name: 'Org Admin',
      mtd_spend_usd: 12.5, effective_quota_usd: 100, pct_used: 12,
      version: 1, status: 'active',
    })
    render(<MemberHome me={ME} />)
    await waitFor(() => expect(screen.getByText('This month')).toBeTruthy())
    expect(screen.getByText('Your profile')).toBeTruthy()
    expect(screen.queryByText(/isn.t set up yet/i)).toBeNull()
  })

  it('still shows the error banner on a genuine (non-404) failure', async () => {
    getUser.mockRejectedValue(_err(500, 'internal error'))
    render(<MemberHome me={ME} />)
    await waitFor(() => expect(screen.getByText('internal error')).toBeTruthy())
    // not mistaken for the empty-state
    expect(screen.queryByText(/isn.t set up yet/i)).toBeNull()
  })
})
