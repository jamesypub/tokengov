import { describe, it, expect } from 'vitest'
import { normalizeRepo } from './repoUrl'

// Mirror of container/tests/test_repo_url.py — keep the case tables in
// sync when either grammar changes.

describe('normalizeRepo — accepted forms', () => {
  const ok = [
    ['https://github.com/NVIDIA/SkillSpector', 'github.com', 'NVIDIA/SkillSpector'],
    ['https://github.com/NVIDIA/SkillSpector.git', 'github.com', 'NVIDIA/SkillSpector'],
    ['https://github.com/NVIDIA/SkillSpector/', 'github.com', 'NVIDIA/SkillSpector'],
    ['https://github.com/NVIDIA/SkillSpector/tree/main', 'github.com', 'NVIDIA/SkillSpector'],
    ['https://github.com/NVIDIA/SkillSpector/pull/42', 'github.com', 'NVIDIA/SkillSpector'],
    ['git@github.com:NVIDIA/SkillSpector.git', 'github.com', 'NVIDIA/SkillSpector'],
    ['ssh://git@github.com/NVIDIA/SkillSpector.git', 'github.com', 'NVIDIA/SkillSpector'],
    ['NVIDIA/SkillSpector', 'github.com', 'NVIDIA/SkillSpector'],
    ['https://gitlab.example.com/team/sub/proj', 'gitlab.example.com', 'team/sub/proj'],
    ['https://gitlab.example.com/team/sub/proj/-/issues/3', 'gitlab.example.com', 'team/sub/proj'],
  ]
  it.each(ok)('parses %s', (raw, host, path) => {
    const n = normalizeRepo(raw)
    expect(n.ok).toBe(true)
    expect(n.host).toBe(host)
    expect(n.path).toBe(path)
    expect(n.canonical).toBe(`${host}/${path}`)
  })

  it('canonical identity is stable across forms', () => {
    const forms = [
      'https://github.com/NVIDIA/SkillSpector',
      'https://github.com/NVIDIA/SkillSpector.git',
      'git@github.com:NVIDIA/SkillSpector.git',
      'https://github.com/NVIDIA/SkillSpector/tree/main',
    ]
    const canon = new Set(forms.map(f => normalizeRepo(f).canonical))
    expect([...canon]).toEqual(['github.com/NVIDIA/SkillSpector'])
  })

  it('flags github vs non-github', () => {
    expect(normalizeRepo('owner/name').isGithub).toBe(true)
    expect(normalizeRepo('https://gitlab.example.com/g/p').isGithub).toBe(false)
  })
})

describe('normalizeRepo — rejected forms', () => {
  const bad = ['', '   ', 'noslash', 'not a repo', 'trailing/', '/leading']
  it.each(bad)('rejects %p', (raw) => {
    const n = normalizeRepo(raw)
    expect(n.ok).toBe(false)
    expect(typeof n.error).toBe('string')
    expect(n.error.length).toBeGreaterThan(0)
  })

  it('rejects unsupported scheme', () => {
    expect(normalizeRepo('ftp://github.com/owner/name').ok).toBe(false)
  })

  it('gives a specific error, not "must be owner/name"', () => {
    const n = normalizeRepo('https://github.com/justone')
    expect(n.ok).toBe(false)
    expect(n.error).toMatch(/owner\/name/)
  })

  it('does not reject hyphenated names (regex range bug guard)', () => {
    const n = normalizeRepo('my-org/my-repo')
    expect(n.ok).toBe(true)
    expect(n.canonical).toBe('github.com/my-org/my-repo')
  })
})
