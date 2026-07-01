import { describe, it, expect } from 'vitest'
import React from 'react'
import { render, screen } from '@testing-library/react'
import {
  sessionName, isEmailShaped, callerBucket,
  governanceState, matchesCallerFilter, driftBannerModel,
  GovernanceCell, GovernanceLegend, GOVERNANCE_LEGEND,
  headerAlignClasses, SummaryTile,
} from './Users'

// #608: the People/Machines filter is a view-time predicate over
// the role-session-name (last ARN segment), never a stored verdict.
// These cover the acceptance cases: email→People, instance-id→
// Machines, root/empty/unparseable→Machines.

// #839: ingestion stores principal_arn = the ROLE arn (so the deny
// reconciler can attach to it, #345) and the email SEPARATELY. So a
// real human assumed-role row carries an email + a role-tail ARN —
// exactly the shape the old sessionName-based bucketing mislabeled.
const human = {
  email: 'tg-org-admin@example.com',
  identity_key: 'tg-org-admin@example.com',
  principal_arn:
    'arn:aws:iam::123456789012:role/tg-consumer',  // role ARN, not session
  principal_type: 'assumed_role',
}
// genuine machine: instance-id session, no email captured.
const machine = {
  email: null,
  identity_key: 'i-0819dd4c6fbfd88bb',
  principal_arn: 'arn:aws:iam::123456789012:role/dev-machine-role-xyz',
  principal_type: 'service',
}
const root = {
  principal_arn: 'arn:aws:iam::123456789012:root',
  principal_type: 'root',
}
const iamUser = {
  email: null,
  principal_arn: 'arn:aws:iam::123456789012:user/alice',
  principal_type: 'iam_user',
}
const unattributed = {
  principal_arn: null,
  principal_type: null,
  email: 'pending@example.com',
}
const unattributedNoEmail = {
  principal_arn: null, principal_type: null, identity_key: 'role:SomeRole',
}

describe('sessionName (display label — last ARN segment)', () => {
  it('extracts the last ARN segment (role/session name)', () => {
    // #839: principal_arn is the ROLE arn, so the display label is
    // the role name — fine for DISPLAY; callerBucket no longer keys
    // People/Machines off this (that was the bug).
    expect(sessionName(human)).toBe('tg-consumer')
    expect(sessionName(machine)).toBe('dev-machine-role-xyz')
  })
  it('renders root as "root"', () => {
    expect(sessionName(root)).toBe('root')
  })
  it('extracts the user name for an IAM user ARN', () => {
    expect(sessionName(iamUser)).toBe('alice')
  })
  it('falls back to email / identity_key when no ARN attributed', () => {
    expect(sessionName(unattributed)).toBe('pending@example.com')
    expect(sessionName(unattributedNoEmail)).toBe('role:SomeRole')
  })
})

describe('isEmailShaped', () => {
  it.each([
    ['tg-org-admin@example.com', true],
    ['someone@corp.co.uk', true],
    ['i-0819dd4c6fbfd88bb', false],
    ['root', false],
    ['', false],
    [null, false],
    ['no-at-sign', false],
    ['missing@dot', false],
    ['two@@at.com', false],
  ])('isEmailShaped(%s) === %s', (input, expected) => {
    expect(isEmailShaped(input)).toBe(expected)
  })
})

// #839: People/Machines must key on the email + principal_type the
// row carries, NOT on sessionName's role-name tail (the bug: a human
// assumed-role whose principal_arn is the role ARN → role-name tail
// `tg-consumer` → mislabeled Machine despite having an email).
describe('callerBucket (#839)', () => {
  it('assumed_role WITH email → people, even though principal_arn '
     + 'is a role ARN (the #839 bug)', () => {
    expect(callerBucket(human)).toBe('people')   // tg-org-admin@…
  })
  it('a +tag email assumed-role session → people', () => {
    expect(callerBucket({
      email: 'tg-org-admin+dev@example.com',
      principal_type: 'assumed_role',
      principal_arn:
        'arn:aws:iam::1:role/tg-install-from-123456789012',
    })).toBe('people')
  })
  it('pre-registered human (no ARN yet) with email → people', () => {
    expect(callerBucket(unattributed)).toBe('people')
  })
  it('email-shaped identity_key (no email field) → people', () => {
    expect(callerBucket({
      email: null, identity_key: 'dev@corp.com',
      principal_type: 'assumed_role',
    })).toBe('people')
  })
  it('service / service_linked → machines', () => {
    expect(callerBucket(machine)).toBe('machines')  // service, instance-id
    expect(callerBucket({
      email: null, principal_type: 'service_linked',
      principal_arn: 'arn:aws:iam::1:role/AWSServiceRoleForX',
    })).toBe('machines')
  })
  it('root → machines (the look-here bucket)', () => {
    expect(callerBucket(root)).toBe('machines')
  })
  it('IAM user with no email-shaped identity → machines', () => {
    expect(callerBucket(iamUser)).toBe('machines')  // user/alice
  })
  it('federated session with no email → machines', () => {
    expect(callerBucket({
      email: null, identity_key: 'role:SomeRole',
      principal_type: 'federated',
    })).toBe('machines')
  })
  it('no email-shaped identity → machines', () => {
    expect(callerBucket(unattributedNoEmail)).toBe('machines')
  })
  it('null user → machines (no crash)', () => {
    expect(callerBucket(null)).toBe('machines')
  })
})

