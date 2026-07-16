/**
 * Cloud-mode CSRF tests for src/api.js (#131).
 *
 * Verifies:
 *  - mutating cloud requests prime CSRF via GET /api/csrf
 *  - the X-CSRF-Token header is attached on the followup request
 *  - 401s on cloud trigger a redirect to /login
 *  - desktop mode does NOT touch /api/csrf or attach the header
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

// Cloud mode: deployment flag absent.
delete window.__TG_DEPLOYMENT__

let fetchMock
let originalLocation

beforeEach(async () => {
  // Per-test fresh module so the in-module CSRF cache doesn't
  // leak across tests.
  vi.resetModules()
  fetchMock = vi.fn()
  global.fetch = fetchMock
  originalLocation = window.location
  // Make assignment to .href observable.
  delete window.location
  window.location = { ...originalLocation, href: '/' }
  // Clear any leftover cookie.
  document.cookie =
    'tg_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
})

describe('CSRF wiring (cloud)', () => {
  it('primes CSRF then attaches header on POST', async () => {
    fetchMock
      // GET /api/csrf
      .mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify({ csrf_token: 'TOK' }),
        json: async () => ({ csrf_token: 'TOK' }),
      })
      // POST /api/users
      .mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify({ ok: true }),
      })

    const { api } = await import('./api')
    await api.preregister({ email: 'x@y.com' })

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/csrf')
    const postOpts = fetchMock.mock.calls[1][1]
    expect(postOpts.method).toBe('POST')
    expect(postOpts.headers['X-CSRF-Token']).toBe('TOK')
  })

  it('does NOT prime CSRF for GET', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      text: async () => JSON.stringify({ users: [] }),
    })
    const { api } = await import('./api')
    await api.listUsers()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/users')
  })

  it('redirects to /login on 401', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false, status: 401,
      statusText: 'Unauthorized',
      text: async () => JSON.stringify({ detail: 'no auth' }),
    })
    const { api } = await import('./api')
    await expect(api.whoami()).rejects.toThrow()
    expect(window.location.href).toContain('/login')
  })
})

describe('CSRF wiring (desktop)', () => {
  it('does NOT prime CSRF or attach header in desktop mode',
      async () => {
    window.__TG_DEPLOYMENT__ = 'desktop'
    fetchMock.mockResolvedValueOnce({
      ok: true,
      text: async () => JSON.stringify({ ok: true }),
    })
    const { api } = await import('./api')
    await api.preregister({ email: 'x@y.com' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/users/preregister')
    const opts = fetchMock.mock.calls[0][1]
    expect(opts.headers['X-CSRF-Token']).toBeUndefined()
    delete window.__TG_DEPLOYMENT__
  })
})
