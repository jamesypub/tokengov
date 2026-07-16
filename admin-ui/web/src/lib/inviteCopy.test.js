import { describe, it, expect } from 'vitest'
import {
  isExternalIdp, ssoName, helpText, inviteText,
} from './inviteCopy'

// Shared SSO-vs-Cognito help + invite copy consumed by BOTH the
// Add-user and Enable-login modals. These assertions pin the exact
// wording so the two surfaces can never diverge, and cover verifying
// the copied string in both branches.

const COGNITO = { cognito: true, okta: false, cognito_provisioning: true }
const SSO = { cognito: false, okta: true, okta_display_name: 'Acme SSO' }
const SSO_UNNAMED = { cognito: false, okta: true, okta_display_name: null }

describe('isExternalIdp', () => {
  it('true only when okta (external IdP) is set', () => {
    expect(isExternalIdp(SSO)).toBe(true)
    expect(isExternalIdp(COGNITO)).toBe(false)
    expect(isExternalIdp(null)).toBe(false)
    expect(isExternalIdp({})).toBe(false)
  })
})

describe('ssoName', () => {
  it('uses the provider display name when present', () => {
    expect(ssoName(SSO)).toBe('Acme SSO')
  })
  it('falls back to generic "company SSO" when unnamed', () => {
    expect(ssoName(SSO_UNNAMED)).toBe('company SSO')
    expect(ssoName(COGNITO)).toBe('company SSO')
  })
})

describe('helpText', () => {
  // Plain-language pass: no AWS/mechanism jargon ("Cognito", bare
  // "authorize"); each branch states the outcome + the admin's next
  // step and (SSO) names the provider.
  it('SSO branch: names the provider, two explicit steps, no jargon', () => {
    const t = helpText(SSO)
    expect(t).toMatch(/Acme SSO/)
    // the two required steps: add here + grant access in {SSO}
    expect(t).toMatch(/recognize them/)
    expect(t).toMatch(/access granted in Acme SSO/)
    expect(t).toMatch(/sign-in link to share/)
    // no jargon
    expect(t).not.toMatch(/\bauthorize\b/i)
    expect(t).not.toMatch(/Cognito/)
    expect(t).not.toMatch(/identity provider/)
  })
  it('SSO branch: falls back to generic "company SSO" when unnamed', () => {
    const t = helpText(SSO_UNNAMED)
    expect(t).toMatch(/company SSO/)
    expect(t).not.toMatch(/IAM Identity Center/)
  })
  it('Cognito branch: self-sufficient email (temp password + link), no jargon', () => {
    const t = helpText(COGNITO)
    expect(t).toMatch(/create their login/)
    // the email carries BOTH a temp password and the sign-in link, so
    // the admin has nothing to relay
    expect(t).toMatch(/temporary password/)
    expect(t).toMatch(/sign-in link/)
    // no longer implies the admin must relay a link
    expect(t).not.toMatch(/link to share/)
    expect(t).not.toMatch(/Cognito/)
    expect(t).not.toMatch(/\bauthorize\b/i)
  })
})

describe('inviteText', () => {
  it('SSO branch: login URL + names the real button, no artifact/secret', () => {
    const t = inviteText(SSO, 'https://tg.example.com')
    expect(t).toContain('https://tg.example.com/login')
    expect(t).toMatch(/Acme SSO/)
    expect(t).toMatch(/Log in with Acme SSO/)
    // the malformed hardcoded artifact (literal ellipsis) is gone
    expect(t).not.toMatch(/Login with … SSO/)
    expect(t).not.toContain('…')
    expect(t).not.toMatch(/password/i)   // never a secret in the invite
  })
  it('Cognito branch: login URL + temp-password-emailed-separately', () => {
    const t = inviteText(COGNITO, 'https://tg.example.com')
    expect(t).toContain('https://tg.example.com/login')
    expect(t).toMatch(/temporary password was emailed to you separately/)
    // The invite never carries the password itself — only that one was
    // emailed out-of-band.
    expect(t).not.toMatch(/password is|password:/i)
  })
  it('normalizes a trailing slash on the origin', () => {
    const t = inviteText(COGNITO, 'https://tg.example.com/')
    expect(t).toContain('https://tg.example.com/login')
    expect(t).not.toContain('.com//login')
  })
})