// #844: the Spend / Cap header must right-align to match its
// right-aligned numeric cells. Alignment is per-column via
// meta.align — NOT a hardcoded text-left on the sortable <th>.
describe('headerAlignClasses (#844)', () => {
  it('right-aligned column → text-right th + full-width end-justified span', () => {
    const a = headerAlignClasses({ align: 'right' })
    expect(a.th).toBe('text-right')
    // w-full + justify-end is what makes the label+arrow sit flush
    // right over the numbers (the inline-flex span otherwise only
    // spans its content, pinned left — the #844 bug).
    expect(a.span).toMatch(/\bw-full\b/)
    expect(a.span).toMatch(/\bjustify-end\b/)
  })
  it('default (no meta / left) → text-left th, no span justification', () => {
    expect(headerAlignClasses(undefined).th).toBe('text-left')
    expect(headerAlignClasses({}).th).toBe('text-left')
    expect(headerAlignClasses({}).span).toBe('')
  })
})

// #628/#1011: governanceState is governed/ungoverned. #1011: IDC is
// NO LONGER terminal — an IDC user is governed/ungoverned like any
// other; isIdc is a separate qualifier.
describe('governanceState', () => {
  it('#1011: IDC follows governed flag (not terminal idc)', () => {
    expect(governanceState({ role_type: 'idc' })).toBe('ungoverned')
    expect(governanceState({ role_type: 'idc', governed: true }))
      .toBe('governed')
  })
  it('governed=true (non-idc) → governed', () => {
    expect(governanceState({ role_type: 'iam', governed: true })).toBe('governed')
    expect(governanceState({ governed: true })).toBe('governed')
  })
  it('discovered, not governed → ungoverned', () => {
    expect(governanceState({ role_type: 'iam', governed: false })).toBe('ungoverned')
    expect(governanceState({})).toBe('ungoverned')
  })
})

// #628: the 5-way list filter. People/Machines slice the
// session-name; Ungoverned/IDC slice the governance state.
describe('matchesCallerFilter', () => {
  const idcUser = {
    role_type: 'idc',
    principal_arn:
      'arn:aws:sts::123:assumed-role/AWSReservedSSO_Dev_x/dev@corp.com',
    principal_type: 'assumed_role',
  }
  // #839: real rows carry email + the role ARN (not a session ARN).
  const governedHuman = {
    role_type: 'iam', governed: true,
    email: 'a@corp.com', identity_key: 'a@corp.com',
    principal_arn: 'arn:aws:iam::123:role/tg-consumer',
    principal_type: 'assumed_role',
  }
  // genuine machine session: no email, service principal type.
  const ungovernedMachine = {
    role_type: 'iam', governed: false,
    email: null, identity_key: 'i-0abc',
    principal_arn: 'arn:aws:iam::123:role/BatchRole',
    principal_type: 'service',
  }

  it('all matches everything', () => {
    expect(matchesCallerFilter(idcUser, 'all')).toBe(true)
    expect(matchesCallerFilter(ungovernedMachine, 'all')).toBe(true)
  })
  it('people / machines slice the session-name', () => {
    expect(matchesCallerFilter(governedHuman, 'people')).toBe(true)
    expect(matchesCallerFilter(governedHuman, 'machines')).toBe(false)
    expect(matchesCallerFilter(ungovernedMachine, 'machines')).toBe(true)
    expect(matchesCallerFilter(ungovernedMachine, 'people')).toBe(false)
  })
  it('ungoverned slices the governance state', () => {
    expect(matchesCallerFilter(ungovernedMachine, 'ungoverned')).toBe(true)
    expect(matchesCallerFilter(governedHuman, 'ungoverned')).toBe(false)
    // #1011: an ungoverned IDC row IS ungoverned now (not terminal).
    expect(matchesCallerFilter(idcUser, 'ungoverned')).toBe(true)
    // ...and a governed IDC row is not.
    expect(matchesCallerFilter(
      { ...idcUser, governed: true }, 'ungoverned')).toBe(false)
  })
  it('idc slices IDC principals', () => {
    expect(matchesCallerFilter(idcUser, 'idc')).toBe(true)
    // #1011: the idc filter is by role_type, independent of governed.
    expect(matchesCallerFilter(
      { ...idcUser, governed: true }, 'idc')).toBe(true)
    expect(matchesCallerFilter(governedHuman, 'idc')).toBe(false)
  })
})


