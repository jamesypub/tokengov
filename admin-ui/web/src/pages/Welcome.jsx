import React, { useEffect, useState } from 'react'
import { api, fmtUsd } from '../api'

export function Welcome() {
  const [me, setMe] = useState(null)
  const [meErr, setMeErr] = useState(null)
  const [users, setUsers] = useState(null)

  useEffect(() => {
    api.whoami().then(setMe).catch(e => setMeErr(String(e)))
    api.listUsers().then(d => setUsers(d.users || [])).catch(() => {})
  }, [])

  const greeting = me?.email || 'admin'
  const checks = computeChecks(me, meErr, users)

  return (
    <div className="welcome">
      <section className="welcome-hero">
        <div className="welcome-eyebrow">TOKEN GOVERNANCE · Bedrock pilot</div>
        <h1 className="welcome-title">
          Tokens at full throttle.<br/>
          <span className="welcome-title-dim">Spend on rails.</span>
        </h1>
        <p className="welcome-lede">
          Per-user dollar quotas on Amazon Bedrock. Edit one number,
          5 minutes later the IAM Deny lands. Hello, <strong>{greeting}</strong>.
        </p>

        <div className="welcome-cta">
          <a href="#/users" className="btn primary big">Open Users →</a>
          <a href="#/dashboard" className="btn big">View Dashboard</a>
        </div>
      </section>

      <section>
        <div className="welcome-h2">Setup checklist</div>
        <div className="checklist">
          {checks.map((c, i) => (
            <div key={i} className={`check ${c.state}`}>
              <span className="check-mark">{c.state === 'done' ? '✓' : c.state === 'warn' ? '!' : '○'}</span>
              <div className="check-body">
                <div className="check-title">{c.title}</div>
                <div className="check-sub">{c.detail}</div>
                {c.cmd && <pre className="check-cmd">{c.cmd}</pre>}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section>
        <div className="welcome-h2">How it works</div>
        <div className="how-grid">
          <HowCard
            n="1"
            title="Tokens become dollars"
            body="Aggregator job reads /aws/bedrock/invocations every 5 min. Per-(user, model) tokens × your editable price table = estimated cost."
          />
          <HowCard
            n="2"
            title="Postgres is source of truth"
            body="Caps and usage live in tg-admin's Postgres DB. Edit a user anywhere — UI, API, psql — and the reconciler picks it up next tick."
          />
          <HowCard
            n="3"
            title="IAM Deny enforces"
            body="Reconciler sums per-user spend, compares to cap, writes one Deny statement to tg-BedrockQuotaDeny. Bedrock returns 403 on the next call. ~5 min total."
          />
          <HowCard
            n="4"
            title="CUR reconciles"
            body="Real-time estimate uses your price table. CUR 2.0 + Athena (Cost Reports page) is the authoritative bill, 24-48h lag — variance tells you if pricing drifted."
          />
        </div>
      </section>

      <section>
        <div className="welcome-h2">Common tasks</div>
        <div className="tasks">
          <TaskRow href="#/users" title="Cap a user's monthly spend" sub="Click a row → Edit cap → enter dollars → Save" />
          <TaskRow href="#/users" title="Unblock a user temporarily" sub="Click a row → Unblock → pick 24h / 7d / until reset" />
          <TaskRow href="#/users" title="Pre-register an incoming dev" sub="Top-right '+ Pre-register' → email → cap kicks in before first call" />
          <TaskRow href="#/cost-reports" title="Reconcile against the AWS bill" sub="Run the saved Athena query against CUR 2.0 → compare vs. estimate" />
        </div>
      </section>
    </div>
  )
}

function HowCard({ n, title, body }) {
  return (
    <div className="how-card">
      <div className="how-num">{n}</div>
      <div className="how-title">{title}</div>
      <div className="how-body">{body}</div>
    </div>
  )
}

function TaskRow({ href, title, sub }) {
  return (
    <a className="task-row" href={href}>
      <div>
        <div className="task-title">{title}</div>
        <div className="task-sub">{sub}</div>
      </div>
      <div className="task-arrow">→</div>
    </a>
  )
}

function computeChecks(me, meErr, users) {
  const checks = []

  // 1. Auth
  if (meErr) {
    checks.push({
      state: 'warn',
      title: 'Sign in to AWS',
      detail: 'Could not resolve your AWS identity. Run aws sso login + relaunch with AWS_PROFILE=bedrock-admin.',
    })
  } else if (me) {
    const isOrgAdmin = !!me.org_admin
    const isTeamAdmin = !isOrgAdmin && (me.team_ids || []).length > 0
    checks.push({
      state: (isOrgAdmin || isTeamAdmin) ? 'done' : 'warn',
      title: isOrgAdmin
        ? `Signed in as ${me.email} (org_admin)`
        : isTeamAdmin
          ? `Signed in as ${me.email} (team_admin)`
          : `Signed in as ${me.email} (no admin role)`,
      detail: isOrgAdmin || isTeamAdmin
        ? 'Full admin UI available.'
        : 'Contact your org_admin to grant a role.',
    })
  } else {
    checks.push({ state: 'pending', title: 'Resolving identity...', detail: '' })
  }

  // 2. Backend reachability via /api/users
  if (users) {
    const totalSpend = users.reduce((s, u) => s + (u.mtd_cost_usd || 0), 0)
    checks.push({
      state: 'done',
      title: 'Backend reachable',
      detail: `${users.length} user(s) tracked · ${fmtUsd(totalSpend)} estimated spend MTD`,
    })
  } else {
    checks.push({
      state: 'pending',
      title: 'Reaching backend...',
      detail: 'GET /api/users — make sure tg-admin-api is deployed and CC_ADMIN_API_URL is set.',
    })
  }

  return checks
}
