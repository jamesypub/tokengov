import { describe, it, expect } from 'vitest'
import React from 'react'
import { render, screen } from '@testing-library/react'
import {
  arnRole, arnAccount, governanceState, quotaKeying,
  actionGateReason, accessBadgeState, userActionTier,
  manageGateReason, UnmanageModal,
  canPerform, capAppliesTo, GovernancePanel,
  showOverCapAffordance,
} from './UserDetail'

// #629/#750: the detail page's pure helpers — governance state,
// quota keying, and the action-gating reason. These drive the
// governance panel + the Set cap / Force block gate.

const human = {
  email: 'alice@test.com',
  principal_arn: 'arn:aws:iam::123:role/tg-consumer',
  principal_type: 'assumed_role',
}
const machine = {
  email: null, identity_key: 'role:BatchRole',
  principal_arn: 'arn:aws:iam::123:role/BatchRole',
  principal_type: 'service',
}
const idc = {
  email: 'dev@corp.com', role_type: 'idc',
  principal_arn:
    'arn:aws:sts::123:assumed-role/AWSReservedSSO_Dev_x/dev@corp.com',
  principal_type: 'assumed_role',
}

describe('arnRole / arnAccount', () => {
  it('pulls role + account from an assumed-role ARN', () => {
    expect(arnRole(idc.principal_arn)).toBe('AWSReservedSSO_Dev_x')
    expect(arnAccount(human.principal_arn)).toBe('123')
  })
})

describe('governanceState', () => {
  it('#1011: IDC follows the governed flag (not terminal idc)', () => {
    expect(governanceState({ role_type: 'idc', governed: true }))
      .toBe('governed')
    expect(governanceState({ role_type: 'idc', governed: false }))
      .toBe('ungoverned')
  })
  it('governed (non-idc) → governed', () => {
    expect(governanceState({ ...human, governed: true }))
      .toBe('governed')
  })
  it('discovered, not governed → ungoverned', () => {
    expect(governanceState({ ...human, governed: false }))
      .toBe('ungoverned')
  })
})

describe('quotaKeying', () => {
  it('email-pinned human → aws:userid *:<email>', () => {
    expect(quotaKeying(human)).toEqual({
      kind: 'aws:userid', value: '*:alice@test.com',
    })
  })
  it('machine / no email → aws:PrincipalArn on the role ARN', () => {
    expect(quotaKeying(machine)).toEqual({
      kind: 'aws:PrincipalArn',
      value: 'arn:aws:iam::123:role/BatchRole',
    })
  })
})

describe('actionGateReason', () => {
  it('null (actions allowed) once governed', () => {
    expect(actionGateReason({ ...human, governed: true }))
      .toBeNull()
  })
  it('ungoverned → gate reason explains the opt-in gate', () => {
    const r = actionGateReason({ ...human, governed: false })
    expect(r).toBeTruthy()
    // #822: makes the opt-in gate explicit — not governed = ignored
    // until you Govern (enroll) it, even over cap.
    expect(r).toMatch(/not governed/)
    expect(r).toMatch(/Govern \(enroll\)/)
    expect(r).toMatch(/no effect/)
  })
  it('#1011: ungoverned IDC → the ungoverned no-op gate (governable)', () => {
    // IDC is governable now: an ungoverned IDC user gets the same
    // "not governed yet → no effect" gate as any ungoverned user,
    // NOT the old terminal "permission set / SCP, not tg" message.
    const r = actionGateReason(idc)
    expect(r).toBeTruthy()
    expect(r).toMatch(/no effect/)
    expect(r).not.toMatch(/not governable/)
  })
  it('#1011: governed IDC → no gate (cap/force-block apply)', () => {
    expect(actionGateReason({ ...idc, governed: true })).toBeNull()
  })
})

