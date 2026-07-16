import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import TypedConfirmModal from './TypedConfirmModal'

const baseProps = {
  open: true,
  title: 'Disable user',
  bodyText: 'This will revoke their permission.',
  matchString: 'alice@example.com',
  confirmLabel: 'Disable',
  onConfirm: vi.fn(),
  onCancel: vi.fn(),
}

describe('TypedConfirmModal', () => {
  it('renders nothing when open=false', () => {
    const { container } = render(<TypedConfirmModal {...baseProps} open={false} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders title and body text when open', () => {
    render(<TypedConfirmModal {...baseProps} />)
    expect(screen.getByText('Disable user')).toBeInTheDocument()
    expect(screen.getByText(/will revoke their permission/)).toBeInTheDocument()
  })

  it('confirm button is disabled until exact email is typed', async () => {
    const user = userEvent.setup()
    const onConfirm = vi.fn()
    render(<TypedConfirmModal {...baseProps} onConfirm={onConfirm} />)
    const button = screen.getByRole('button', { name: /^Disable$/ })
    expect(button).toBeDisabled()

    const input = screen.getByPlaceholderText('alice@example.com')
    await user.type(input, 'wrong@example.com')
    expect(button).toBeDisabled()

    await user.clear(input)
    await user.type(input, 'alice@example.com')
    expect(button).not.toBeDisabled()
  })

  it('calls onConfirm only when match string is typed and button clicked', async () => {
    const user = userEvent.setup()
    const onConfirm = vi.fn()
    render(<TypedConfirmModal {...baseProps} onConfirm={onConfirm} />)

    await user.type(screen.getByPlaceholderText('alice@example.com'), 'alice@example.com')
    await user.click(screen.getByRole('button', { name: /^Disable$/ }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('cancel button calls onCancel', async () => {
    const user = userEvent.setup()
    const onCancel = vi.fn()
    render(<TypedConfirmModal {...baseProps} onCancel={onCancel} />)
    await user.click(screen.getByRole('button', { name: /Cancel/ }))
    expect(onCancel).toHaveBeenCalled()
  })

  it('close X button calls onCancel', async () => {
    const user = userEvent.setup()
    const onCancel = vi.fn()
    render(<TypedConfirmModal {...baseProps} onCancel={onCancel} />)
    await user.click(screen.getByLabelText('Close'))
    expect(onCancel).toHaveBeenCalled()
  })

  it('does not call onConfirm if disabled button is clicked', async () => {
    const user = userEvent.setup()
    const onConfirm = vi.fn()
    render(<TypedConfirmModal {...baseProps} onConfirm={onConfirm} />)
    const btn = screen.getByRole('button', { name: /^Disable$/ })
    // Even with explicit click, browser respects disabled
    fireEvent.click(btn)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  // #947: optional prominent callout (e.g. enforcement-timing notice).
  it('renders highlightText as real text (role=note) when given', () => {
    render(<TypedConfirmModal
      {...baseProps}
      highlightText="⏳ Takes effect within a few minutes" />)
    const note = screen.getByRole('note')
    expect(note).toHaveTextContent(/Takes effect within a few minutes/)
  })

  it('omits the highlight callout when no highlightText', () => {
    render(<TypedConfirmModal {...baseProps} />)
    expect(screen.queryByRole('note')).toBeNull()
  })
})
