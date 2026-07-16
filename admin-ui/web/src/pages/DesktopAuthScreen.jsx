import React, { useState } from 'react'
import { api } from '../api'

/**
 * Shown by App.jsx only when:
 *   - window.__TG_DEPLOYMENT__ === 'desktop'  (tg-admin binary)
 *   - GET /api/desktop/auth-status returned {ok: false}
 *
 * The cloud surface never sees this. #132 / #197.
 */
export default function DesktopAuthScreen({ status, onRetry }) {
  const [busy, setBusy] = useState(false)
  const profile = status?.profile || ''
  const cmd = profile
    ? `aws sso login --profile ${profile}`
    : 'aws sso login --sso-session tg-sso'

  async function copy() {
    try { await navigator.clipboard.writeText(cmd) } catch {}
  }

  async function retry() {
    setBusy(true)
    try { await onRetry() } finally { setBusy(false) }
  }

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

        <p className="tg-login-body">
          tg-admin uses your AWS SSO session to authenticate
          to Bedrock and the admin API. Run this in your
          terminal, then click <strong>Retry</strong> below.
        </p>

        <div className="tg-login-cmd">
          <code>$ {cmd}</code>
          <button type="button" onClick={copy}>Copy</button>
        </div>

        <button
          type="button"
          onClick={retry}
          disabled={busy}
          className="tg-login-cta"
        >{busy ? 'Checking…' : 'Retry'}</button>

        {status?.detail && (
          <details className="tg-login-details">
            <summary>Details</summary>
            <pre>
              reason: {status.reason}
              {status.profile ? `\nprofile: ${status.profile}` : ''}
              {`\n\n${status.detail}`}
            </pre>
          </details>
        )}

        <p className="tg-login-foot">
          Different profile? Pass <code>--profile</code> to
          tg-admin and relaunch.
        </p>
      </div>
    </div>
  )
}

export async function fetchDesktopAuthStatus() {
  try {
    return await api.desktopAuthStatus()
  } catch (e) {
    return { ok: false, reason: 'fetch_failed', detail: String(e) }
  }
}
