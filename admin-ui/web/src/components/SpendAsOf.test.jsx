import React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

// #703: SpendAsOf is mounted on spend pages every persona can see
// (Velocity & Cost, Teams, UserDetail), but its data source —
// GET /api/cur/data-through — is org-admin-only (cur.py
// require_org_admin). Firing it unconditionally put a 403 in
// non-admin consoles on a working page. These assert the component
// gates the fetch on the persona from useTeamScope: only org_admin
// issues the request; team_admin / member never do (no denied
// round-trip, no console 403), and the watermark simply doesn't
// render for them.

const curDataThrough = vi.fn()

vi.mock('../api', () => ({
  api: { curDataThrough: (...a) => curDataThrough(...a) },
}))

let _persona = 'org_admin'
vi.mock('../TeamScope', () => ({
  useTeamScope: () => ({ selectedTeam: null, persona: _persona }),
}))

import SpendAsOf from './SpendAsOf'

beforeEach(() => {
  curDataThrough.mockReset()
  _persona = 'org_admin'
})

describe('SpendAsOf — admin-only fetch gating (#703)', () => {
  it('org_admin fetches the watermark and renders it', async () => {
    _persona = 'org_admin'
    curDataThrough.mockResolvedValue({ data_through: '2026-06-07T08:00:00Z' })
    render(<SpendAsOf />)
    await waitFor(() => expect(curDataThrough).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/Spend current as of/)).toBeTruthy()
  })

  it('team_admin never calls curDataThrough (no 403 noise)', async () => {
    _persona = 'team_admin'
    const { container } = render(<SpendAsOf />)
    // give any (incorrect) effect a chance to fire
    await new Promise(r => setTimeout(r, 20))
    expect(curDataThrough).not.toHaveBeenCalled()
    expect(container.textContent).toBe('')   // renders nothing
  })

  it('member never calls curDataThrough (no 403 noise)', async () => {
    _persona = 'member'
    const { container } = render(<SpendAsOf />)
    await new Promise(r => setTimeout(r, 20))
    expect(curDataThrough).not.toHaveBeenCalled()
    expect(container.textContent).toBe('')
  })
})
