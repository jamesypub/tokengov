import { getImpersonation } from './dev'

// Deployment mode: 'desktop' is injected by tg-admin's
// server.py; cloud's index.html omits it, so undefined = cloud.
const IS_DESKTOP =
  typeof window !== 'undefined' &&
  window.__TG_DEPLOYMENT__ === 'desktop'

const MUTATING = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

// CSRF token cache. Populated on first mutating call (or
// explicitly via primeCsrf()). Refreshed on 403 mismatch.
let _csrfToken = null

function _readCsrfCookie() {
  if (typeof document === 'undefined') return null
  const m = document.cookie.match(/(?:^|;\s*)tg_csrf=([^;]+)/)
  return m ? decodeURIComponent(m[1]) : null
}

async function _primeCsrf() {
  // Prefer cookie if set (issued by /auth/callback); otherwise
  // fetch a fresh one from /api/csrf.
  const fromCookie = _readCsrfCookie()
  if (fromCookie) { _csrfToken = fromCookie; return _csrfToken }
  try {
    const r = await fetch('/api/csrf')
    if (r.ok) {
      const d = await r.json()
      _csrfToken = d.csrf_token
    }
  } catch {}
  return _csrfToken
}

function _onUnauthenticated() {
  // Desktop: the binary's <DesktopAuthScreen> handles this on
  // page load; mid-session 401s just throw and let callers
  // surface the error — there's no /login page in the binary.
  if (IS_DESKTOP) return
  // Cloud: bounce to Okta. Preserve the current location so
  // the user lands back where they were after callback.
  if (typeof window !== 'undefined') {
    const here = window.location.pathname + window.location.hash
    if (!window.location.pathname.startsWith('/login')) {
      window.location.href =
        '/login?next=' + encodeURIComponent(here)
    }
  }
}

async function http(method, path, body, opts = {}) {
  const fetchOpts = {
    method, headers: {},
    credentials: 'same-origin',  // send cookies on cloud
  }
  if (body !== undefined) {
    fetchOpts.headers['Content-Type'] = 'application/json'
    fetchOpts.body = JSON.stringify(body)
  }
  // Dev impersonation. The api ignores this unless
  // TG_AUTH_TEST_TRUST=1, so it's safe to attach unconditionally.
  const impersonate = getImpersonation()
  if (impersonate) {
    fetchOpts.headers['X-Tg-Test-Email'] = impersonate
  }
  if (opts.ifMatch !== undefined && opts.ifMatch !== null) {
    fetchOpts.headers['If-Match'] = String(opts.ifMatch)
  }
  // Attach CSRF on mutating cloud requests. Desktop uses SigV4
  // (Authorization header) which the backend exempts.
  if (!IS_DESKTOP && MUTATING.has(method)) {
    if (!_csrfToken) await _primeCsrf()
    if (_csrfToken) {
      fetchOpts.headers['X-CSRF-Token'] = _csrfToken
    }
  }

  let r = await fetch(path, fetchOpts)

  // CSRF mismatch on a stale token — refresh and retry once.
  if (r.status === 403 && !IS_DESKTOP && MUTATING.has(method)) {
    const peek = await r.clone().text()
    if (peek.includes('CSRF')) {
      _csrfToken = null
      await _primeCsrf()
      if (_csrfToken) {
        fetchOpts.headers['X-CSRF-Token'] = _csrfToken
        r = await fetch(path, fetchOpts)
      }
    }
  }

  const text = await r.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }

  if (r.status === 401) _onUnauthenticated()

  if (!r.ok) {
    // FastAPI's `detail` may be a plain string OR a structured
    // object like {code, detail} (the analytics 503s use the
    // latter: api_runner_not_configured / creds_expired). #433:
    // unwrap the object form so `msg` is a string (not the
    // "[object Object]" you get from String(obj)) and `code`
    // resolves from the nested field — otherwise the
    // creds_expired / api_runner banners never trigger and the
    // Cost Reports error renders as "[object Object]".
    const detail = data && data.detail
    let msg, code
    if (detail && typeof detail === 'object') {
      msg = detail.detail || detail.message
        || `${r.status} ${r.statusText}`
      code = detail.code
    } else {
      msg = (detail || (data && data.error))
        || `${r.status} ${r.statusText}`
      code = data && data.code
    }
    const err = new Error(msg)
    err.status = r.status
    err.code = code
    err.data = data
    throw err
  }
  return data
}

