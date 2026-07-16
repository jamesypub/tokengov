import React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

// The Diagnostics page: relocated read-only spend-source/models content
// + a "Report an issue" block linking to the PUBLIC tokengov repo's
// low-friction new-issue forms (never the private dev repo), opening in
// a new tab. Data calls (cur health + admin config) are mocked.

const curHealth = vi.fn()
const getAdminConfig = vi.fn()
const getSamlSettings = vi.fn()

vi.mock('../api', () => ({
  api: {
    curHealth: (...a) => curHealth(...a),
    getAdminConfig: (...a) => getAdminConfig(...a),
    getSamlSettings: (...a) => getSamlSettings(...a),
  },
}))

import Diagnostics from './Diagnostics'

beforeEach(() => {
  curHealth.mockReset()
  getAdminConfig.mockReset()
  getSamlSettings.mockReset()
  curHealth.mockResolvedValue({ status: 'healthy' })
  getAdminConfig.mockResolvedValue({
    cur_source: { glue_database: 'tg_cur' },
    cur_new_models: [{ model_id: 'us.anthropic.claude-x' }],
  })
  // Default: SAML off → the provider card is absent.
  getSamlSettings.mockResolvedValue({ configured: false })
})

describe('Diagnostics page', () => {
  it('has a Diagnostics h1 and renders the moved spend-source content', async () => {
    render(<Diagnostics />)
    expect(screen.getByRole('heading', { level: 1, name: /Diagnostics/ }))
      .toBeTruthy()
    await waitFor(() =>
      expect(screen.getByText('tg_cur')).toBeTruthy())
    expect(screen.getByText(/Spend source/i)).toBeTruthy()
    expect(screen.getByText(/us\.anthropic\.claude-x/)).toBeTruthy()
  })

  it('report-issue links point at the PUBLIC tokengov repo, new tab', async () => {
    render(<Diagnostics />)
    const bug = await screen.findByText(/Report a bug/i)
    const feat = screen.getByText(/Request a feature/i)
    const bugHref = bug.closest('a').getAttribute('href')
    const featHref = feat.closest('a').getAttribute('href')
    // Public tokengov repo, NOT the private dev repo.
    expect(bugHref).toContain('github.com/jamesypub/tokengov')
    expect(bugHref).not.toContain('tokengov')
    expect(bugHref).toContain('template=bug.yml')
    expect(featHref).toContain('template=feature_request.yml')
    // Opens in a new tab, safely.
    expect(bug.closest('a').getAttribute('target')).toBe('_blank')
    expect(bug.closest('a').getAttribute('rel')).toContain('noopener')
  })

  it('renders without the spend-source detail when CUR data is absent', async () => {
    getAdminConfig.mockResolvedValue({})
    curHealth.mockResolvedValue(null)
    render(<Diagnostics />)
    // The report-issue block is static, so it's always present.
    expect(await screen.findByText(/Report an issue/i)).toBeTruthy()
  })

  it('shows the read-only SAML provider name when SAML is configured', async () => {
    getSamlSettings.mockResolvedValue({
      configured: true, provider_name: 'tg-cognito-saml-1a2b',
    })
    render(<Diagnostics />)
    await waitFor(() =>
      expect(screen.getByText('tg-cognito-saml-1a2b')).toBeTruthy())
    expect(screen.getByText(/SAML provider \(Cognito\)/i)).toBeTruthy()
    expect(screen.getByText(/Managed by tg/i)).toBeTruthy()
  })

  it('omits the SAML provider card when SAML is not configured', async () => {
    getSamlSettings.mockResolvedValue({ configured: false })
    render(<Diagnostics />)
    // Wait for the always-present report block, then assert absence.
    await screen.findByText(/Report an issue/i)
    expect(screen.queryByText(/SAML provider \(Cognito\)/i)).toBeNull()
  })
})
