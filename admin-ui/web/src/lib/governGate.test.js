import { describe, it, expect } from 'vitest'
import {
  ROLE_ARN_RE, isRoleArn, isIdcRoleArn, arnAccountOf,
  NO_ROLE_ARN_REASON, AWAITING_PRINCIPAL_CHIP,
  PREREGISTER_NOTICE_BODY, isAwaitingPrincipal,
  IDC_GOVERN_NOTICE_BODY, IDC_GOVERNED_PENDING_NOTE,
  IDC_GOVERNED_ENFORCED_NOTE, IDC_REMEDIATION_BODY,
  IDC_REMEDIATION_SUMMARY,
} from './governGate'

describe('isRoleArn (#946 — mirrors server _role_name_from_arn)', () => {
  it('accepts an assumed-role role ARN', () => {
    expect(isRoleArn('arn:aws:iam::123456789012:role/tg-consumer'))
      .toBe(true)
  })
  it('trims whitespace before matching', () => {
    expect(isRoleArn('  arn:aws:iam::1:role/x  ')).toBe(true)
  })
  it('rejects an IAM-user ARN', () => {
    expect(isRoleArn('arn:aws:iam::1:user/bob')).toBe(false)
  })
  it('rejects root', () => {
    expect(isRoleArn('arn:aws:iam::1:root')).toBe(false)
  })
  it('rejects empty / null', () => {
    expect(isRoleArn('')).toBe(false)
    expect(isRoleArn(null)).toBe(false)
  })
  it('ROLE_ARN_RE is the exported source the page imports', () => {
    expect(ROLE_ARN_RE.test('arn:aws:iam::9:role/r')).toBe(true)
  })
})

describe('isIdcRoleArn', () => {
  it('flags an AWSReservedSSO_* role ARN', () => {
    expect(isIdcRoleArn(
      'arn:aws:iam::1:role/aws-reserved/sso.amazonaws.com/'
      + 'AWSReservedSSO_Dev_abc')).toBe(true)
  })
  it('flags a bare AWSReservedSSO_ role name', () => {
    expect(isIdcRoleArn('arn:aws:iam::1:role/AWSReservedSSO_X'))
      .toBe(true)
  })
  it('does not flag a normal role', () => {
    expect(isIdcRoleArn('arn:aws:iam::1:role/tg-consumer'))
      .toBe(false)
  })
})

describe('arnAccountOf', () => {
  it('pulls the account segment', () => {
    expect(arnAccountOf('arn:aws:iam::123456789012:role/x'))
      .toBe('123456789012')
  })
  it('empty when not an iam ARN', () => {
    expect(arnAccountOf('not-an-arn')).toBe('')
    expect(arnAccountOf(null)).toBe('')
  })
})

describe('isAwaitingPrincipal (#946)', () => {
  it('true for an ARN-less non-IDC human (pre-registered)', () => {
    expect(isAwaitingPrincipal({
      email: 'new@x.com', principal_arn: null, role_type: 'iam',
    })).toBe(true)
  })
  it('false once a role ARN is present', () => {
    expect(isAwaitingPrincipal({
      principal_arn: 'arn:aws:iam::1:role/tg-consumer',
    })).toBe(false)
  })
  it('false for IDC (never governable, not "awaiting")', () => {
    expect(isAwaitingPrincipal({
      principal_arn: null, role_type: 'idc',
    })).toBe(false)
  })
  it('false for a service principal', () => {
    expect(isAwaitingPrincipal({
      principal_arn: null, is_service: true,
    })).toBe(false)
  })
  it('false for null user (no crash)', () => {
    expect(isAwaitingPrincipal(null)).toBe(false)
  })
})

describe('shared copy (#946 — one string, no drift)', () => {
  it('names the ARN blocker, not spend', () => {
    expect(NO_ROLE_ARN_REASON).toMatch(/no IAM role ARN/)
    expect(NO_ROLE_ARN_REASON).not.toMatch(/invoke a model/)
  })
  it('chip tooltip IS the reason copy (they share)', () => {
    expect(AWAITING_PRINCIPAL_CHIP.title).toBe(NO_ROLE_ARN_REASON)
  })
  it('pre-register notice offers both paths, neutral tone', () => {
    expect(PREREGISTER_NOTICE_BODY).toMatch(/Add the role ARN now/)
    expect(PREREGISTER_NOTICE_BODY).toMatch(/first Bedrock activity/)
  })
})

// The governed-IDC copy must tell the truth about enforcement
// (pending, not "enforced") AND obey the owner's UI-copy directive —
// plain business language, no internal tg-* names / script filenames /
// mechanism internals in the default (non-disclosure) copy.
describe('IDC honest-enforcement copy', () => {
  const DEFAULT_COPY = [
    IDC_GOVERN_NOTICE_BODY,
    IDC_GOVERNED_PENDING_NOTE,
    IDC_GOVERNED_ENFORCED_NOTE,
  ]

  it('the false "enforced via" claim is gone from the governed note', () => {
    expect(IDC_GOVERNED_PENDING_NOTE).not.toMatch(/enforced via/i)
    // pending note states intent + not-active + who acts
    expect(IDC_GOVERNED_PENDING_NOTE).toMatch(/not yet active/i)
    expect(IDC_GOVERNED_PENDING_NOTE).toMatch(/identity administrator/i)
  })

  it('enforced note claims active only (shown when tg-verified)', () => {
    expect(IDC_GOVERNED_ENFORCED_NOTE).toMatch(/active|enforced/i)
  })

  it('no internal tg-* / script names in the DEFAULT copy', () => {
    const bad = /tg-BedrockQuotaDeny|tg-consumer|tg-QuotaDenyPermissionSet|tg-idc-quota-permset|AWSReservedSSO|permission-set policy reference|management account|deny statement/i
    for (const s of DEFAULT_COPY) {
      expect(s).not.toMatch(bad)
    }
  })

  it('the technical remediation lives behind a disclosure, plain-worded', () => {
    expect(IDC_REMEDIATION_SUMMARY).toMatch(/finish enabling/i)
    // the disclosure describes the step in words, still no raw tg-* names
    expect(IDC_REMEDIATION_BODY).toMatch(/permission set/i)
    expect(IDC_REMEDIATION_BODY).not.toMatch(/tg-BedrockQuotaDeny|tg-consumer|tg-QuotaDenyPermissionSet|tg-idc-quota-permset\.sh/)
  })
})
