import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { useTeamScope } from '../TeamScope'

// #737 (#726 follow-up): a one-line "spend current as of <ts>"
// freshness stamp for the spend surfaces (UserDetail, Activity,
// Velocity & Cost, Teams). CUR is the sole spend source (#720) and
// is billed-data — delivered with up to a ~24h lag — so every spend
// figure has a server-derived currency watermark
// (max usage_hour in cur_user_spend, via GET /api/cur/data-through).
//
// Renders nothing until the watermark resolves, and nothing if no
// CUR spend has landed yet (the page's own empty state covers that).
// The timestamp is NOT a client-side guess — it comes from the API.
export default function SpendAsOf({ className = '' }) {
  const [ts, setTs] = useState(null)
  const { persona } = useTeamScope()
  // #703: GET /api/cur/data-through is org-admin-only
  // (cur.py require_org_admin), but SpendAsOf is mounted on spend
  // pages every persona can see (V&C, Teams, UserDetail). Firing it
  // unconditionally filled non-admin consoles with 403s on a working
  // page. Gate on the role the SPA already knows from /api/whoami:
  // only org_admin fetches the watermark; for everyone else it simply
  // doesn't render (same outcome as the old .catch, minus the denied
  // round-trip). Mirrors the #868 gating on VelocityCost's getAdminConfig.
  const isOrgAdmin = persona === 'org_admin'
  useEffect(() => {
    if (!isOrgAdmin) { setTs(null); return }
    let alive = true
    // Defensive: a partial api mock (some unit tests) may not
    // stub curDataThrough — degrade to rendering nothing rather
    // than throwing.
    if (typeof api.curDataThrough !== 'function') return
    api.curDataThrough()
      .then(d => { if (alive) setTs(d?.data_through || null) })
      .catch(() => {})
    return () => { alive = false }
  }, [isOrgAdmin])

  if (!ts) return null
  let label = ts
  try {
    // Compact, locale-aware: "Jun 7, 2026, 08:00 UTC".
    label = new Date(ts).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
    }) + ' UTC'
  } catch { /* fall back to the raw ISO string */ }

  return (
    <div className={`text-[12px] text-[var(--ink-4)] ${className}`}>
      Spend current as of {label} · billed CUR data (~24h lag)
    </div>
  )
}
