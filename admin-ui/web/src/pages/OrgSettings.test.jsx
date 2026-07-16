import { describe, it, expect } from 'vitest'
import {
  buildOrgNotices, PAGE_GROUPS, authMethodSaveState, makeProviderName,
} from './OrgSettings'

// #744/#746: buildOrgNotices is a pure derivation (like Users.jsx's
// governanceState) over data the page already loads. The static
// precondition note is always present, so count is never < 1;
// dynamic notices appear only when their condition holds. The badge
// count is just notices.length. #746: the model notice flipped from
// "no approved-model allow-list (warn)" to "all models allowed / no
// block-list (info)". A non-empty `blockedModels` suppresses it.

const idcUser = { role_type: 'idc' }
const iamUser = { role_type: 'iam' }
const noTypeUser = {} // defaults to 'iam'
const BLOCKED = ['us.anthropic.claude-x'] // non-empty → no model notice

describe('buildOrgNotices — static note', () => {
  it('always includes the grant-outside-tg static note', () => {
    const n = buildOrgNotices({ blockedModels: BLOCKED })
    const ids = n.map(x => x.id)
    expect(ids).toContain('grant-outside-tg')
    expect(n.find(x => x.id === 'grant-outside-tg').kind).toBe('static')
  })

  it('count is never below 1 (static keeps the area present)', () => {
    // Everything healthy / no IDC / no new models / block-list set.
    const n = buildOrgNotices({
      users: [iamUser],
      blockedModels: BLOCKED,
      curHealth: { status: 'healthy' },
      newModels: [],
    })
    expect(n.length).toBe(1)
    expect(n[0].id).toBe('grant-outside-tg')
  })
})

describe('buildOrgNotices — IDC dynamic notice', () => {
  it('appears only when an IDC principal is present', () => {
    const without = buildOrgNotices({
      users: [iamUser, noTypeUser], blockedModels: BLOCKED,
    })
    expect(without.map(x => x.id)).not.toContain('idc-not-manageable')

    const withIdc = buildOrgNotices({
      users: [iamUser, idcUser], blockedModels: BLOCKED,
    })
    const idc = withIdc.find(x => x.id === 'idc-not-manageable')
    expect(idc).toBeTruthy()
    expect(idc.kind).toBe('dynamic')
    expect(idc.tone).toBe('warn')
    expect(idc.title).toContain('1 IDC principal ')
  })

  it('pluralizes the IDC count', () => {
    const n = buildOrgNotices({
      users: [idcUser, idcUser, iamUser], blockedModels: BLOCKED,
    })
    expect(n.find(x => x.id === 'idc-not-manageable').title)
      .toContain('2 IDC principals')
  })
})

describe('buildOrgNotices — no-blocked-models dynamic notice', () => {
  it('appears (info) when the block-list is empty (fail-open)', () => {
    const n = buildOrgNotices({ blockedModels: [] })
    const note = n.find(x => x.id === 'no-blocked-models')
    expect(note).toBeTruthy()
    // It's the intended default → info, not a warning.
    expect(note.tone).toBe('info')
  })
  it('is absent when a block-list is configured', () => {
    const n = buildOrgNotices({ blockedModels: BLOCKED })
    expect(n.map(x => x.id)).not.toContain('no-blocked-models')
  })
})

describe('buildOrgNotices — CUR health dynamic notice', () => {
  it('appears when CUR is unhealthy, with its detail as the body', () => {
    const n = buildOrgNotices({
      blockedModels: BLOCKED,
      curHealth: { status: 'degraded', detail: 'no delivery in 48h' },
    })
    const c = n.find(x => x.id === 'cur-attention')
    expect(c).toBeTruthy()
    expect(c.body).toBe('no delivery in 48h')
  })
  it('is absent when CUR is healthy or unknown', () => {
    expect(buildOrgNotices({ blockedModels: BLOCKED, curHealth: { status: 'healthy' } })
      .map(x => x.id)).not.toContain('cur-attention')
    expect(buildOrgNotices({ blockedModels: BLOCKED, curHealth: null })
      .map(x => x.id)).not.toContain('cur-attention')
  })
})