export async function primeCsrf() { return _primeCsrf() }

const u = (e) => encodeURIComponent(e)

export async function authProviders() {
  // Public endpoint — used by the login page to choose which
  // affordances to render. Returns `{cognito, okta,
  // okta_display_name}`. No session required.
  const r = await fetch('/auth/providers')
  if (!r.ok) throw new Error('providers fetch failed')
  return r.json()
}

export async function getVersion() {
  try {
    const d = await http('GET', '/api/version')
    return d?.version || null
  } catch { return null }
}

// #1104: reduce a full build version (v1.1.0-ga2c3a69-dirty) to the
// bare release (v1.1.0) for the UI footer. /api/version itself stays
// FULL (the support source of truth) — this is presentation only.
// Mirrors runner.display_version: a release collapses to v1.1.0; a
// bare-SHA or "dev" build (no release token) passes through unchanged.
export function displayVersion(full) {
  if (!full) return full
  const m = /^(v\d+\.\d+\.\d+)(?:-g[0-9a-f]+)?(?:-dirty)?$/.exec(full)
  return m ? m[1] : full
}

export const api = {
  // Identity
  whoami:        ()             => http('GET',    '/api/whoami'),
  listPersonas:  ()             => http('GET',    '/api/dev/personas'),

  // Users (new spec-matching shape, hits api.py)
  listUsers:     (filters = {}) => {
    const qs = new URLSearchParams()
    if (filters.team && filters.team !== '*') qs.set('team', filters.team)
    if (filters.status) qs.set('status', filters.status)
    const q = qs.toString()
    return http('GET', `/api/users${q ? '?' + q : ''}`)
  },
  getUser:       (email)               => http('GET',    `/api/users/${u(email)}`),
  // Verified enforcement state for a governed IDC user ({state,
  // enforced}). Read-only; drives the honest governed-IDC badge
  // (pending vs verified-enforced).
  getIdcEnforcement: (email)           => http('GET',    `/api/users/${u(email)}/idc-enforcement`),
  preregister:   (body)                => http('POST',   '/api/users/preregister', body),
  setCap:        (email, cap, ver)     => http('PUT',    `/api/users/${u(email)}/cap`,
                                                          { cap_usd: cap }, { ifMatch: ver }),
  approve:       (email, ver)          => http('POST',   `/api/users/${u(email)}/approve`,
                                                          {}, { ifMatch: ver }),
  setTeam:       (email, team, ver)    => http('PUT',    `/api/users/${u(email)}/team`,
                                                          { team_id: team }, { ifMatch: ver }),
  setNotes:      (email, notes, ver)   => http('PUT',    `/api/users/${u(email)}/notes`,
                                                          { notes }, { ifMatch: ver }),
  // #750: Disable→Force block; Re-enable→Unblock (clears the
  // manual force-block; the reconciler re-decides against the cap —
  // it does NOT force-allow). The time-boxed temp-unblock
  // (api.unblock/cancelUnblock) is removed; raising the cap is the
  // reprieve path. NOTE: api.unblockServiceAccount (service-account
  // caps) is a DIFFERENT feature and stays.
  forceBlock:    (email, confirm, ver) => http('POST',   `/api/users/${u(email)}/force-block`,
                                                          { confirm_email: confirm }, { ifMatch: ver }),
  unblock:       (email, ver)          => http('POST',   `/api/users/${u(email)}/unblock`,
                                                          {}, { ifMatch: ver }),
  deleteUser:    (email)               => http('DELETE', `/api/users/${u(email)}`),
  // #629/#627/#856: deny-only governance. govern attaches
  // tg-BedrockQuotaDeny to the principal's role (one-time) and
  // marks governed; ungovern reverses (detaches when last on
  // role). The HTTP route paths (/manage, /unmanage) and the
  // users.governed DB column are unchanged — only the client
  // method names track the UI's Govern/Ungovern vocabulary (#856).
  // #629/#625: setDisplayName sets/clears the
  // admin label via PATCH (never touches the ARN-derived caller).
  govern:        (id, ver)             => http('POST',  `/api/users/${u(id)}/manage`,
                                                          {}, { ifMatch: ver }),
  ungovern:      (id, ver)             => http('POST',  `/api/users/${u(id)}/unmanage`,
                                                          {}, { ifMatch: ver }),
  setDisplayName: (id, name, ver)      => http('PATCH', `/api/users/${u(id)}`,
                                                          { display_name: name }, { ifMatch: ver }),
  // Set/clear the IAM-user NAME of a user's Bedrock API key (mantle/
  // Codex CUR attribution). Same PATCH endpoint as display_name but an
  // org-admin-only field; server 409s if the key is already mapped to
  // another user. Pass null / '' to clear.
  setBedrockKeyUser: (id, key, ver)    => http('PATCH', `/api/users/${u(id)}`,
                                                          { bedrock_key_user: key }, { ifMatch: ver }),
  // #946: record an admin-supplied IAM role ARN on a pre-registered /
  // ARN-less principal so Govern is attachable without Bedrock spend.
  // Dedicated admin-only route (NOT the self-service display_name
  // PATCH) — the server validates the role-ARN shape + account.
  setPrincipalArn: (id, arn, ver)      => http('POST', `/api/users/${u(id)}/principal-arn`,
                                                          { principal_arn: arn }, { ifMatch: ver }),

  // Policies, pricing, settings
  getDefaultPolicy:    ()        => http('GET', '/api/policies/default'),
  setDefaultPolicy:    (cap)     => http('PUT', '/api/policies/default', { monthly_cap_usd: cap }),
  getAdminConfig:      ()        => http('GET', '/api/admin/config'),
  setAdminConfig:      (body)    => http('PUT', '/api/admin/config', body),
  // #746 (reverses #630/#626): org-wide blocked-model list
  // (drives the model DENYLIST deny's Resource set). Stores
  // catalog model_ids; empty = allow every model (fail-open).
  getBlockedModels:    ()        => http('GET', '/api/settings/blocked-models'),
  setBlockedModels:    (ids)     => http('PUT', '/api/settings/blocked-models', { blocked_models: ids }),
  // Bedrock invocation-logging (analytics capture) region catalog.
  // GET → {regions:[{region,bucket,enabled,text_on}], updated_at};
  // PUT saves + applies via the Bedrock API, returns {regions, apply}.
  getInvocationLogs:   ()        => http('GET', '/api/settings/invocation-logs'),
  setInvocationLogs:   (regions) => http('PUT', '/api/settings/invocation-logs', { regions }),
  // Runtime SSO login config — SAML IdP connection + editable
  // button label, applied to the live Cognito pool with no redeploy.
  // GET returns {configured, provider_name, metadata_url,
  // has_metadata_xml, email_attribute, idp_signout, sso_button_label,
  // status:{present,on_app_client,error}, registration:{sp_entity_id,
  // acs_url,email_attribute,acs_url_error}}. A label-only PUT (just
  // sso_button_label) skips the Cognito call.
  getSamlSettings:     ()        => http('GET', '/api/settings/saml'),
  setSamlSettings:     (body)    => http('PUT', '/api/settings/saml', body),
  deleteSamlSettings:  ()        => http('DELETE', '/api/settings/saml'),
  // Spend-cap email alerts: the warn threshold (% of cap) + whether
  // an over-cap event emails. GET returns {warn_pct, exceeded};
  // PUT takes {warn_pct?, exceeded?}. Delivery needs a notification
  // transport (SMTP and/or webhook) set in Notifications below.
  getSpendAlerts:      ()        => http('GET', '/api/settings/spend-alerts'),
  setSpendAlerts:      (body)    => http('PUT', '/api/settings/spend-alerts', body),
  // Notification transport: generic SMTP (any provider, incl.
  // SES-as-SMTP) + an optional Slack/webhook announcement. GET never
  // returns the SMTP password or the webhook URL (both bearer
  // secrets) — only smtp_password_configured / webhook_configured
  // booleans. PUT takes any of host/port/username/password/from/tls/
  // alert_webhook_url; a blank password or webhook means keep-existing.
  getNotifications:    ()        => http('GET', '/api/settings/notifications'),
  setNotifications:    (body)    => http('PUT', '/api/settings/notifications', body),
  // #726 (#720 slice 4): the pricing-management client methods
  // (listPricing / pricingPendingCount / setPricing / confirm /
  // repropose / audit) are retired with the auto-pricing pipeline
  // — CUR carries billed spend directly; there's no token→price
  // estimate to manage.
  // #649: governance drift — count drives the Users nav badge;
  // list backs a future drift view.
  governanceDriftCount: ()       => http('GET', '/api/governance/drift-count'),
  governanceDrift:      ()       => http('GET', '/api/governance/drift'),
  // Re-run enforcement for ONE drifted principal (shared reconcile);
  // returns {identity_key, apply:{state,enforced,...}}.
  reapplyGovernance:    (key)    => http('POST', `/api/governance/reapply/${u(key)}`),
  // #726: CUR spend-source health — drives the Settings banner +
  // the data-source/freshness visibility.
  curHealth:            ()       => http('GET', '/api/cur/health'),
  // #737: the spend freshness watermark (max usage_hour in CUR) —
  // the spend pages stamp "spend current as of <ts>" from this.
  curDataThrough:       ()       => http('GET', '/api/cur/data-through'),
  // #346: per-role budgets for service principals.
  listServiceAccountCaps: () =>
    http('GET', '/api/service-account-caps'),
  getServiceAccountCap: (idKey) =>
    http('GET', `/api/service-account-caps/${u(idKey)}`),
  putServiceAccountCap: (idKey, body) =>
    http('PUT', `/api/service-account-caps/${u(idKey)}`, body),
  deleteServiceAccountCap: (idKey) =>
    http('DELETE', `/api/service-account-caps/${u(idKey)}`),
  listServiceAccountAlerts: (idKey, limit = 50) =>
    http(
      'GET',
      `/api/service-account-caps/alerts?identity_key=${u(idKey)}&limit=${limit}`,
    ),
  unblockServiceAccount: (idKey) =>
    http(
      'POST',
      `/api/service-account-caps/unblock?identity_key=${u(idKey)}`,
    ),
  listModelCatalog:    ()        => http('GET', '/api/models/catalog'),

  // Admin roles
  listRoles:           ()        => http('GET',    '/api/admin-roles'),
  // #357: body may include provision_cognito:true to also
  // create the Cognito user (sends invite email). Ignored
  // server-side unless TG_AUTH_PROVIDER=cognito.
  grantRole:           (body)    => http('POST',   '/api/admin-roles', body),
  // #927: "Enable login" for a person on the Users screen —
  // authorizes them in tg (role defaults to 'member') and, when tg
  // owns the directory (Cognito), provisions the user + sends the
  // invite. On an external IdP it only authorizes (they sign in via
  // SSO). Returns {login_enabled, cognito_provisioned, directory,
  // role}. 409 if they already have a login.
  enableLogin:         (email, body) =>
    http('POST', `/api/admin-roles/${u(email)}/enable-login`, body || {}),
  // #357: public — {cognito, okta, okta_display_name,
  // cognito_provisioning}. The Admins panel reads
  // cognito_provisioning to gate the invite checkbox.
  authProviders:       ()        => authProviders(),
  revokeRole:          (email, team, role) =>
    http('DELETE', `/api/admin-roles/${u(email)}${team ? '/' + u(team) : ''}`,
         { role: role || 'team_admin' }),

  // Test alert. Optional channel ('email'|'webhook') picks which
  // transport to exercise (the two "Send test" buttons); defaults
  // email. Returns the soft {sent, reason} probe result.
  testAlert:           (channel)  =>
    http('POST', '/api/settings/alerts/test',
         channel ? { channel } : {}),

  // Jobs
  getJobRuns:          ()        => http('GET',  '/api/jobs'),
  runQuotaSync:        ()        => http('POST', '/api/jobs/run'),
  pauseJobs:           (minutes) => http('POST', '/api/admin/jobs/pause',
                                          { minutes }),
  resumeJobs:          ()        => http('DELETE', '/api/admin/jobs/pause'),

  // Velocity & Cost (#213)
  velocityLeaderboard: (window = '30d', team = null) => {
    const t = (team && team !== '*')
      ? `&team=${encodeURIComponent(team)}` : ''
    return http('GET',
      `/api/velocity/leaderboard?window=${encodeURIComponent(window)}` + t)
  },
  velocitySpeed:       (window, type, team = null) => {
    const t = (team && team !== '*')
      ? `&team=${encodeURIComponent(team)}` : ''
    return http('GET',
      `/api/velocity/speed?window=${encodeURIComponent(window)}` +
      `&type=${encodeURIComponent(type)}` + t)
  },
  velocitySpeedUsers:  (team, window, type) => {
    const t = type ? `&type=${encodeURIComponent(type)}` : ''
    return http('GET',
      `/api/velocity/speed?breakdown=user` +
      `&team=${encodeURIComponent(team)}` +
      `&window=${encodeURIComponent(window)}` + t)
  },
  velocityLeaderboardUsers: (team, window = '30d', type = 'all') => {
    return http('GET',
      `/api/velocity/leaderboard/users` +
      `?team_id=${encodeURIComponent(team)}` +
      `&window=${encodeURIComponent(window)}` +
      `&type=${encodeURIComponent(type)}`)
  },
  getLinkedAccounts:   (email)             => http('GET',
    `/api/users/${u(email)}/linked-accounts`),
  putLinkedAccount:    (email, vendor, body) => http('PUT',
    `/api/users/${u(email)}/linked-accounts/${encodeURIComponent(vendor)}`,
    body),
  deleteLinkedAccount: (email, vendor)     => http('DELETE',
    `/api/users/${u(email)}/linked-accounts/${encodeURIComponent(vendor)}`),

  // GitHub integration (#213)
  ghTokenStatus:       ()                  => http('GET',  '/api/integrations/github/token'),
  ghPutToken:          (token)             => http('PUT',  '/api/integrations/github/token', { token }),
  ghDeleteToken:       ()                  => http('DELETE', '/api/integrations/github/token'),
  ghListRepos:         ()                  => http('GET',  '/api/integrations/github/repos'),
  ghAddRepo:           (repo, team_id)     => http('POST', '/api/integrations/github/repos',
                                                              { repo, team_id: team_id || null }),
  ghUpdateRepo:        (repo, patch)       => http('PATCH',
                                                `/api/integrations/github/repos/${repo}`, patch),
  ghDeleteRepo:        (repo)              => http('DELETE',
                                                `/api/integrations/github/repos/${repo}`),
  ghSync:              (repo)              => http('POST', '/api/integrations/github/sync',
                                                              repo ? { repo } : {}),
  ghGetLabelMap:       ()                  => http('GET',  '/api/integrations/github/label-map'),
  ghPutLabelMap:       (body)              => http('PUT',  '/api/integrations/github/label-map', body),
  ghCoverage:          ()                  => http('GET',
    '/api/integrations/github/classification-coverage'),
  ghPreviewClassification: (body)          => http('POST',
    '/api/integrations/github/preview-classification', body),

  // Jira-aware V&C (#365)
  vcJiraSprints:       (window)            => http('GET',
    '/api/velocity/jira/sprints?window=' + encodeURIComponent(window || '90d')),
  vcJiraEpics:         (window)            => http('GET',
    '/api/velocity/jira/epics?window=' + encodeURIComponent(window || '90d')),
  vcJiraSeries:        (params)            => {
    const qs = Object.entries(params || {})
      .filter(([_, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
      .join('&')
    return http('GET', '/api/velocity/jira/series' + (qs ? '?' + qs : ''))
  },
  vcJiraSprint:        (sprintId)          => http('GET',
    `/api/velocity/jira/sprint/${encodeURIComponent(sprintId)}`),
  vcJiraPrRefs:        (repo, prNumbers)   => {
    const nums = (prNumbers || []).join(',')
    return http('GET',
      `/api/velocity/jira/pr-refs?repo=${encodeURIComponent(repo)}&pr_numbers=${nums}`)
  },

  // Jira integration (#364)
  jiraListSites:       ()                  => http('GET',  '/api/integrations/jira'),
  jiraAddSite:         (body)              => http('POST', '/api/integrations/jira', body),
  jiraUpdateSite:      (id, body)          => http('PATCH', `/api/integrations/jira/${id}`, body),
  jiraDeleteSite:      (id)                => http('DELETE', `/api/integrations/jira/${id}`),
  jiraTestSite:        (id)                => http('POST', `/api/integrations/jira/${id}/test`),
  jiraSyncSite:        (id)                => http('POST', `/api/integrations/jira/${id}/sync-now`),

  runJob:              (job_name)          => http('POST',
    '/api/jobs/run', { job: job_name }),

  // Desktop-only (#132). Returns {ok: true} or
  // {ok: false, reason, detail?, profile?}. Cloud surface 404s
  // — caller must guard on window.__TG_DEPLOYMENT__ === 'desktop'.
  desktopAuthStatus:   ()        => http('GET', '/api/desktop/auth-status'),

  // Cloud login/logout (#131). No-op on desktop.
  logout:              ()        => http('POST', '/auth/logout'),

}

export function fmtUsd(n) {
  if (n == null) return '—'
  if (n < 0.01 && n > 0) return `$${n.toFixed(4)}`
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export function fmtTokens(n) {
  if (n == null) return '—'
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return `${n}`
}

export function pct(used, cap) {
  if (!cap || cap <= 0) return null
  return (used / cap) * 100
}

function withTeam(path, team) {
  if (!team || team === '*') return path
  const sep = path.includes('?') ? '&' : '?'
  return `${path}${sep}team=${encodeURIComponent(team)}`
}

export async function getSummary(team) {
  return http('GET', withTeam('/api/summary', team))
}

export async function getUsage(team) {
  return http('GET', withTeam('/api/usage', team))
}

export async function getAnalyticsQueries() {
  return http('GET', '/api/analytics/queries')
}

export async function runAnalyticsQuery(
  queryId, { refresh = false, start = '', end = '' } = {},
) {
  // start/end are ISO YYYY-MM-DD strings selecting a date window
  // applied across reports; blank = month-to-date (server default).
  // Only sent when non-blank so a default run keeps the bare body.
  const payload = { query_id: queryId, refresh }
  if (start) payload.start = start
  if (end) payload.end = end
  return http('POST', '/api/analytics/run', payload)
}

export async function getTeams() {
  return http('GET', '/api/teams')
}
export async function createTeam(data) {
  return http('POST', '/api/teams', data)
}
export async function updateTeam(teamId, data) {
  return http('PUT', `/api/teams/${encodeURIComponent(teamId)}`, data)
}
export async function deleteTeam(teamId) {
  return http('DELETE', `/api/teams/${encodeURIComponent(teamId)}`)
}
export async function getTeamMembers(teamId) {
  return http('GET', `/api/teams/${encodeURIComponent(teamId)}/members`)
}
export async function addTeamMember(teamId, email) {
  return http('POST', `/api/teams/${encodeURIComponent(teamId)}/members`, { email })
}
export async function removeTeamMember(teamId, email) {
  return http('DELETE', `/api/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(email)}`)
}
export function formatTokens(n) {
  if (n == null) return '—'
  if (n >= 1e9) return `${(n/1e9).toFixed(2)}B`
  if (n >= 1e6) return `${(n/1e6).toFixed(2)}M`
  if (n >= 1e3) return `${(n/1e3).toFixed(1)}K`
  return `${n}`
}