// #707: Govern must be disabled (not 400) when there's no attachable
// IAM role. The reason mirrors the server's _role_name_from_arn rule
// (accepts only arn:aws:iam::<acct>:role/<name>).
describe('manageGateReason (#707)', () => {
  it('null (attachable) for an assumed-role principal', () => {
    expect(manageGateReason(human)).toBeNull()
  })
  it('null (attachable) for a service role principal', () => {
    expect(manageGateReason(machine)).toBeNull()
  })
  it('pre-registered (no principal_arn) → names the no-role-ARN '
    + 'blocker + both paths out (#946)', () => {
    const r = manageGateReason({
      email: 'new@test.com',
      principal_arn: null, principal_type: null,
    })
    expect(r).toBeTruthy()
    // #946: the real blocker is "no IAM role ARN", NOT missing spend.
    expect(r).toMatch(/no IAM role ARN/)
    // names both ways out: record it now OR observed at Bedrock.
    expect(r).toMatch(/Add the role ARN/)
    expect(r).toMatch(/observed at\s+Bedrock/)
    // must NOT imply Bedrock spend is required.
    expect(r).not.toMatch(/invoke a model/)
  })
  it('IAM-user principal (non-role ARN) → not attachable', () => {
    const r = manageGateReason({
      email: 'bob@test.com',
      principal_arn: 'arn:aws:iam::123:user/bob',
      principal_type: 'iam_user',
    })
    expect(r).toBeTruthy()
    expect(r).toMatch(/no attachable IAM role|IAM user/)
  })
  it('root principal → not attachable', () => {
    const r = manageGateReason({
      principal_arn: 'arn:aws:iam::123:root',
      principal_type: 'root',
    })
    expect(r).toBeTruthy()
  })
  it('null user → null (no crash)', () => {
    expect(manageGateReason(null)).toBeNull()
  })
})

// #642 regression: the Access-card "Status" badge must reflect the
// deny-only `governed` flag (the thing Govern/Ungovern toggles), NOT
// the #345 `managed` heuristic — else the status never changes when
// you Govern/Ungovern (the reported bug).
describe('accessBadgeState (#642)', () => {
  const humanArn = 'arn:aws:iam::123:role/tg-consumer'

  it('governed=true → governed (flips with the Govern action)', () => {
    expect(accessBadgeState({
      principal_type: 'assumed_role', principal_arn: humanArn,
      governed: true,
    })).toBe('governed')
  })

  it('governed=false → ungoverned, even if the #345 managed '
     + 'heuristic is true (badge must not read .managed)', () => {
    expect(accessBadgeState({
      principal_type: 'assumed_role', principal_arn: humanArn,
      governed: false, managed: true,
    })).toBe('ungoverned')
  })

  it('IDC wins over governed', () => {
    expect(accessBadgeState({
      principal_type: 'assumed_role',
      principal_arn: 'arn:aws:sts::123:assumed-role/AWSReservedSSO_x/a@b.com',
      role_type: 'idc', governed: true,
    })).toBe('idc')
  })

  it('principal-type buckets when ungoverned', () => {
    expect(accessBadgeState({
      principal_type: 'service',
      principal_arn: 'arn:aws:iam::123:role/Batch',
    })).toBe('service')
    expect(accessBadgeState({
      principal_type: 'root',
      principal_arn: 'arn:aws:iam::123:root',
    })).toBe('root')
    expect(accessBadgeState({})).toBe('unknown')
  })
})

// #650: 3-tier action gate, mirroring the server rule.
describe('userActionTier (#650)', () => {
  const userA = { email: 'a@t.com', team_id: 'A' }
  const userB = { email: 'b@t.com', team_id: 'B' }

  it('org_admin can admin any user', () => {
    const me = { email: 'org@t.com', persona: 'org_admin',
      org_admin: true, team_ids: [] }
    expect(userActionTier(me, userB))
      .toMatchObject({ canAdmin: true, isOrgAdmin: true })
  })

  it('team_admin can admin users in their subtree, not outside', () => {
    const me = { email: 'ta@t.com', persona: 'team_admin',
      org_admin: false, team_ids: ['A'] }
    expect(userActionTier(me, userA).canAdmin).toBe(true)
    expect(userActionTier(me, userB).canAdmin).toBe(false)
  })

  it('member: not admin, but isSelf on their own row', () => {
    const me = { email: 'a@t.com', persona: 'member',
      org_admin: false, team_ids: [] }
    expect(userActionTier(me, userA))
      .toMatchObject({ canAdmin: false, isSelf: true })
    expect(userActionTier(me, userB))
      .toMatchObject({ canAdmin: false, isSelf: false })
  })

  it('isSelf is case-insensitive on email', () => {
    const me = { email: 'A@T.com', persona: 'member',
      team_ids: [] }
    expect(userActionTier(me, userA).isSelf).toBe(true)
  })

  it('null me/user → no authority', () => {
    expect(userActionTier(null, userA))
      .toEqual({ isSelf: false, canAdmin: false, isOrgAdmin: false })
  })
})

