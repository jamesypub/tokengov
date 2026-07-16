import React, { useEffect, useState } from 'react'
import { api, fmtUsd } from '../api'
import { Card } from '../ui/Card'
import { Button } from '../ui/Button'
import { Input } from '../ui/Input'

// #929: the member-facing view — the lowest-privilege authenticated
// surface. A member sees ONLY their own usage/spend and can self-edit
// two fields (display name + GitHub link). NO admin nav, NO other-user
// data. Enforcement is server-side (the #927 member Scope + the #929
// get_user self-allow); this page just renders what the member's own
// endpoints return. A member who somehow hits an admin endpoint gets a
// server 403 — the UI never assumes trust.
export default function MemberHome({ me }) {
  const email = me?.email
  const [user, setUser] = useState(null)
  const [linked, setLinked] = useState([])
  const [err, setErr] = useState(null)
  // #942: a 404 on the member's OWN /api/users/<self> is not an error —
  // it's the "authenticated but not yet provisioned/discovered" state
  // (login works, but no users row yet: pre-discovery, or an admin
  // hasn't enabled them). Render a graceful onboarding empty-state, not
  // the raw "User <email> not found" string. Tracked separately from
  // `err` so a genuine failure (500/network) still shows the banner.
  const [notSetUp, setNotSetUp] = useState(false)
  const [loading, setLoading] = useState(true)

  async function load() {
    if (!email) return
    setLoading(true); setErr(null); setNotSetUp(false)
    try {
      const u = await api.getUser(email)
      setUser(u)
      try {
        setLinked(await api.getLinkedAccounts(email))
      } catch { setLinked([]) }   // linked-accounts flag may be off
    } catch (e) {
      // 404 = not provisioned yet → empty-state, not an error banner.
      if (e && e.status === 404) setNotSetUp(true)
      else setErr(String(e.message || e))
    }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [email])

  if (!email) return null
  return (
    <div className="min-h-screen bg-[var(--bg)] text-[var(--ink)]">
      <header className="border-b px-6 py-4 flex items-center justify-between">
        <div className="font-semibold">Token Governance</div>
        <a href="/auth/logout"
           className="text-sm text-[var(--accent)] hover:underline">
          Sign out
        </a>
      </header>
      <main className="max-w-2xl mx-auto px-6 py-8">
        <h1 className="text-xl font-bold mb-1">Your usage</h1>
        <p className="text-sm text-[var(--ink-3)] mb-6">
          Signed in as {email}. You can see your own usage and update
          your profile below.
        </p>
        {err && (
          <div className="bg-red-50 border border-red-300 text-red-800 px-3 py-2 rounded text-sm mb-4">
            {err}
          </div>
        )}
        {loading ? (
          <p className="text-sm text-[var(--ink-3)]">Loading…</p>
        ) : notSetUp ? (
          <NotSetUpCard />
        ) : user && (
          <>
            <UsageCard user={user} />
            <ProfileCard user={user} linked={linked} onSaved={load} />
          </>
        )}
      </main>
    </div>
  )
}

// #942: the "authenticated but not yet provisioned" empty-state. Shown
// when the member's own /api/users/<self> 404s — their login works, but
// tg has no usage row for them yet (pre-discovery, or not enabled by an
// admin). Plain-language guidance, not a raw error (#9 recognize/recover,
// #1 system status). Mirrors the Cost-page CUR-delay notice tone.
function NotSetUpCard() {
  return (
    <Card className="p-5">
      <h2 className="text-base font-semibold mb-2">
        Your usage isn’t set up yet
      </h2>
      <p className="text-sm text-[var(--ink-3)] mb-2">
        Your login works — but tg doesn’t have any usage recorded for
        you yet. This is normal if you’ve just been given access or
        haven’t used Bedrock through tg yet.
      </p>
      <p className="text-sm text-[var(--ink-3)]">
        Billed usage data arrives from AWS up to ~24 hours after your
        first use and will show up here automatically. If it’s been
        longer, ask your admin to confirm your access is enabled.
      </p>
    </Card>
  )
}

function UsageCard({ user }) {
  const cap = user.effective_quota_usd
  const spend = user.mtd_spend_usd || 0
  const pct = user.pct_used
  return (
    <Card className="p-5 mb-5">
      <h2 className="text-base font-semibold mb-3">This month</h2>
      <div className="grid grid-cols-3 gap-4 text-sm">
        <div>
          <div className="text-[var(--ink-3)]">Spend (MTD)</div>
          <div className="text-lg font-semibold">{fmtUsd(spend)}</div>
        </div>
        <div>
          <div className="text-[var(--ink-3)]">Your cap</div>
          <div className="text-lg font-semibold">
            {cap ? fmtUsd(cap) : '—'}
          </div>
        </div>
        <div>
          <div className="text-[var(--ink-3)]">Used</div>
          <div className="text-lg font-semibold">
            {pct == null ? '—' : `${pct}%`}
          </div>
        </div>
      </div>
      {user.status && user.status !== 'active' && (
        <p className="text-sm text-amber-700 mt-3">
          Status: {user.status} — contact your admin if you need access
          restored.
        </p>
      )}
    </Card>
  )
}

function ProfileCard({ user, linked, onSaved }) {
  const gh = (linked || []).find(l => l.vendor === 'github')
  const [name, setName] = useState(user.display_name || '')
  const [handle, setHandle] = useState(gh?.external_handle || '')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [err, setErr] = useState(null)

  async function saveName() {
    setBusy(true); setMsg(null); setErr(null)
    try {
      await api.setDisplayName(user.email, name || null, user.version)
      setMsg('Display name saved.'); onSaved()
    } catch (e) { setErr(String(e.message || e)) }
    finally { setBusy(false) }
  }
  async function saveHandle() {
    setBusy(true); setMsg(null); setErr(null)
    try {
      await api.putLinkedAccount(user.email, 'github',
        { external_handle: handle.trim() })
      setMsg('GitHub link saved.'); onSaved()
    } catch (e) { setErr(String(e.message || e)) }
    finally { setBusy(false) }
  }

  return (
    <Card className="p-5">
      <h2 className="text-base font-semibold mb-3">Your profile</h2>
      {msg && (
        <div className="bg-emerald-50 border border-emerald-300 text-emerald-800 px-3 py-2 rounded text-sm mb-3">
          {msg}
        </div>
      )}
      {err && (
        <div className="bg-red-50 border border-red-300 text-red-800 px-3 py-2 rounded text-sm mb-3">
          {err}
        </div>
      )}
      <div className="mb-4">
        <label className="block text-sm font-medium mb-1">Display name</label>
        <div className="flex gap-2">
          <Input value={name} onChange={e => setName(e.target.value)}
                 disabled={busy} placeholder="Your name" />
          <Button variant="secondary" disabled={busy} onClick={saveName}>
            Save
          </Button>
        </div>
      </div>
      <div>
        <label className="block text-sm font-medium mb-1">GitHub username</label>
        <div className="flex gap-2">
          <Input value={handle} onChange={e => setHandle(e.target.value)}
                 disabled={busy} placeholder="your-github-handle" />
          <Button variant="secondary"
                  disabled={busy || !handle.trim()} onClick={saveHandle}>
            Save
          </Button>
        </div>
        <p className="text-xs text-[var(--ink-3)] mt-1">
          Links your GitHub PRs to your usage for velocity reporting.
        </p>
      </div>
    </Card>
  )
}
