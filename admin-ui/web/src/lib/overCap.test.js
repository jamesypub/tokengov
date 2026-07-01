import { describe, it, expect } from 'vitest'
import {
  classifyOverCap, notEnforcedReason, notEnforcedTooltip,
} from './overCap'

const fmtUsd = (v) => `$${Number(v || 0).toFixed(2)}`

describe('classifyOverCap — the four over-cap cases', () => {
  it('under cap → null (no badge)', () => {
    expect(classifyOverCap({
      cap_usd: 100, mtd_spend_usd: 20, projected: 25,
      status: 'active', governed: true,
      estimate_enforcement: 'off',
    })).toBeNull()
  })

  it('no cap set → null', () => {
    expect(classifyOverCap({
      cap_usd: null, mtd_spend_usd: 999, status: 'active',
    })).toBeNull()
    expect(classifyOverCap({
      cap_usd: 0, mtd_spend_usd: 999, status: 'active',
    })).toBeNull()
  })

  it('blocked / force_blocked → enforced (existing red badge)', () => {
    expect(classifyOverCap({
      cap_usd: 10, mtd_spend_usd: 50, status: 'blocked',
      governed: true,
    })).toBe('enforced')
    expect(classifyOverCap({
      cap_usd: 10, mtd_spend_usd: 5, status: 'force_blocked',
      governed: true,
    })).toBe('enforced')
  })

  it('governed warn-mode + projected_over_cap → warn (amber)', () => {
    expect(classifyOverCap({
      cap_usd: 10, mtd_spend_usd: 8, projected: 12,
      status: 'active', governed: true,
      estimate_enforcement: 'warn', projected_over_cap: true,
    })).toBe('warn')
  })

  it('managed-but-ungoverned far over cap → not_enforced (the gap)', () => {
    // The reported case: managed:true, governed:false, $666 vs $10 cap,
    // status active, projected_over_cap false → no enforced/warn badge,
    // so it must surface as the new not_enforced signal.
    expect(classifyOverCap({
      cap_usd: 10, mtd_spend_usd: 666.13, projected: 666.13,
      status: 'active', governed: false,
      estimate_enforcement: 'warn', projected_over_cap: false,
    })).toBe('not_enforced')
  })

  it('governed but enforcement OFF and over cap → not_enforced', () => {
    // A governed user billed over cap with enforcement off: no deny yet,
    // no warn badge → still "not enforced" (the cap isn't being enforced
    // right now). Treating this as not-enforced is intentional.
    expect(classifyOverCap({
      cap_usd: 10, mtd_spend_usd: 40, projected: 40,
      status: 'active', governed: true,
      estimate_enforcement: 'off',
    })).toBe('not_enforced')
  })

  it('over only on projection (billed under cap), ungoverned → not_enforced', () => {
    expect(classifyOverCap({
      cap_usd: 10, mtd_spend_usd: 8, projected: 15,
      status: 'active', governed: false,
      estimate_enforcement: 'off',
    })).toBe('not_enforced')
  })

  it('enforced takes precedence over a would-be not_enforced', () => {
    // blocked always wins — never double-signal.
    expect(classifyOverCap({
      cap_usd: 10, mtd_spend_usd: 666, status: 'blocked',
      governed: false,
    })).toBe('enforced')
  })
})

describe('notEnforcedReason / tooltip', () => {
  it('ungoverned names the principal as ungoverned + Govern action', () => {
    const row = { cap_usd: 10, mtd_spend_usd: 666, governed: false }
    expect(notEnforcedReason(row)).toMatch(/ungoverned/)
    const tip = notEnforcedTooltip(row, fmtUsd)
    expect(tip).toContain('$666.00')
    expect(tip).toContain('$10.00')
    expect(tip).toMatch(/ungoverned/)
    expect(tip).toMatch(/Govern the user/)
  })

  it('governed+enforcement-off names enforcement off + the enforcement action', () => {
    const row = { cap_usd: 10, mtd_spend_usd: 40, governed: true }
    expect(notEnforcedReason(row)).toMatch(/enforcement is off/)
    const tip = notEnforcedTooltip(row, fmtUsd)
    expect(tip).toMatch(/enforcement is off/)
    expect(tip).toMatch(/Turn on estimate enforcement/)
  })
})
