/**
 * Tests for src/api.js — verifies the client constructs requests correctly
 * (URLs, headers, If-Match, etc.) without hitting any real backend.
 *
 * Uses the desktop deployment flag (set in test-setup.js) to bypass
 * the cloud-only CSRF-prime fetch — these tests cover URL/body shape,
 * not CSRF wiring (which has its own coverage in test_csrf.py).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

// Set BEFORE importing api.js so the IS_DESKTOP module-load
// constant resolves to true.
window.__TG_DEPLOYMENT__ = 'desktop'

const { api, displayVersion } = await import('./api')

let fetchMock

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    text: async () => JSON.stringify({ ok: true }),
  })
  global.fetch = fetchMock
})

describe('api client URLs', () => {
  it('whoami → GET /api/whoami', async () => {
    await api.whoami()
    expect(fetchMock).toHaveBeenCalledWith('/api/whoami', expect.objectContaining({ method: 'GET' }))
  })

  it('listUsers with no filters → /api/users', async () => {
    await api.listUsers()
    expect(fetchMock.mock.calls[0][0]).toBe('/api/users')
  })

  it('listUsers with filters → /api/users?team=eng&status=blocked', async () => {
    await api.listUsers({ team: 'eng', status: 'blocked' })
    const url = fetchMock.mock.calls[0][0]
    expect(url).toContain('team=eng')
    expect(url).toContain('status=blocked')
  })

  it('preregister sends JSON body', async () => {
    await api.preregister({ email: 'x@y.com', cap_usd: 50 })
    const opts = fetchMock.mock.calls[0][1]
    expect(opts.method).toBe('POST')
    expect(opts.headers['Content-Type']).toBe('application/json')
    expect(JSON.parse(opts.body)).toEqual({ email: 'x@y.com', cap_usd: 50 })
  })

  it('setCap with version sends If-Match header', async () => {
    await api.setCap('alice@example.com', 100, 7)
    const opts = fetchMock.mock.calls[0][1]
    expect(opts.headers['If-Match']).toBe('7')
  })

  it('setCap with null cap sends cap_usd: null', async () => {
    await api.setCap('alice@example.com', null, 0)
    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body.cap_usd).toBeNull()
  })

  it('forceBlock URL-encodes the email + hits /force-block', async () => {
    // #750: disable → forceBlock.
    await api.forceBlock('alice+work@example.com', 'alice+work@example.com', 1)
    const url = fetchMock.mock.calls[0][0]
    expect(url).toContain('alice%2Bwork%40example.com')
    expect(url).toContain('/force-block')
  })

  it('unblock POSTs to /unblock with no body (cap-respecting)', async () => {
    // #750: unblock clears the manual block only — no temp-unblock body.
    await api.unblock('alice@example.com', 1)
    const url = fetchMock.mock.calls[0][0]
    const opts = fetchMock.mock.calls[0][1]
    expect(url).toContain('/unblock')
    expect(opts.method).toBe('POST')
  })

  it('approve POST with no body has empty object', async () => {
    await api.approve('alice@example.com', 0)
    const opts = fetchMock.mock.calls[0][1]
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({})
  })

  it('throws Error on non-OK response with status code attached', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 409,
      statusText: 'Conflict',
      text: async () => JSON.stringify({ error: 'Version mismatch' }),
    })
    await expect(api.setCap('a@b.c', 1, 0)).rejects.toMatchObject({
      message: 'Version mismatch',
      status: 409,
    })
  })

  it('throws Error with status text fallback when no body', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      text: async () => '',
    })
    await expect(api.whoami()).rejects.toMatchObject({
      message: '500 Internal Server Error',
      status: 500,
    })
  })

  // #433: structured FastAPI `detail` (object) — analytics 503s.
  it('unwraps object detail {code,detail} (not [object Object])', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 503,
      statusText: 'Service Unavailable',
      text: async () => JSON.stringify({
        detail: {
          code: 'api_runner_not_configured',
          detail: 'TG_API_RUNNER_ROLE_ARN is not set.',
        },
      }),
    })
    await expect(api.whoami()).rejects.toMatchObject({
      message: 'TG_API_RUNNER_ROLE_ARN is not set.',
      code: 'api_runner_not_configured',
      status: 503,
    })
  })

  // Top-level code + string detail (central handler shape).
  it('reads top-level code with string detail', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 503,
      statusText: 'Service Unavailable',
      text: async () => JSON.stringify({
        detail: 'Container AWS credentials expired.',
        code: 'creds_expired',
      }),
    })
    await expect(api.whoami()).rejects.toMatchObject({
      message: 'Container AWS credentials expired.',
      code: 'creds_expired',
      status: 503,
    })
  })

  // #357: Cognito provisioning contract.
  it('grantRole without provision_cognito → plain body', async () => {
    await api.grantRole({ email: 'a@b.com', role: 'org_admin' })
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/admin-roles')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({
      email: 'a@b.com', role: 'org_admin',
    })
  })

  it('grantRole passes provision_cognito through verbatim', async () => {
    await api.grantRole({
      email: 'a@b.com', role: 'org_admin', provision_cognito: true,
    })
    const opts = fetchMock.mock.calls[0][1]
    expect(JSON.parse(opts.body)).toEqual({
      email: 'a@b.com', role: 'org_admin', provision_cognito: true,
    })
  })

  it('authProviders → GET /auth/providers, returns json', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        cognito: true, okta: false,
        cognito_provisioning: true,
      }),
    })
    const r = await api.authProviders()
    expect(fetchMock.mock.calls[0][0]).toBe('/auth/providers')
    expect(r.cognito_provisioning).toBe(true)
  })
})

// #1104: displayVersion reduces a full build version to the bare
// release (v1.1.0) for the UI footer; /api/version stays full. Mirrors
// runner.display_version (Python) — same cases.
describe('displayVersion (#1104)', () => {
  it('collapses a full release string to the bare release', () => {
    expect(displayVersion('v1.1.0-ga2c3a69-dirty')).toBe('v1.1.0')
    expect(displayVersion('v1.1.0-ga2c3a69')).toBe('v1.1.0')
    expect(displayVersion('v1.1.0')).toBe('v1.1.0')
  })
  it('passes through a bare SHA / dev build (no release to collapse)', () => {
    expect(displayVersion('a2c3a69')).toBe('a2c3a69')
    expect(displayVersion('a2c3a69-dirty')).toBe('a2c3a69-dirty')
    expect(displayVersion('dev')).toBe('dev')
  })
  it('is null/empty-safe', () => {
    expect(displayVersion(null)).toBe(null)
    expect(displayVersion('')).toBe('')
  })
})
