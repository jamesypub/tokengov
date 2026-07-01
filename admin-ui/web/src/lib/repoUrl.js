// Repo-reference normalizer (#1042) — JS mirror of
// container/api/repo_url.py. Keep the two grammars in sync.
//
// URL-first: accept a full URL, the SCP-SSH short form, or the
// owner/name shorthand and reduce them to a canonical {host, path}.
// Host is first-class so the model can grow beyond github.com later
// (self-hosted GitLab, nested subgroups) without re-modelling.
//
// Returns { ok: true, host, path, canonical, isGithub } on success, or
// { ok: false, error: '<specific reason>' } — the caller shows the
// error inline and keeps the submit button enabled (no silent-disabled).

export const GITHUB_HOST = 'github.com'
const ALLOWED_SCHEMES = ['https', 'http', 'ssh', 'git']
const MAX_SEGMENTS = 20 // GitLab subgroup nesting limit
const GITHUB_ROUTE_WORDS = new Set([
  'tree', 'blob', 'pull', 'pulls', 'commit', 'commits', 'releases',
  'tags', 'branches', 'issues', 'wiki', 'actions', 'settings',
  'compare', 'graphs', 'network', 'pulse', 'projects',
])
const SEG_RE = /^[\w.-]+$/

function cleanHost(netloc) {
  let h = netloc
  if (h.includes('@')) h = h.slice(h.lastIndexOf('@') + 1)
  if (h.includes(':')) h = h.split(':', 1)[0]
  return h.trim().toLowerCase()
}

export function normalizeRepo(raw) {
  if (raw == null) return { ok: false, error: 'Enter a repository URL or owner/name' }
  const s = String(raw).trim()
  if (!s) return { ok: false, error: 'Enter a repository URL or owner/name' }
  if (/\s/.test(s)) {
    return { ok: false, error: "Repository reference can't contain spaces" }
  }

  let host = GITHUB_HOST
  let path = s

  const colon = s.indexOf(':')
  const slash = s.indexOf('/')
  if (
    s.includes('@') &&
    colon !== -1 &&
    (slash === -1 || slash > colon) &&
    !s.includes('://')
  ) {
    // 1. SCP-SSH git@host:group/proj.git
    const userhost = s.slice(0, colon)
    host = cleanHost(userhost)
    path = s.slice(colon + 1)
  } else if (s.includes('://')) {
    // 2. Absolute URL scheme://host/path
    let u
    try {
      u = new URL(s)
    } catch {
      return { ok: false, error: 'Could not parse that URL' }
    }
    const scheme = u.protocol.replace(/:$/, '').toLowerCase()
    if (!ALLOWED_SCHEMES.includes(scheme)) {
      return { ok: false, error: `Unsupported URL scheme '${scheme}' (use https, ssh or git)` }
    }
    if (!u.host) return { ok: false, error: 'Could not parse that URL' }
    host = cleanHost(u.host)
    path = u.pathname
  }
  // 3. Shorthand owner/name — host stays github.com, path = s.

  if (!host) return { ok: false, error: 'Could not parse that URL' }

  // GitLab `/-/` separates project path from route.
  if (path.includes('/-/')) path = path.split('/-/')[0]
  path = path.replace(/^\/+|\/+$/g, '')
  if (path.endsWith('.git')) path = path.slice(0, -4)

  let segs = path.split('/').filter(Boolean)

  // GitHub suffix routes follow owner/name; clip at the first one.
  if (host === GITHUB_HOST && segs.length > 2) {
    for (let i = 0; i < segs.length; i++) {
      if (i >= 2 && GITHUB_ROUTE_WORDS.has(segs[i].toLowerCase())) {
        segs = segs.slice(0, i)
        break
      }
    }
  }

  if (segs.length < 2) {
    return { ok: false, error: 'Need at least owner/name (two path segments)' }
  }
  if (segs.length > MAX_SEGMENTS) {
    return { ok: false, error: `Path is too deep (max ${MAX_SEGMENTS} segments)` }
  }
  for (const seg of segs) {
    if (!SEG_RE.test(seg)) {
      return { ok: false, error: `Invalid character in path segment '${seg}'` }
    }
  }
  if (!SEG_RE.test(host)) {
    return { ok: false, error: 'Invalid host in repository reference' }
  }

  const normPath = segs.join('/')
  return {
    ok: true,
    host,
    path: normPath,
    canonical: `${host}/${normPath}`,
    isGithub: host === GITHUB_HOST,
  }
}
