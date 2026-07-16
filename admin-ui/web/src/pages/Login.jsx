import React, { useEffect, useState } from 'react'
import { authProviders } from '../api'

/**
 * Cloud-only landing page for unauthenticated users. Hash-routed at
 * #/login. The buttons do a full-page navigation to `/auth/login` so
 * FastAPI sets HTTP-only cookies and 302's to Cognito's hosted page;
 * the picker bypass (`?identity_provider=`) makes Cognito land
 * directly on either the federated IdP or the COGNITO password form,
 * not its own picker.
 *
 * This landing page shows EXACTLY ONE primary login affordance, driven
 * by the configured identity setting (`/auth/providers`):
 *   - okta:true  (an external IdP owns the directory) → the SSO button
 *     only, plus a low-prominence "Trouble signing in?" break-glass
 *     recovery for a locked-out org admin (hands off to the COGNITO
 *     password page, bypassing the IdP — so a broken SAML config can't
 *     block it).
 *   - cognito:true (tg owns the directory) → the password CTA only; no
 *     SSO button and no recovery link (password is the everyday method).
 * Never a signup affordance — admins are provisioned. Falls back to the
 * password button if `/auth/providers` is unreachable.
 */
export default function Login() {
  const params = new URLSearchParams(
    typeof window !== 'undefined'
      ? window.location.search : '')
  const error = params.get('error')

  const [providers, setProviders] = useState(null)
  const [recoveryOpen, setRecoveryOpen] = useState(false)

  useEffect(() => {
    let cancelled = false
    authProviders()
      .then(p => { if (!cancelled) setProviders(p) })
      .catch(() => {
        if (!cancelled) setProviders({ cognito: true, okta: false })
      })
    return () => { cancelled = true }
  }, [])

  function go(identityProvider) {
    const q = identityProvider
      ? `?identity_provider=${encodeURIComponent(identityProvider)}`
      : ''
    window.location.href = `/auth/login${q}`
  }

  // The federated button uses the admin's configured SSO label
  // (sourced from sso_button_label via /auth/providers). Never fall
  // back to a hardcoded provider name like "Okta" — when the
  // configured provider is IDC, "Okta" is wrong. The backend default
  // is already a generic label ("Login with Your SSO"); this generic
  // string only covers the unlikely case it's blank.
  const ssoLabel = providers?.okta_display_name || 'Continue with SSO'
  const showSso = !!providers?.okta
  // Federation off (or fetch-pending): render the single password CTA.
  const cognitoOnly = !showSso
  // Break-glass recovery is offered only on an SSO org (on a password
  // org the password CTA already IS the recovery). Surfaced both in the
  // normal SSO state and on the auth-error state so an error is never a
  // dead end.
  const showRecovery = showSso

  // The recovery disclosure: a low-prominence toggle that reveals the
  // org-admin-only break-glass explanation + a button handing off to
  // Cognito's COGNITO password page (skips the IdP, so it works even
  // when SAML is broken).
  const recovery = showRecovery && (
    <div className="tg-login-recovery">
      <button
        type="button"
        aria-expanded={recoveryOpen}
        onClick={() => setRecoveryOpen(o => !o)}
        className="tg-login-recovery-toggle"
      >Trouble signing in?</button>
      {recoveryOpen && (
        <div role="region" className="tg-login-recovery-panel">
          <p className="tg-login-recovery-copy">
            Org-admin recovery — for an administrator who can repair SSO.
            This signs in with your password and <strong>bypasses
            SSO</strong>, so it works even when the SSO connection is
            misconfigured. Everyday users should sign in with SSO above.
          </p>
          <button
            type="button"
            onClick={() => go('COGNITO')}
            className="tg-login-cta-secondary"
          >Recover access with password</button>
        </div>
      )}
    </div>
  )

  return (
    <div className="tg-login-frame">
      <div className="tg-login-card">
        <div className="tg-login-eyebrow">
          <span className="dot"></span>Token Governance · tg-admin
        </div>

        <h1 className="tg-login-h1">Cost vs. Value</h1>

        <p className="tg-login-lede">
          Start with cost. <em>End with outcome.</em>
        </p>

        {error === 'expired' && (
          <div className="tg-login-alert tg-login-alert--info">
            Your session expired. Sign in again to continue.
          </div>
        )}
        {error && error !== 'expired' && (
          <div className="tg-login-alert tg-login-alert--err">
            {decodeURIComponent(error)}
          </div>
        )}

        <p className="tg-login-body">
          Sign in to manage Bedrock quotas, teams, and pricing
          for your org.
        </p>

        {cognitoOnly && (
          <button
            type="button"
            onClick={() => go(null)}
            className="tg-login-cta"
          >Sign in →</button>
        )}

        {showSso && (
          <>
            <button
              type="button"
              onClick={() => go(null)}
              className="tg-login-cta"
            >{ssoLabel} →</button>
            {recovery}
          </>
        )}
      </div>
    </div>
  )
}
