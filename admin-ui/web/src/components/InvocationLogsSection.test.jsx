import React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

const getInvocationLogs = vi.fn()
const setInvocationLogs = vi.fn()
vi.mock('../api', () => ({
  api: {
    getInvocationLogs: (...a) => getInvocationLogs(...a),
    setInvocationLogs: (...a) => setInvocationLogs(...a),
  },
}))

import InvocationLogsSection, { applyOutcomeLabel }
  from './InvocationLogsSection'

beforeEach(() => {
  getInvocationLogs.mockReset()
  setInvocationLogs.mockReset()
})

describe('applyOutcomeLabel', () => {
  it('maps outcomes to plain language', () => {
    expect(applyOutcomeLabel('enabled')).toMatch(/capturing/)
    expect(applyOutcomeLabel('already_enabled')).toMatch(/already active/)
    expect(applyOutcomeLabel('not_ours')).toMatch(/different logging config/)
    expect(applyOutcomeLabel('failed')).toMatch(/retry/)
  })
})

describe('InvocationLogsSection', () => {
  it('empty catalog → off state, no privacy warning', async () => {
    getInvocationLogs.mockResolvedValue({ regions: [], updated_at: null })
    render(<InvocationLogsSection />)
    expect(await screen.findByText(/invocation logging is off/i))
      .toBeInTheDocument()
    expect(screen.queryByText(/Privacy:/)).toBeNull()
  })

  it('an enabled region with Text on shows the unmissable privacy warning', async () => {
    getInvocationLogs.mockResolvedValue({
      regions: [{ region: 'us-east-1', bucket: 'b', enabled: true, text_on: true }],
      updated_at: '2026-07-02T00:00:00Z',
    })
    render(<InvocationLogsSection />)
    const note = await screen.findByRole('note')
    // conveyed by text (source code / AI output), not color alone
    expect(note.textContent).toMatch(/source code and AI output/i)
    expect(note.textContent).toMatch(/Privacy/i)
  })

  it('add-region validates the region format', async () => {
    getInvocationLogs.mockResolvedValue({ regions: [], updated_at: null })
    render(<InvocationLogsSection />)
    await screen.findByText(/invocation logging is off/i)
    fireEvent.change(screen.getByLabelText(/Add a region/i),
      { target: { value: 'not-a-region' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(screen.getByRole('alert').textContent).toMatch(/valid AWS region/i)
  })

  it('save posts the catalog and shows per-region apply outcomes', async () => {
    getInvocationLogs.mockResolvedValue({ regions: [], updated_at: null })
    setInvocationLogs.mockResolvedValue({
      regions: [{ region: 'us-east-1', bucket: 'b', enabled: true, text_on: true }],
      apply: [{ region: 'us-east-1', outcome: 'enabled' }],
    })
    render(<InvocationLogsSection />)
    await screen.findByText(/invocation logging is off/i)
    fireEvent.change(screen.getByLabelText(/Add a region/i),
      { target: { value: 'us-east-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    fireEvent.click(screen.getByRole('button', { name: /Save & apply/i }))
    await waitFor(() => expect(setInvocationLogs).toHaveBeenCalledTimes(1))
    // sends region + enabled + text_on (bucket is server-derived)
    expect(setInvocationLogs).toHaveBeenCalledWith(
      [{ region: 'us-east-1', enabled: true, text_on: true }])
    expect(await screen.findByText(/capturing/)).toBeInTheDocument()
  })

  it('Text toggle disabled when a region is not capturing', async () => {
    getInvocationLogs.mockResolvedValue({
      regions: [{ region: 'us-east-1', bucket: 'b', enabled: false, text_on: true }],
      updated_at: null,
    })
    render(<InvocationLogsSection />)
    const textToggle = await screen.findByLabelText(/Capture prompt and response text/i)
    expect(textToggle).toBeDisabled()
    // not enabled → no privacy warning even though text_on is true
    expect(screen.queryByRole('note')).toBeNull()
  })

  it('title is "Bedrock invocation logs" and copy names the customer account', async () => {
    getInvocationLogs.mockResolvedValue({ regions: [], updated_at: null })
    render(<InvocationLogsSection />)
    expect(
      await screen.findByRole('heading', { name: /^Bedrock invocation logs$/i })
    ).toBeInTheDocument()
    // copy reinforces the data stays in the customer's own account
    expect(screen.getByText(/your own AWS account’s S3/i)).toBeInTheDocument()
    expect(screen.getByText(/your account’s security/i)).toBeInTheDocument()
  })

  it('shows the full S3 path for an ENABLED region, hides it for an off one', async () => {
    getInvocationLogs.mockResolvedValue({
      regions: [
        { region: 'us-east-1', bucket: 'tg-bedrock-invlogs-us-east-1-123456789012',
          s3_uri: 's3://tg-bedrock-invlogs-us-east-1-123456789012',
          enabled: true, text_on: false },
        { region: 'eu-west-1', bucket: 'tg-bedrock-invlogs-eu-west-1-123456789012',
          s3_uri: 's3://tg-bedrock-invlogs-eu-west-1-123456789012',
          enabled: false, text_on: false },
      ],
      updated_at: '2026-07-03T00:00:00Z',
    })
    render(<InvocationLogsSection />)
    // enabled region → full s3:// path is shown
    expect(
      await screen.findByText(/s3:\/\/tg-bedrock-invlogs-us-east-1-123456789012/)
    ).toBeInTheDocument()
    // off region → its path is NOT shown (consistent with the off state)
    expect(
      screen.queryByText(/s3:\/\/tg-bedrock-invlogs-eu-west-1-123456789012/)
    ).toBeNull()
  })
})