// #818/#846: the governance-drift banner model. Pure show/hide +
// row shape so the banner's behavior is testable without a full
// render. #846 adds plain-language copy keyed on the `direction`
// enum + a detail-page href; raw expected/actual/detail stay for the
// Technical-details disclosure.
describe('driftBannerModel (#818/#846)', () => {
  const sample = [
    {
      identity_key: 'tg-org-admin@example.com',
      email: 'tg-org-admin@example.com',
      role_arn: 'arn:aws:iam::123:role/tg-install',
      direction: 'governed_no_deny',
      expected: 'governed',
      actual: 'deny-not-attached',
      detail: 'tg-BedrockQuotaDeny not attached to the role',
      sweep_at: '2026-06-08T19:30:00+00:00',
    },
  ]

  it('drift>0 → banner shows, with count + sweep label + rows', () => {
    const b = driftBannerModel(sample, '2026-06-08T19:30:00+00:00')
    expect(b.show).toBe(true)
    expect(b.count).toBe(1)
    expect(b.sweepLabel).toBe('2026-06-08 19:30 UTC')
    expect(b.rows[0].who).toBe('tg-org-admin@example.com')
    // technical-details fields preserved for the disclosure
    expect(b.rows[0].roleArn).toBe('arn:aws:iam::123:role/tg-install')
    expect(b.rows[0].expected).toBe('governed')
    expect(b.rows[0].actual).toBe('deny-not-attached')
    expect(b.rows[0].detail).toMatch(/not attached/)
  })

  it('drift==0 (clean sweep) → banner hidden', () => {
    expect(driftBannerModel([], '2026-06-08T19:30:00+00:00').show)
      .toBe(false)
  })

  it('non-admin / 403 (caller passes []/undefined) → hidden, no throw', () => {
    // The page catches the 403 and sets drift=[]; null is also safe.
    expect(driftBannerModel(undefined, null).show).toBe(false)
    expect(driftBannerModel(null, null).show).toBe(false)
  })

  it('falls back to identity_key when email is absent (machine role)', () => {
    const b = driftBannerModel([{
      identity_key: 'role:Batch', email: null,
      role_arn: 'arn:aws:iam::1:role/Batch',
      direction: 'deny_no_governed',
      expected: 'ungoverned', actual: 'deny-attached',
    }], null)
    expect(b.rows[0].who).toBe('role:Batch')
    expect(b.sweepLabel).toBeNull()  // no sweep_at → no label
  })

  // #846: plain, action-oriented copy keyed on `direction`, NOT the
  // raw expected/actual jargon.
  it('governed_no_deny → plain "set to be governed but not enforced" + Govern action', () => {
    const r = driftBannerModel(sample, null).rows[0]
    expect(r.direction).toBe('governed_no_deny')
    expect(r.plain).toMatch(/set to be governed/i)
    expect(r.plain).toMatch(/aren’t actually being enforced/i)
    expect(r.action).toMatch(/re-apply Govern/i)
    // no raw jargon in the primary plain/action copy
    expect(r.plain).not.toMatch(/tg-BedrockQuotaDeny|governed=true|deny-not-attached/)
  })

  it('deny_no_governed → plain "still has enforcement but ungoverned" + Ungovern action', () => {
    const r = driftBannerModel([{
      identity_key: 'x@test.com', email: 'x@test.com',
      role_arn: 'arn:aws:iam::1:role/r',
      direction: 'deny_no_governed',
      expected: 'ungoverned', actual: 'deny-attached',
    }], null).rows[0]
    expect(r.plain).toMatch(/still has enforcement applied/i)
    expect(r.action).toMatch(/Ungovern to remove it/i)
  })

  it('each row links to the user detail page', () => {
    const r = driftBannerModel(sample, null).rows[0]
    expect(r.href).toBe('#/users/tg-org-admin%40example.com')
  })

  it('unknown direction → graceful generic copy (no crash)', () => {
    const r = driftBannerModel([{
      email: 'y@test.com', direction: 'something_new',
      expected: 'x', actual: 'y',
    }], null).rows[0]
    expect(r.plain).toBeTruthy()
    expect(r.action).toBeTruthy()
  })
})

