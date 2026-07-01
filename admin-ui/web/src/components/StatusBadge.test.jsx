import React from 'react'
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import StatusBadge from './StatusBadge'

describe('StatusBadge', () => {
  it.each([
    ['active', 'active'],
    ['blocked', 'blocked'],
    ['force_blocked', 'force-blocked'],
  ])('renders label for status=%s', (status, label) => {
    render(<StatusBadge status={status} />)
    expect(screen.getByText(label)).toBeInTheDocument()
  })

  it('renders unknown status verbatim with fallback styling', () => {
    render(<StatusBadge status="zzz" />)
    expect(screen.getByText('zzz')).toBeInTheDocument()
  })

  it('renders sub-line when sub prop provided', () => {
    render(<StatusBadge status="active" sub="92%" />)
    expect(screen.getByText(/92%/)).toBeInTheDocument()
  })

  it('omits sub-line when not provided', () => {
    render(<StatusBadge status="active" />)
    // Just the dot character + "active" — no extra middot/sub text
    expect(screen.queryByText(/·/)).toBeNull()
  })

  it('force_blocked badge has correct title attribute (a11y hint)', () => {
    const { container } = render(<StatusBadge status="force_blocked" />)
    const span = container.querySelector('span[title]')
    expect(span).toBeTruthy()
    expect(span.getAttribute('title')).toBe(
      'admin override — denied regardless of spend')
  })
})