describe('buildOrgNotices — newly-seen models dynamic notice', () => {
  it('appears (counted) only when newModels is non-empty', () => {
    const without = buildOrgNotices({ blockedModels: BLOCKED, newModels: [] })
    expect(without.map(x => x.id)).not.toContain('newly-seen-models')

    const withNm = buildOrgNotices({
      blockedModels: BLOCKED,
      newModels: [{ model_id: 'a' }, { model_id: 'b' }],
    })
    const nm = withNm.find(x => x.id === 'newly-seen-models')
    expect(nm).toBeTruthy()
    expect(nm.title).toContain('2 newly-seen models')
  })
})

describe('buildOrgNotices — defaults', () => {
  it('handles being called with no args (all defaults)', () => {
    const n = buildOrgNotices()
    // static + no-blocked-models (blockedModels defaults to []).
    const ids = n.map(x => x.id)
    expect(ids).toContain('grant-outside-tg')
    expect(ids).toContain('no-blocked-models')
    expect(ids).not.toContain('idc-not-manageable')
    expect(ids).not.toContain('newly-seen-models')
  })
})

// Blocked models sits directly under Org default quota in the
// Governance group. PAGE_GROUPS is the single ordered source for BOTH
// the rendered section order and the "On this page" index, so
// asserting it here guards the reorder for both surfaces.
describe('PAGE_GROUPS — Governance order', () => {
  const gov = PAGE_GROUPS.find(g => g.group === 'Governance')
  const ids = gov.items.map(i => i.id)

  it('puts Blocked models immediately after Org default quota', () => {
    const q = ids.indexOf('sec-quota')
    const b = ids.indexOf('sec-blocked-models')
    expect(q).toBeGreaterThanOrEqual(0)
    expect(b).toBe(q + 1)
  })

  it('keeps Notifications after Blocked models', () => {
    expect(ids.indexOf('sec-notifications'))
      .toBeGreaterThan(ids.indexOf('sec-blocked-models'))
  })
})

// The save-gated Authentication picker decision: the radio selection
// vs the persisted method. A change is "dirty" (shows the save bar);
// only turning SSO OFF is "destructive" (needs the remove-SSO confirm).
describe('authMethodSaveState — Settings dirty/confirm flow', () => {
  it('no change → not dirty, not destructive', () => {
    expect(authMethodSaveState(false, false))
      .toEqual({ dirty: false, destructive: false })
    expect(authMethodSaveState(true, true))
      .toEqual({ dirty: false, destructive: false })
  })

  it('enabling SSO (password → SAML) → dirty, NOT destructive', () => {
    // Benign direction: no confirm dialog.
    expect(authMethodSaveState(true, false))
      .toEqual({ dirty: true, destructive: false })
  })

  it('removing SSO (SAML → password) → dirty AND destructive', () => {
    // The only direction that opens the remove-SSO confirm.
    expect(authMethodSaveState(false, true))
      .toEqual({ dirty: true, destructive: true })
  })
})

// The Cognito Provider name is internal + tg-owned — no admin input in
// Settings; tg always generates it on save. It must read as tg-owned and
// satisfy Cognito's ProviderName constraints (1-32 chars, no spaces).
describe('makeProviderName — internal tg-owned generator', () => {
  it('uses the tg-cognito-saml prefix, ≤32 chars, no spaces', () => {
    const name = makeProviderName('My Company SSO')
    expect(name.startsWith('tg-cognito-saml-')).toBe(true)
    expect(name.length).toBeLessThanOrEqual(32)
    expect(name).not.toMatch(/\s/)
  })

  it('never leaks the old CompanySso default, even with an empty label', () => {
    const name = makeProviderName('')
    expect(name.startsWith('tg-cognito-saml-')).toBe(true)
    expect(name).not.toContain('CompanySso')
  })

  it('is deterministic for a given label (stable across re-renders)', () => {
    expect(makeProviderName('Acme')).toBe(makeProviderName('Acme'))
  })
})
