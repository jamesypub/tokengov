// Dev-only impersonation helper. Backed by /api/dev/personas
// which the api gates on TG_AUTH_TEST_TRUST=1, so this is
// inert in any prod-shaped deployment.
//
// Usage:
//   - api.js attaches X-Tg-Test-Email when getImpersonation()
//     returns an email
//   - Layout.jsx renders a "View as" select that calls
//     setImpersonation()
//
// The chosen email persists in localStorage so a hard refresh
// keeps the same persona — exactly what you want when testing
// scope-dependent UI.

const KEY = 'tg_dev_impersonate'

export function getImpersonation() {
  try {
    return localStorage.getItem(KEY) || ''
  } catch {
    return ''
  }
}

export function setImpersonation(email) {
  try {
    if (email) localStorage.setItem(KEY, email)
    else localStorage.removeItem(KEY)
  } catch {}
  // Reload so every page-level fetch (including whoami) re-runs
  // with the new identity. Cheaper than threading impersonation
  // through every component's data hooks.
  if (typeof window !== 'undefined') window.location.reload()
}