// #824: the Governance column no longer uses a bare help-cursor +
// native title on its data cells. The per-row icon keeps an
// accessible name; the legend lives behind a deliberate header
// affordance (ⓘ) that reveals on hover AND keyboard focus via a
// styled role=tooltip, not the native title attribute.
describe('GovernanceCell (#824)', () => {
  it('keeps an accessible name and drops the help-cursor + title', () => {
    const { container } = render(
      <GovernanceCell user={{ role_type: 'iam', governed: true }} />)
    const icon = screen.getByRole('img', { name: 'Governed' })
    expect(icon).toBeInTheDocument()
    // No help-cursor on the data cell, and no native title tooltip.
    expect(icon.className).not.toMatch(/cursor-help/)
    expect(icon).not.toHaveAttribute('title')
    expect(container.querySelector('[title]')).toBeNull()
  })

  it('#1011: an IDC row renders state icon + an IDC qualifier badge', () => {
    // ungoverned IDC user → ○ governance icon PLUS a ◆ IDC qualifier
    // (IDC is no longer a terminal state).
    render(<GovernanceCell user={{ role_type: 'idc' }} />)
    expect(screen.getByRole('img', { name: 'Ungoverned' }))
      .toBeInTheDocument()
    // the IDC qualifier rides alongside, with the words in aria-label
    const qual = screen.getByLabelText(/^IDC —/)
    expect(qual.textContent).toContain('◆')
    expect(qual.textContent).toContain('IDC')
  })

  it('#1011: a governed IDC row shows ✓ + the IDC qualifier', () => {
    render(<GovernanceCell
      user={{ role_type: 'idc', governed: true }} />)
    expect(screen.getByRole('img', { name: 'Governed' }))
      .toBeInTheDocument()
    expect(screen.getByLabelText(/^IDC —/)).toBeInTheDocument()
  })
})

describe('GovernanceLegend header affordance (#824)', () => {
  it('exposes an accessible legend button, not a native title', () => {
    const { container } = render(<GovernanceLegend />)
    const btn = screen.getByRole('button', {
      name: 'Governance column legend',
    })
    expect(btn).toBeInTheDocument()
    // The styled tooltip is a real role=tooltip the button
    // describes — not a native title attribute.
    expect(btn).toHaveAttribute(
      'aria-describedby', 'governance-legend-tip')
    const tip = container.querySelector('#governance-legend-tip')
    expect(tip).toHaveAttribute('role', 'tooltip')
    expect(container.querySelector('[title]')).toBeNull()
  })

  it('the tooltip carries all three legend entries', () => {
    render(<GovernanceLegend />)
    // Each legend label + desc is present in the (CSS-hidden until
    // hover/focus) tooltip, so AT users reach the full legend.
    for (const g of Object.values(GOVERNANCE_LEGEND)) {
      expect(screen.getByText(g.label)).toBeInTheDocument()
    }
  })

  it('the legend button (only) carries the help cursor', () => {
    render(<GovernanceLegend />)
    const btn = screen.getByRole('button', {
      name: 'Governance column legend',
    })
    expect(btn.className).toMatch(/cursor-help/)
  })
})

// #1192: the four summary cards relocated from Activity to Users. The
// SummaryTile renders a skeleton while loading (value === null) and the
// resolved value once the summary fetch lands. (The card LABELS + the
// getSummary(selectedTeam) wiring live in the page body; this pins the
// tile's loading-vs-loaded behavior.)
describe('SummaryTile (#1192 Users cards)', () => {
  it('renders a skeleton while value is null (loading)', () => {
    const { container } = render(
      <SummaryTile label="Blocked" value={null} sub="Status: blocked" />)
    expect(screen.getByText('Blocked')).toBeInTheDocument()
    expect(screen.getByText('Status: blocked')).toBeInTheDocument()
    // no resolved value yet
    expect(screen.queryByText('0')).toBeNull()
  })

  it('renders the resolved value (number) once loaded', () => {
    render(<SummaryTile label="≥90% of cap" value={3} sub="Approaching cap" />)
    expect(screen.getByText('≥90% of cap')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('renders a formatted money string verbatim', () => {
    render(<SummaryTile label="Total spend" value="$12.34" sub="Month to date" />)
    expect(screen.getByText('Total spend')).toBeInTheDocument()
    expect(screen.getByText('$12.34')).toBeInTheDocument()
  })

  it('a zero value renders (not treated as loading)', () => {
    render(<SummaryTile label="Active users" value={0} sub="Month to date" />)
    expect(screen.getByText('0')).toBeInTheDocument()
  })
})
