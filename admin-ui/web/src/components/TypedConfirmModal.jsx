import React, { useState } from 'react'

/**
 * Typed-confirmation modal: user must type the exact match string before
 * the destructive button activates. Used for Disable user + Remove org_admin.
 *
 * Props:
 *   open         boolean
 *   title        string
 *   bodyText     string  (the warning copy)
 *   highlightText string (optional #947 — a short line rendered as a
 *                  prominent callout above the body, e.g. an
 *                  enforcement-timing notice. Real text, so it's
 *                  screen-reader reachable — not an icon/color cue.)
 *   matchString  string  (what the user must type)
 *   confirmLabel string
 *   onConfirm    () => void
 *   onCancel     () => void
 */
export default function TypedConfirmModal({
  open, title, bodyText, highlightText, matchString,
  confirmLabel = 'Confirm', onConfirm, onCancel,
}) {
  const [text, setText] = useState('')
  if (!open) return null
  const ok = text === matchString
  return (
    <div style={overlay} role="dialog" aria-modal="true" aria-label={title}>
      <div style={modal}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h3 style={{ margin: 0 }}>{title}</h3>
          <button onClick={onCancel} aria-label="Close" style={closeBtn}>✕</button>
        </div>
        {highlightText && (
          <p style={highlight} role="note">{highlightText}</p>
        )}
        <p style={{ marginTop: '1em', whiteSpace: 'pre-wrap' }}>{bodyText}</p>
        <p style={{ fontSize: '0.9em', color: '#374151' }}>
          Type <code style={code}>{matchString}</code> to confirm:
        </p>
        <input
          autoFocus
          value={text}
          onChange={(e) => setText(e.target.value)}
          style={input}
          placeholder={matchString}
        />
        <div style={{ marginTop: '1em', display: 'flex', justifyContent: 'flex-end', gap: '0.5em' }}>
          <button onClick={onCancel} style={cancelBtn}>Cancel</button>
          <button
            onClick={() => { if (ok) onConfirm() }}
            disabled={!ok}
            style={ok ? destructiveBtn : destructiveBtnDisabled}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}

const overlay = {
  position: 'fixed', inset: 0,
  background: 'rgba(0,0,0,0.4)',
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  zIndex: 1000,
}
const modal = {
  background: 'white', padding: '2em', borderRadius: '0.75em',
  maxWidth: '480px', width: '90%',
  boxShadow: '0 8px 32px rgba(0,0,0,0.2)',
}
const closeBtn = { background: 'none', border: 'none', fontSize: '1.2em', cursor: 'pointer' }
const code = {
  background: '#f3f4f6', padding: '0.1em 0.4em', borderRadius: '0.3em',
  fontFamily: 'ui-monospace, monospace',
}
const input = {
  width: '100%', padding: '0.6em', borderRadius: '0.4em',
  border: '1px solid #d1d5db', fontFamily: 'ui-monospace, monospace',
  fontSize: '1em',
}
const cancelBtn = { padding: '0.5em 1em', borderRadius: '0.4em', border: '1px solid #d1d5db', background: 'white', cursor: 'pointer' }
const destructiveBtn = { padding: '0.5em 1em', borderRadius: '0.4em', border: 'none', background: '#dc2626', color: 'white', cursor: 'pointer', fontWeight: 600 }
const destructiveBtnDisabled = { ...destructiveBtn, background: '#fca5a5', cursor: 'not-allowed' }
// #947: prominent enforcement-timing callout — bordered amber line so
// the admin sees at a glance the action isn't instant. Real text
// (role=note), not a color-only cue.
const highlight = {
  marginTop: '1em', marginBottom: 0,
  padding: '0.6em 0.8em', borderRadius: '0.4em',
  border: '1px solid #fcd34d', background: '#fffbeb',
  color: '#92400e', fontWeight: 600, fontSize: '0.95em',
}
