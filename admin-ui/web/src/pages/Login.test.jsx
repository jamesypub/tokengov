import React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

// Login page: shows EXACTLY ONE primary login affordance driven by
// /auth/providers. SSO org (okta:true) → the federated button labeled
// with the configured SSO label (never a hardcoded "Okta") plus a
// low-prominence "Trouble signing in?" break-glass recovery (org-admin
// only; hands off to the COGNITO password page, bypassing the IdP).
// Password org (cognito:true) → the password CTA only, no SSO button,
// no recovery link. /auth/providers unreachable → degrade to the
// password button, never a dead page.

const authProviders = vi.fn()

vi.mock('../api', () => ({
  authProviders: (...a) => authProviders(...a),
}))

import Login from './Login'

beforeEach(() => {
  authProviders.mockReset()
  // jsdom has no real navigation; stub the location the page reads/sets.
  delete window.location
  window.location = { search: '', href: '' }
})

describe('Login — single config-driven affordance', () => {
  it('SSO org: one federated button with the configured label, no "Okta"', async () => {
    authProviders.mockResolvedValue({
      cognito: false, okta: true,
      okta_display_name: 'Continue with IDC',
    })
    render(<Login />)
    await waitFor(() =>
      expect(screen.getByText(/Continue with IDC/)).toBeTruthy())
    expect(screen.queryByText(/Okta/i)).toBeNull()
    // Exactly one primary CTA, and no always-visible secondary button.
    expect(document.querySelectorAll('.tg-login-cta').length).toBe(1)
    expect(
      document.querySelector('.tg-login-cta-secondary')).toBeNull()
    // The old admin_roles footer copy was removed.
    expect(screen.queryByText(/not registered/i)).toBeNull()
    expect(document.querySelector('.tg-login-foot')).toBeNull()
  })

  it('SSO org: the recovery toggle is a styled, interactive control', async () => {
    authProviders.mockResolvedValue({
      cognito: false, okta: true,
      okta_display_name: 'Continue with IDC',
    })
    render(<Login />)
    const toggle = await screen.findByRole('button', {
      name: /Trouble signing in\?/i,
    })
    // It carries the recovery-toggle class (styled as a visible link,
    // not inert text) and is a real button with aria-expanded.
    expect(toggle.classList.contains('tg-login-recovery-toggle'))
      .toBe(true)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
  })

  it('SSO org: a low-prominence recovery disclosure is present, collapsed', async () => {
    authProviders.mockResolvedValue({
      cognito: false, okta: true,
      okta_display_name: 'Continue with IDC',
    })
    render(<Login />)
    const toggle = await screen.findByRole('button', {
      name: /Trouble signing in\?/i,
    })
    // Collapsed by default — the recovery hand-off is not yet shown.
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByText(/Recover access with password/i)).toBeNull()
  })

  it('SSO org: opening recovery reveals the org-admin COGNITO hand-off', async () => {
    authProviders.mockResolvedValue({
      cognito: false, okta: true,
      okta_display_name: 'Continue with IDC',
    })
    render(<Login />)
    const toggle = await screen.findByRole('button', {
      name: /Trouble signing in\?/i,
    })
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    // Copy names the SSO-bypass nature of the path.
    expect(screen.getByText(/bypasses/i)).toBeTruthy()
    const recover = screen.getByText(/Recover access with password/i)
    expect(recover).toBeTruthy()
    // It hands off to the COGNITO password page (skips the IdP).
    fireEvent.click(recover)
    expect(window.location.href).toContain('identity_provider=COGNITO')
  })

  it('falls back to the generic SSO label when none configured', async () => {
    authProviders.mockResolvedValue({
      cognito: false, okta: true, okta_display_name: null,
    })
    render(<Login />)
    await waitFor(() =>
      expect(screen.getByText(/Continue with SSO/)).toBeTruthy())
    expect(screen.queryByText(/Okta/i)).toBeNull()
  })

  it('password org: only the password CTA, no SSO button, no recovery', async () => {
    authProviders.mockResolvedValue({ cognito: true, okta: false })
    render(<Login />)
    await waitFor(() =>
      expect(screen.getByText(/Sign in →/)).toBeTruthy())
    expect(
      document.querySelector('.tg-login-cta-secondary')).toBeNull()
    expect(screen.queryByRole('button', {
      name: /Trouble signing in\?/i,
    })).toBeNull()
  })

  it('/auth/providers unreachable → password button, never a dead page', async () => {
    authProviders.mockRejectedValue(new Error('network'))
    render(<Login />)
    await waitFor(() =>
      expect(screen.getByText(/Sign in →/)).toBeTruthy())
  })
})

describe('Login — auth error never dead-ends', () => {
  it('SSO org error: shows the error banner AND the recovery link', async () => {
    window.location = {
      search: '?error=' + encodeURIComponent('SAML attribute mismatch'),
      href: '',
    }
    authProviders.mockResolvedValue({
      cognito: false, okta: true,
      okta_display_name: 'Continue with IDC',
    })
    render(<Login />)
    await waitFor(() =>
      expect(screen.getByText(/SAML attribute mismatch/)).toBeTruthy())
    // The same recovery affordance is reachable from the error state.
    expect(await screen.findByRole('button', {
      name: /Trouble signing in\?/i,
    })).toBeTruthy()
  })
})
