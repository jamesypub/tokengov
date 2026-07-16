import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

// EnableLoginModal calls api.authProviders() + api.enableLogin(). Mock
// the api module so we can drive the Cognito vs external-IdP branches.
const authProviders = vi.fn()
const enableLogin = vi.fn()
vi.mock('../api', () => ({
  api: {
    authProviders: (...a) => authProviders(...a),
    enableLogin: (...a) => enableLogin(...a),
  },
  fmtUsd: (n) => `$${n}`,
  getTeams: () => Promise.resolve({ teams: [] }),
}))

import { EnableLoginModal } from './UserDetail'

const USER = { email: 'new@t.com' }

beforeEach(() => {
  authProviders.mockReset()
  enableLogin.mockReset()
  // jsdom origin is http://localhost:3000
})

describe('EnableLoginModal — mode-aware copy + sign-in URL', () => {
  it('Cognito branch: mode-aware button + self-sufficient-email copy + URL/Copy', async () => {
    authProviders.mockResolvedValue({
      cognito: true, okta: false, cognito_provisioning: true })
    render(<EnableLoginModal user={USER} onClose={() => {}} onDone={() => {}} />)
    // the Cognito button names the action (no "Enable login"/"Cognito")
    expect(await screen.findByRole('button', {
      name: 'Create login & send invite' })).toBeInTheDocument()
    // guidance: the email is self-sufficient (temp password + sign-in
    // link); it NO LONGER says "Forgot password"/"also send the URL"
    const notes = screen.getAllByRole('note').map(n => n.textContent).join(' ')
    expect(notes).toMatch(/temporary password/)
    expect(notes).toMatch(/sign-in link/)
    expect(notes).not.toMatch(/Forgot password/)
    // sign-in URL field kept (backup); carries the browser origin + /login
    const url = screen.getByLabelText('Sign-in URL')
    expect(url.value).toMatch(/\/login$/)
    // Copy announces to the aria-live region
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() =>
      expect(screen.getByText('Sign-in URL copied')).toBeInTheDocument())
  })

  it('external-IdP branch: two-step grant-in-IdP note names the SSO', async () => {
    authProviders.mockResolvedValue({
      cognito: false, okta: true, okta_display_name: 'Acme SSO',
      cognito_provisioning: false })
    render(<EnableLoginModal user={USER} onClose={() => {}} onDone={() => {}} />)
    expect(await screen.findByText(/Two steps — both on your side/))
      .toBeInTheDocument()
    // the SSO button names the real action (authorize + surface invite)
    expect(screen.getByRole('button', {
      name: 'Authorize & show invite' })).toBeInTheDocument()
    // names the provider and the "grant access in your identity provider" step
    const note = screen.getByRole('note')
    expect(note.textContent).toMatch(/Acme SSO/)
    expect(note.textContent).toMatch(/identity provider/)
    // sign-in URL still shown
    expect(screen.getByLabelText('Sign-in URL').value).toMatch(/\/login$/)
  })

  it('Cognito toast says the invite was emailed (no relay/Forgot-password)', async () => {
    authProviders.mockResolvedValue({
      cognito: true, okta: false, cognito_provisioning: true })
    enableLogin.mockResolvedValue({ cognito_provisioned: true })
    let toast = null
    render(<EnableLoginModal
      user={USER} onClose={() => {}} onDone={(m) => { toast = m }} />)
    fireEvent.click(await screen.findByRole('button', {
      name: 'Create login & send invite' }))
    await waitFor(() => expect(toast).toBeTruthy())
    expect(toast).toMatch(/invite emailed/)
    expect(toast).toMatch(/temporary password/)
    // the email is self-sufficient — no relay-URL / Forgot-password step
    expect(toast).not.toMatch(/Forgot password/)
  })

  it('external toast names the SSO, the IdP-grant step, and the URL; no email', async () => {
    authProviders.mockResolvedValue({
      cognito: false, okta: true, okta_display_name: 'Acme SSO',
      cognito_provisioning: false })
    enableLogin.mockResolvedValue({ cognito_provisioned: false })
    let toast = null
    render(<EnableLoginModal
      user={USER} onClose={() => {}} onDone={(m) => { toast = m }} />)
    await screen.findByText(/Two steps/)
    fireEvent.click(screen.getByRole('button', { name: 'Authorize & show invite' }))
    await waitFor(() => expect(toast).toBeTruthy())
    expect(toast).toMatch(/Acme SSO/)
    expect(toast).toMatch(/no.*email sent/i)
    expect(toast).toMatch(/identity provider/)
    expect(toast).toMatch(/\/login/)
  })
})
