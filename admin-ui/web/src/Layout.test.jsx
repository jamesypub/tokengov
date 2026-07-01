import { describe, it, expect } from 'vitest'
import { firstNavPath, roleOf, itemVisible } from './Layout'

// #651: Users is the first menu item and admins land on it at
// login. firstNavPath resolves the login-landing route to the
// first nav item visible to the role, in menu order.

describe('firstNavPath (#651/#1056)', () => {
  it('org_admin lands on Users (the first item)', () => {
    expect(firstNavPath('org_admin')).toBe('/users')
  })
  it('team_admin lands on Users', () => {
    expect(firstNavPath('team_admin')).toBe('/users')
  })
  it('member (V&C flag off) falls through to Activity, not V&C', () => {
    // /users is admin-only; /velocity-cost is now flag-gated and OFF
    // by default (#1056), so the member lands on Activity (the next
    // ungated, unflagged item) — NOT a hidden V&C page.
    expect(firstNavPath('member')).toBe('/activity')
    expect(firstNavPath('member', {})).toBe('/activity')
  })
  it('member with vc_enabled ON sees V&C as the fallback', () => {
    expect(firstNavPath('member', { vc_enabled: true }))
      .toBe('/velocity-cost')
  })
})

describe('roleOf', () => {
  it('org_admin flag wins', () => {
    expect(roleOf({ org_admin: true }, false)).toBe('org_admin')
  })
  it('null while loading', () => {
    expect(roleOf(null, true)).toBeNull()
  })
  it('no me → member', () => {
    expect(roleOf(null, false)).toBe('member')
  })
})

describe('itemVisible', () => {
  it('ungated item is visible to everyone', () => {
    expect(itemVisible({ path: '/activity' }, 'member')).toBe(true)
  })
  it('gated item hidden from member', () => {
    expect(itemVisible({ roles: ['org_admin'] }, 'member'))
      .toBe(false)
  })
  // #1056: flag axis — a flag-gated item is hidden unless the flag
  // is truthy, independent of role.
  it('flag-gated item hidden when flag off / absent', () => {
    const vc = { path: '/velocity-cost', flag: 'vc_enabled' }
    expect(itemVisible(vc, 'org_admin')).toBe(false)        // no flags
    expect(itemVisible(vc, 'org_admin', {})).toBe(false)    // flag absent
    expect(itemVisible(vc, 'org_admin', { vc_enabled: false }))
      .toBe(false)
  })
  it('flag-gated item shown when flag on', () => {
    const vc = { path: '/velocity-cost', flag: 'vc_enabled' }
    expect(itemVisible(vc, 'member', { vc_enabled: true })).toBe(true)
    expect(itemVisible(vc, 'org_admin', { vc_enabled: true })).toBe(true)
  })
})
