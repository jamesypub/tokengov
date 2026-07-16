import React from 'react'

// #750: force_blocked (manual admin override) reads distinctly from
// blocked (auto over-cap) so an admin can tell WHY a user is denied.
const META = {
  active:        { label: 'active',        color: '#16a34a', dot: '🟢', hint: '' },
  blocked:       { label: 'blocked',       color: '#dc2626', dot: '🔴', hint: 'over cap' },
  force_blocked: { label: 'force-blocked', color: '#374151', dot: '⚫', hint: 'admin override — denied regardless of spend' },
}

export default function StatusBadge({ status, sub }) {
  const m = META[status] || { label: status, color: '#6b7280', dot: '·', hint: '' }
  return (
    <span title={m.hint} style={{
      display: 'inline-flex', alignItems: 'center', gap: '0.4em',
      padding: '0.1em 0.6em', borderRadius: '0.6em',
      backgroundColor: m.color + '20',
      color: m.color, fontWeight: 500, fontSize: '0.85em',
    }}>
      <span aria-hidden="true">{m.dot}</span>
      <span>{m.label}</span>
      {sub ? <span style={{ opacity: 0.7, fontWeight: 400 }}>· {sub}</span> : null}
    </span>
  )
}