// #827: the Unmanage confirm must warn — only when the target is
// force_blocked — that unmanaging ALSO lifts the force-block, so the
// admin isn't surprised an unrelated-looking action restores access.
describe('UnmanageModal force-block warning (#827)', () => {
  const noop = () => {}

  it('warns about the lifted force-block when force_blocked', () => {
    render(<UnmanageModal
      user={{
        email: 'fb@test.com', status: 'force_blocked',
        principal_arn: 'arn:aws:iam::123:role/tg-install',
      }}
      busy={false} onClose={noop} onConfirm={noop} />)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(/force-block/i)
    expect(alert).toHaveTextContent(/unblock/i)
  })

  it('shows no force-block warning for an active principal', () => {
    render(<UnmanageModal
      user={{
        email: 'act@test.com', status: 'active',
        principal_arn: 'arn:aws:iam::123:role/tg-install',
      }}
      busy={false} onClose={noop} onConfirm={noop} />)
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

// #837: per-action gate must match the API authz exactly — every
// action is team-admin EXCEPT delete_user (org-admin-only).
describe('canPerform (#837)', () => {
  const orgAdmin = { canAdmin: true, isOrgAdmin: true }
  const teamAdmin = { canAdmin: true, isOrgAdmin: false }
  const member = { canAdmin: false, isOrgAdmin: false }

  it('team-admin can perform every non-delete action', () => {
    for (const a of ['govern', 'ungovern', 'cap', 'set_team',
                     'force_block', 'unblock']) {
      expect(canPerform(a, teamAdmin)).toBe(true)
    }
  })
  it('delete_user is org-admin-only', () => {
    expect(canPerform('delete_user', orgAdmin)).toBe(true)
    expect(canPerform('delete_user', teamAdmin)).toBe(false)
  })
  it('org-admin can perform everything', () => {
    for (const a of ['govern', 'ungovern', 'cap', 'set_team',
                     'force_block', 'unblock', 'delete_user']) {
      expect(canPerform(a, orgAdmin)).toBe(true)
    }
  })
  it('a non-admin can perform nothing', () => {
    expect(canPerform('cap', member)).toBe(false)
    expect(canPerform('delete_user', member)).toBe(false)
    expect(canPerform('govern', null)).toBe(false)
  })
})

// #821: the over-cap "Raise cap to unblock" affordance gating —
// ONLY for the auto over-cap state (status='blocked'), never the
// manual force_blocked or active, and only for an admin who can set
// the cap (it routes into the existing CapModal willUnblock path).
describe('showOverCapAffordance (#821)', () => {
  const orgAdmin = { canAdmin: true, isOrgAdmin: true }
  const teamAdmin = { canAdmin: true, isOrgAdmin: false }
  const member = { canAdmin: false, isOrgAdmin: false }

  it('shows for an auto over-cap blocked user (admin)', () => {
    expect(showOverCapAffordance({ status: 'blocked' }, orgAdmin)).toBe(true)
    expect(showOverCapAffordance({ status: 'blocked' }, teamAdmin)).toBe(true)
  })
  it('does NOT show for force_blocked (manual — keeps its Unblock button)', () => {
    expect(showOverCapAffordance({ status: 'force_blocked' }, orgAdmin))
      .toBe(false)
  })
  it('does NOT show for an active user', () => {
    expect(showOverCapAffordance({ status: 'active' }, orgAdmin)).toBe(false)
  })
  it('does NOT show to a non-admin who cannot set the cap', () => {
    expect(showOverCapAffordance({ status: 'blocked' }, member)).toBe(false)
    expect(showOverCapAffordance({ status: 'blocked' }, null)).toBe(false)
  })
  it('tolerates a null user (no crash)', () => {
    expect(showOverCapAffordance(null, orgAdmin)).toBe(false)
  })
})

describe('capAppliesTo (#837)', () => {
  it('email principal → the person, across any role', () => {
    const r = capAppliesTo({ email: 'tg-org-admin+ops@example.com' })
    expect(r.who).toBe('tg-org-admin+ops@example.com')
    expect(r.scope).toMatch(/across any role/i)
  })
  it('machine principal → the role, its sessions', () => {
    const r = capAppliesTo({
      email: null, identity_key: 'i-0abc',
      principal_arn: 'arn:aws:iam::123:role/BatchRole',
    })
    expect(r.who).toBe('BatchRole')
    expect(r.scope).toMatch(/role/i)
  })
})

// #837: GovernancePanel is status-only + plain-language with a
// Technical details disclosure — no Govern/Ungovern buttons (those
// moved to the Actions bar).
describe('GovernancePanel plain-language status (#837)', () => {
  const governedUser = {
    governed: true, role_type: 'iam',
    email: 'tg-org-admin+ops@example.com',
    principal_arn: 'arn:aws:iam::123:role/tg-install-from-123456789012',
  }

  it('leads with plain language, not raw ARN/condition strings', () => {
    render(<GovernancePanel user={governedUser} />)
    expect(screen.getByText(/Governed on role:/i)).toBeInTheDocument()
    expect(screen.getByText(/Spend cap applies to:/i)).toBeInTheDocument()
    // plain self-detach advisory, not "iam:* / advisory" jargon
    expect(screen.getByText(/best-effort, not guaranteed/i))
      .toBeInTheDocument()
  })

  it('hides raw ARN + condition keys behind a Technical details disclosure', () => {
    const { container } = render(<GovernancePanel user={governedUser} />)
    // a <details> disclosure exists, labeled "Technical details"
    const details = container.querySelector('details')
    expect(details).not.toBeNull()
    expect(screen.getByText('Technical details')).toBeInTheDocument()
    // the raw keying string lives inside it (present in DOM, but not
    // the primary copy)
    expect(details.textContent).toMatch(/aws:userid/)
  })

  it('renders NO Govern/Ungovern button (moved to Actions)', () => {
    render(<GovernancePanel user={governedUser} />)
    expect(screen.queryByRole('button', { name: /ungovern/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /govern/i })).toBeNull()
  })

  it('ungoverned principal points the admin at Actions → Govern', () => {
    render(<GovernancePanel user={{
      governed: false, role_type: 'iam',
      email: 'x@test.com',
      principal_arn: 'arn:aws:iam::123:role/r',
    }} />)
    expect(screen.getByText(/Ungoverned/i)).toBeInTheDocument()
  })

  it('#1011: ungoverned IDC shows the governable advisory, not "not governable"', () => {
    const { container } = render(<GovernancePanel user={{
      role_type: 'idc', email: 'dev@corp.com',
      principal_arn: 'arn:aws:sts::1:assumed-role/AWSReservedSSO_x/dev@corp.com',
    }} />)
    // ungoverned IDC reads the Ungoverned · IDC state + the advisory
    // precondition (tg-consumer / permission-set), in plain language —
    // no terminal "not governable" copy. Raw ARN jargon
    // (AWSReservedSSO_*) is allowed only inside the Technical details
    // disclosure, not in the primary copy.
    expect(screen.getByText(/Ungoverned/i)).toBeInTheDocument()
    expect(screen.getByText(/permission set/i)).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/not governable/i)
    // jargon is tucked behind the disclosure, never up front
    const details = container.querySelector('details')
    const upFront = container.textContent.replace(
      details ? details.textContent : '', '')
    expect(upFront).not.toMatch(/AWSReservedSSO/)
  })

  it('#1011: governed IDC reads "Governed · IDC" + advisory note', () => {
    const { container } = render(<GovernancePanel user={{
      role_type: 'idc', governed: true, email: 'dev@corp.com',
      principal_arn: 'arn:aws:sts::1:assumed-role/AWSReservedSSO_x/dev@corp.com',
    }} />)
    expect(container.textContent).toMatch(/Governed · ◆ IDC/)
    expect(container.textContent).toMatch(/enforced via the permission-set/i)
  })
})


// Apply-timing status: govern/block/unblock enforce via the
// deny_reconciler (~5-min tick), not instantly. The user page shows the
// SAME shared GovernanceApplyStatus component as Org Settings → Blocked
// models (one source, identical wording) — assert it's imported/reused,
// NOT re-defined here (the reuse-not-duplicate AC).
import { readFileSync } from 'fs'

describe('GovernanceApplyStatus reuse on UserDetail', () => {
  const src = readFileSync('src/pages/UserDetail.jsx', 'utf8')

  it('imports the shared component (does not re-define it)', () => {
    expect(src).toMatch(
      /import\s+GovernanceApplyStatus\s+from\s+'\.\.\/components\/GovernanceApplyStatus'/)
    // no local re-definition of the component
    expect(src).not.toMatch(/function\s+GovernanceApplyStatus/)
  })

  it('renders it keyed on governance_updated_at, gated to governed', () => {
    // the status is wired to the per-user save timestamp and only the
    // governed branch (an ungoverned principal has no deny to enforce).
    expect(src).toMatch(/<GovernanceApplyStatus/)
    expect(src).toMatch(/updatedAt=\{user\.governance_updated_at\}/)
    expect(src).toMatch(/g === 'governed' && \(\s*<GovernanceApplyStatus/)
  })
})
