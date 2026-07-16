// Shared SSO-vs-Cognito help + invite copy for the user-onboarding
// surfaces (Add user and Enable login). Building this in ONE place is
// deliberate: the two modals must never diverge on the auth-method
// guidance or the invite wording. Both consume these helpers.
//
// The auth branch is derived from GET /auth/providers
// ({cognito, okta, okta_display_name, cognito_provisioning}): `okta`
// true → an external IdP (SSO) owns identities; otherwise tg owns the
// Cognito directory and provisions the login itself.

// True when an external IdP (SAML/IDC) is configured — tg authorizes
// only and the user signs in via SSO. Cognito branch otherwise.
export function isExternalIdp(providers) {
  return !!(providers && providers.okta)
}

// The display name of the SSO provider for the "grant access in your
// {SSO}" wording. Generic ("company SSO") when unnamed, so the copy
// survives any external IdP (never a literal "IAM Identity Center").
export function ssoName(providers) {
  const n = providers && providers.okta_display_name
  const s = (n || '').trim()
  return s || 'company SSO'
}

// The in-modal help text shown BEFORE submitting. Mode-aware and
// written in plain language (no AWS/mechanism names): external IdP →
// "adding recognizes them, but they still need access granted in
// {SSO}"; Cognito → "we create the login and email them everything
// they need (temp password + sign-in link)." The Cognito invite email
// is self-sufficient — it carries both the sign-in URL and a temporary
// password — so the admin does NOT have to relay a link.
export function helpText(providers) {
  if (isExternalIdp(providers)) {
    return (
      'Your organization signs in through ' + ssoName(providers) +
      '. Adding them here lets tg recognize them — but they still ' +
      'need access granted in ' + ssoName(providers) + ' before they ' +
      'can sign in. After you add them, you’ll get a sign-in link to ' +
      'share.'
    )
  }
  return (
    'tg will create their login and email them everything they need — ' +
    'a temporary password and the sign-in link. They’ll set their own ' +
    'password on first sign-in.'
  )
}

// The copyable invite text shown AFTER a successful add/enable.
// `origin` is the login host the admin is on (window.location.origin —
// the ALB). Contains only the login URL + auth-method guidance; NEVER
// a password/secret (Cognito emails the temp password out-of-band).
export function inviteText(providers, origin) {
  const base = (origin || '').replace(/\/+$/, '')
  const url = base + '/login'
  if (isExternalIdp(providers)) {
    return (
      "You've been granted access to Token Governance. Sign in at " +
      url + ' and choose "Log in with ' + ssoName(providers) + '."'
    )
  }
  return (
    "You've been granted access to Token Governance. Sign in at " +
    url + '. A temporary password was emailed to you separately; ' +
    "you'll set a new one on first sign-in."
  )
}
