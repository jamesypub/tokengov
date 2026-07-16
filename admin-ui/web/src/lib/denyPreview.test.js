import { describe, it, expect } from 'vitest'
import {
  INVOKE_ACTIONS, agnosticToken, blockedResources,
  denyPreviewStatement, denyPreviewDocument,
} from './denyPreview'

const SONNET = 'us.anthropic.claude-sonnet-4-6'
const OPUS_GLOBAL = 'global.anthropic.claude-opus-4-8'

describe('agnosticToken', () => {
  it('strips the us. / global. / apac. / eu. CRIS prefix', () => {
    expect(agnosticToken(SONNET)).toBe('anthropic.claude-sonnet-4-6')
    expect(agnosticToken(OPUS_GLOBAL)).toBe('anthropic.claude-opus-4-8')
    expect(agnosticToken('apac.anthropic.claude-x'))
      .toBe('anthropic.claude-x')
  })
  it('leaves a bare foundation-model id unchanged', () => {
    expect(agnosticToken('anthropic.claude-v2'))
      .toBe('anthropic.claude-v2')
  })
  it('us.* and global.* of the same model reduce to one token', () => {
    expect(agnosticToken('us.anthropic.claude-opus-4-8'))
      .toBe(agnosticToken(OPUS_GLOBAL))
  })
})

describe('blockedResources', () => {
  it('emits both resource types per model on the agnostic token', () => {
    expect(blockedResources([SONNET])).toEqual([
      'arn:aws:bedrock:*:*:inference-profile/*anthropic.claude-sonnet-4-6*',
      'arn:aws:bedrock:*::foundation-model/*anthropic.claude-sonnet-4-6*',
    ])
  })
  it('dedupes (us.* and global.* of one model collapse)', () => {
    const res = blockedResources([
      'us.anthropic.claude-opus-4-8', OPUS_GLOBAL,
    ])
    expect(res).toHaveLength(2)  // one token → 2 resource types
  })
})

describe('denyPreviewStatement', () => {
  it('returns null for an empty / whitespace-only list', () => {
    expect(denyPreviewStatement([])).toBeNull()
    expect(denyPreviewStatement(null)).toBeNull()
    expect(denyPreviewStatement(['  '])).toBeNull()
  })

  it('emits a Deny with Resource = blocked wildcards (denylist)', () => {
    const stmt = denyPreviewStatement([SONNET])
    expect(stmt.Sid).toBe('DenyBlockedModels')
    expect(stmt.Effect).toBe('Deny')
    expect(stmt.Resource).toEqual(blockedResources([SONNET]))
    // Resource is the mechanism — NotResource (allowlist) must be absent.
    expect(stmt.NotResource).toBeUndefined()
  })

  it('lists Converse + ConverseStream explicitly (AWS gotcha)', () => {
    const stmt = denyPreviewStatement([SONNET])
    expect(stmt.Action).toEqual(INVOKE_ACTIONS)
    expect(stmt.Action).toContain('bedrock:Converse')
    expect(stmt.Action).toContain('bedrock:ConverseStream')
  })
})

describe('denyPreviewDocument', () => {
  it('wraps the statement in a policy doc, or null when empty', () => {
    expect(denyPreviewDocument([])).toBeNull()
    const doc = denyPreviewDocument([SONNET])
    expect(doc.Version).toBe('2012-10-17')
    expect(doc.Statement).toHaveLength(1)
    expect(doc.Statement[0].Sid).toBe('DenyBlockedModels')
  })
})
