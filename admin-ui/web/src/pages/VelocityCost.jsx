import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { useTeamScope } from '../TeamScope'
import { Card } from '../ui/Card'
import { Button } from '../ui/Button'
import SpendAsOf from '../components/SpendAsOf'

const WINDOWS = [
  { key: '7d',  label: '7d'  },
  { key: '30d', label: '30d' },
  { key: '90d', label: '90d' },
  { key: 'ytd', label: 'YTD' },
]

const COST_SORTS = [
  { key: 'cheapest',  label: 'cheapest $/PR' },
  { key: 'most_prs',  label: 'most PRs' },
  { key: 'top_spend', label: 'highest spend' },
]
const SPEED_SORTS = [
  { key: 'fastest_median', label: 'fastest median' },
  { key: 'fastest_p90',    label: 'fastest P90' },
  { key: 'cheapest',       label: 'cheapest $/PR' },
]
const SPEED_TYPES = [
  { key: 'all',   label: 'All',   dot: 'var(--ink-5)' },
  { key: 'story', label: 'Story', dot: 'var(--type-story, #4f46e5)' },
  { key: 'bug',   label: 'Bug',   dot: 'var(--type-bug, #dc2626)' },
  { key: 'task',  label: 'Task',  dot: 'var(--type-task, #525252)' },
]

function tabFromHash(path) {
  if (path.endsWith('/speed')) return 'speed'
  if (path.endsWith('/jira'))  return 'jira'
  return 'cost'
}

export default function VelocityCost() {
  const [tab, setTab] = useState(tabFromHash(window.location.hash))
  const [win, setWin] = useState('30d')
  const [type, setType] = useState('all')
  const [costSort, setCostSort] = useState('cheapest')
  const [speedSort, setSpeedSort] = useState('fastest_median')
  const [search, setSearch] = useState('')
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [loading, setLoading] = useState(true)
  // #447: the Jira tab + route are gated behind the runtime
  // admin_config flag jira_enabled (toggled in Org Settings →
  // Experimental features). Default OFF until V1.3 lands. Read
  // from /admin/config — the same store page settings use, so
  // toggling needs no env var and no redeploy.
  const [jiraEnabled, setJiraEnabled] = useState(false)
  const { selectedTeam, persona } = useTeamScope()

  // #703: /admin/config is org-admin-only (403 for team_admin /
  // member). Velocity & Cost is visible to every persona, so firing
  // it unconditionally filled non-admin consoles with 403s on a
  // working page. Gate on the role the SPA already knows from
  // /api/whoami: only org_admin reads the flag; for everyone else
  // jira_enabled stays its default OFF (same outcome as the old
  // .catch, minus the denied round-trip).
  const isOrgAdmin = persona === 'org_admin'

  useEffect(() => {
    if (!isOrgAdmin) { setJiraEnabled(false); return }
    let alive = true
    api.getAdminConfig()
      .then(d => { if (alive) setJiraEnabled(!!d?.jira_enabled) })
      .catch(() => { if (alive) setJiraEnabled(false) })
    return () => { alive = false }
  }, [isOrgAdmin])

  useEffect(() => {
    function onHash() {
      setTab(tabFromHash(window.location.hash))
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // Effective tab: a deep-link to #/velocity-cost/jira while the
  // feature is OFF falls back to Cost (#447). All render + fetch
  // logic keys off effTab so the disabled Jira panel never shows.
  const effTab = (tab === 'jira' && !jiraEnabled) ? 'cost' : tab

  useEffect(() => {
    if (effTab === 'jira') {
      setLoading(false); setErr(null); setData(null)
      return
    }
    setLoading(true); setErr(null)
    const p = effTab === 'speed'
      ? api.velocitySpeed(win, type, selectedTeam)
      : api.velocityLeaderboard(win, selectedTeam)
    p.then(setData)
     .catch(e => setErr(String(e)))
     .finally(() => setLoading(false))
  }, [effTab, win, type, selectedTeam])

  const teams = data?.teams || []
  const noData = !loading && teams.length === 0
  // Distinguish "rollup hasn't seen any activity yet" from
  // "rollup ran but cycle stats are still NULL" — the second
  // case means github_sync landed PRs but the chained rollup
  // hasn't filled cycle_*_hours yet (or did but filtered the
  // teams out for this view's filter). #215
  const totalIssues = data?.org?.total_issues || 0
  const compiling = noData && totalIssues > 0

  return (
    <div className="vc-page">
      <header className="vc-page__head">
        <div>
          <h1 className="vc-page__title">Velocity & Cost</h1>
          <p className="vc-page__sub">
            Per-team merge throughput and Bedrock spend.
            Sourced from <code>github_activity</code> and Bedrock CUR
            via Athena. Updated every 30 minutes.
          </p>
          <SpendAsOf className="mt-1" />
        </div>
      </header>

      <div className="vc-strip">
        <a
          href="#/velocity-cost/cost"
          className={'vc-strip__tab ' + (effTab === 'cost' ? 'is-active' : '')}
        >
          <span className="vc-strip__icon">$</span>
          <span>
            <span className="vc-strip__label">Cost</span>
            <span className="vc-strip__sub">$/PR by team</span>
          </span>
        </a>
        <a
          href="#/velocity-cost/speed"
          className={'vc-strip__tab ' + (effTab === 'speed' ? 'is-active' : '')}
        >
          <span className="vc-strip__icon">⏱</span>
          <span>
            <span className="vc-strip__label">Speed</span>
            <span className="vc-strip__sub">Cycle time by class</span>
          </span>
        </a>
        {jiraEnabled && (
          <a
            href="#/velocity-cost/jira"
            className={'vc-strip__tab ' + (effTab === 'jira' ? 'is-active' : '')}
          >
            <span className="vc-strip__icon">J</span>
            <span>
              <span className="vc-strip__label">Jira</span>
              <span className="vc-strip__sub">Sprint / Epic / $/SP</span>
            </span>
          </a>
        )}
      </div>

      <div className="vc-controls">
        <div className="vc-seg">
          {WINDOWS.map(w => (
            <button
              key={w.key}
              className={'vc-seg__btn ' + (win === w.key ? 'is-active' : '')}
              onClick={() => setWin(w.key)}
            >{w.label}</button>
          ))}
        </div>
        {effTab === 'speed' && (
          <div className="vc-seg">
            {SPEED_TYPES.map(t => (
              <button
                key={t.key}
                className={'vc-seg__btn ' + (type === t.key ? 'is-active' : '')}
                onClick={() => setType(t.key)}
              >
                <span className="vc-seg__dot" style={{ background: t.dot }} />
                {t.label}
              </button>
            ))}
          </div>
        )}
        {effTab !== 'jira' && (
          <div className="vc-sorts">
            {(effTab === 'speed' ? SPEED_SORTS : COST_SORTS).map(s => {
              const v = effTab === 'speed' ? speedSort : costSort
              const set = effTab === 'speed' ? setSpeedSort : setCostSort
              return (
                <button
                  key={s.key}
                  className={'vc-chip ' + (v === s.key ? 'is-active' : '')}
                  onClick={() => set(s.key)}
                >{s.label}</button>
              )
            })}
            <input
              className="vc-search"
              type="search"
              placeholder="filter teams…"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
          </div>
        )}
      </div>

      {err && (
        <Card className="vc-empty">
          <div className="vc-empty__title">Error</div>
          <div className="vc-empty__sub">{err}</div>
        </Card>
      )}

      {loading && !err && effTab !== 'jira' && (
        <Card className="vc-empty">
          <div className="vc-empty__title">Loading…</div>
        </Card>
      )}

      {noData && !err && !compiling && effTab !== 'jira' && (
        <Card className="vc-empty">
          <div className="vc-empty__title">
            First rollup running…
          </div>
          <div className="vc-empty__sub">
            Configure a GitHub repo in{' '}
            <a href="#/integrations" className="vc-link">
              Integrations
            </a>
            {' '}to populate the leaderboard. After the first sync,
            data appears within ~30 minutes.
          </div>
        </Card>
      )}

      {compiling && !err && effTab !== 'jira' && (
        <Card className="vc-empty">
          <div className="vc-empty__title">
            Cycle stats compiling — refresh in a minute.
          </div>
          <div className="vc-empty__sub">
            {totalIssues.toLocaleString()} merged PRs detected for
            this window, but per-team cycle medians haven't landed
            yet. The next rollup tick fills them in.
          </div>
        </Card>
      )}

      {!loading && !err && teams.length > 0 && effTab === 'cost' && (
        <CostView
          data={data} sort={costSort} search={search}
          window_={win} type={type}
        />
      )}
      {!loading && !err && teams.length > 0 && effTab === 'speed' && (
        <SpeedView data={data} sort={speedSort} type={type} search={search} />
      )}

      {effTab === 'jira' && !err && (
        <JiraView window_={win} selectedTeam={selectedTeam} />
      )}

      <div className="vc-footnote">
        $/PR is derived as <code>spend ÷ PRs merged</code>.
        Cycle time is the median hours from issue-open to PR-merge.
        Per-class cost attribution is deferred to v1.5 — the
        $/PR column is team-aggregate across all classes.
      </div>
    </div>
  )
}

function CostView({ data, sort, search, window_, type }) {
  const [selectedTeam, setSelectedTeam] = useState(null)
  const org = data.org || {}
  let teams = (data.teams || []).slice()
  if (search?.trim()) {
    const q = search.trim().toLowerCase()
    teams = teams.filter(t =>
      (t.name || '').toLowerCase().includes(q) ||
      (t.team_id || '').toLowerCase().includes(q) ||
      (t.repos || []).some(r => r.toLowerCase().includes(q))
    )
  }
  if (sort === 'cheapest') {
    teams.sort((a, b) =>
      (a.dollar_per_pr || Infinity) - (b.dollar_per_pr || Infinity))
  } else if (sort === 'most_prs') {
    teams.sort((a, b) => (b.prs_merged || 0) - (a.prs_merged || 0))
  } else if (sort === 'top_spend') {
    teams.sort((a, b) => (b.spend_usd || 0) - (a.spend_usd || 0))
  }

  const selectedTeamObj = selectedTeam
    ? (data.teams || []).find(t => t.team_id === selectedTeam)
    : null

  // #810: V&C now reports ALL Bedrock spend, not just GitHub-linked
  // devs. `total_spend_usd` is the bill-reconciling SUM over every
  // principal (matches the Users page + AWS bill); `unlinked` lists
  // the role/machine/federated-session principals with spend but no
  // GitHub link. Fall back to the PR-attributed total when the
  // backend didn't send the all-spend figure (team-scoped caller).
  const unlinked = data.unlinked || []
  const totalSpend = org.total_spend_usd != null
    ? org.total_spend_usd : org.spend_usd

  return (
    <>
      <div className="vc-summary">
        <SummaryCard label="org $/PR" value={fmtMoney(org.dollar_per_pr)} />
        <SummaryCard label="total spend" value={fmtMoney(totalSpend)} />
        <SummaryCard label="PRs merged" value={(org.prs_merged || 0).toLocaleString()} />
        <SummaryCard
          label="trend vs prev"
          value={fmtTrend(org.trend_pct_vs_prev)}
        />
      </div>

      {selectedTeam ? (
        <>
          <div className="vc-breadcrumb">
            <button
              className="vc-breadcrumb__back"
              onClick={() => setSelectedTeam(null)}
            >
              ← All teams
            </button>
            <span className="vc-breadcrumb__sep"> / </span>
            <span className="vc-breadcrumb__current">
              {selectedTeamObj?.name || selectedTeam}
            </span>
          </div>
          <MixLegend />
          <UserDrillDown
            tab="cost" team_id={selectedTeam}
            window_={window_} type={type}
          />
        </>
      ) : (
        <Card className="vc-table-card">
          <MixLegend />
          <table className="vc-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Team</th>
                <th className="num">PRs</th>
                <th className="num">Spend</th>
                <th className="num">$ / PR</th>
                <th className="num">Budget</th>
              </tr>
            </thead>
            <tbody>
              {teams.length === 0 && (
                <tr>
                  <td colSpan="6" style={{
                    padding: '24px', textAlign: 'center',
                    fontSize: '13px', color: 'var(--ink-4)',
                  }}>
                    No teams match.
                  </td>
                </tr>
              )}
              {teams.map((t, i) => (
                <tr key={t.team_id}>
                  <td className="vc-rank">{i + 1}</td>
                  <td>
                    <button
                      className="vc-team-link"
                      onClick={() => setSelectedTeam(t.team_id)}
                      title={`View users in ${t.name}`}
                    >
                      <div className="vc-team-name">{t.name}</div>
                    </button>
                    <div className="vc-team-sub">
                      {t.devs} devs · {t.repos?.length || 0} repos
                    </div>
                    <MixBar mix={t.mix_pct} />
                  </td>
                  <td className="num">{t.prs_merged || 0}</td>
                  <td className="num">{fmtMoney(t.spend_usd)}</td>
                  <td className="num">{fmtMoney(t.dollar_per_pr)}</td>
                  <td className="num">
                    {t.budget_usd != null
                      ? fmtMoney(t.budget_usd) : ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {!selectedTeam && (
        <UnlinkedSpend rows={unlinked} total={totalSpend} />
      )}
    </>
  )
}

/* #810: the "Unlinked / service" section — every principal with
   Bedrock spend that is NOT a linked GitHub dev (role sessions,
   machine roles, federated sessions). Keyed on the role-session-name
   verbatim, so tg-org-admin+dev@ / tg-org-admin+ops@ each appear as their own
   row. PR metrics are NA (these principals have no GitHub-attributed
   PRs). The whole point: V&C Cost now reflects ALL Bedrock spend, so
   the total reconciles to the Users page + the AWS bill. */
function UnlinkedSpend({ rows, total }) {
  if (!rows || rows.length === 0) return null
  const sum = rows.reduce((a, r) => a + (Number(r.spend_usd) || 0), 0)
  return (
    <Card className="vc-table-card" style={{ marginTop: 16 }}>
      <div className="vc-mix-legend">
        <span className="vc-mix-legend__label">
          Unlinked / service principals
        </span>
        <span className="vc-mix-legend__item" style={{ color: 'var(--ink-4)' }}>
          spend with no GitHub link — role / machine / federated
          sessions. Included in total spend{total != null
            ? ` (${fmtMoney(total)})` : ''} so it reconciles to the
          AWS bill.
        </span>
      </div>
      <table className="vc-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Principal</th>
            <th>Type</th>
            <th className="num">Spend</th>
            <th className="num">$ / PR</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.identity_key || r.email}>
              <td className="vc-rank">{i + 1}</td>
              <td><code>{r.identity_key || r.email}</code></td>
              <td>{r.is_service ? 'service' : (r.principal_type || '—')}</td>
              <td className="num">{fmtMoney(r.spend_usd)}</td>
              <td className="num">—</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}

function UserDrillDown({ tab, team_id, window_, type }) {
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    setErr(null); setRows(null)
    if (tab === 'cost') {
      api.velocityLeaderboardUsers(
        team_id, window_ || '30d', type || 'all',
      )
        .then(d => setRows(d?.users || []))
        .catch(e => setErr(String(e)))
    } else {
      api.velocitySpeedUsers(team_id, window_ || '30d', type)
        .then(d => setRows(d?.users || []))
        .catch(e => setErr(String(e)))
    }
  }, [tab, team_id, window_, type])

  if (err) return (
    <Card className="vc-empty">
      <div className="vc-empty__title">Error</div>
      <div className="vc-empty__sub">{err}</div>
    </Card>
  )
  if (!rows) return (
    <Card className="vc-empty">
      <div className="vc-empty__title">Loading…</div>
    </Card>
  )
  if (rows.length === 0) return (
    <Card className="vc-empty">
      <div className="vc-empty__sub" style={{ padding: 24 }}>
        {tab === 'cost'
          ? 'No PR data for this team yet.'
          : 'No user data for this team in the selected window.'}
      </div>
    </Card>
  )

  if (tab === 'cost') {
    return (
      <Card className="vc-table-card">
        <table className="vc-table">
          <thead>
            <tr>
              <th>#</th>
              <th>User</th>
              <th className="num">PRs</th>
              <th className="num">Spend</th>
              <th className="num">$ / PR</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.email}>
                <td className="vc-rank">{i + 1}</td>
                <td>
                  <div className="vc-team-name">
                    <code>{r.email}</code>
                  </div>
                  <MixBar mix={r.mix_pct} />
                </td>
                <td className="num">{r.prs_merged || 0}</td>
                <td className="num">{fmtMoney(r.spend_usd)}</td>
                <td className="num">
                  {r.prs_merged > 0 && r.dollar_per_pr != null
                    ? fmtMoney(r.dollar_per_pr) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    )
  }

  return (
    <Card className="vc-table-card">
      <table className="vc-table">
        <thead>
          <tr>
            <th>#</th>
            <th>User</th>
            <th className="num">PRs</th>
            <th className="num">Median</th>
            <th className="num">P90</th>
            <th className="num">$ / PR</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.email}>
              <td className="vc-rank">{i + 1}</td>
              <td><code>{r.email}</code></td>
              <td className="num">{r.prs_merged || 0}</td>
              <td className="num">{fmtDays(r.median_hours)}</td>
              <td className="num">{fmtDays(r.p90_hours)}</td>
              <td className="num">
                {r.dollar_per_pr != null
                  ? fmtMoney(r.dollar_per_pr) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}

function fmtMoney(v) {
  const n = Number(v) || 0
  if (n === 0) return '$0'
  if (n < 1) return `$${n.toFixed(2)}`
  if (n >= 1000) return `$${Math.round(n).toLocaleString()}`
  return `$${Math.round(n)}`
}

function fmtTrend(pct) {
  const p = Number(pct) || 0
  if (p === 0) return '· flat'
  return `${p > 0 ? '▲' : '▼'} ${Math.abs(p)}%`
}

function MixLegend() {
  return (
    <div className="vc-mix-legend">
      <span className="vc-mix-legend__label">PR type:</span>
      <span className="vc-mix-legend__item">
        <span className="vc-dot vc-dot--story" />
        story
      </span>
      <span className="vc-mix-legend__item">
        <span className="vc-dot vc-dot--bug" />
        bug
      </span>
      <span className="vc-mix-legend__item">
        <span className="vc-dot vc-dot--task" />
        task
      </span>
    </div>
  )
}

function MixBar({ mix }) {
  if (!mix) return null
  const { story = 0, bug = 0, task = 0 } = mix
  const total = story + bug + task
  if (total === 0) return null
  return (
    <div
      className="vc-mix"
      title={`${story}% story · ${bug}% bug · ${task}% task`}
    >
      <div className="vc-mix__seg vc-mix__seg--story" style={{ flexBasis: `${story}%` }} />
      <div className="vc-mix__seg vc-mix__seg--bug"   style={{ flexBasis: `${bug}%` }} />
      <div className="vc-mix__seg vc-mix__seg--task"  style={{ flexBasis: `${task}%` }} />
    </div>
  )
}

function Sparkline({ values, trend }) {
  if (!values || values.length === 0) return null
  const W = 88, H = 24
  const max = Math.max(...values, 0.0001)
  const stepX = values.length > 1 ? W / (values.length - 1) : W
  const pts = values.map((v, i) => {
    const x = i * stepX
    const y = H - 2 - (v / max) * (H - 4)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  const fillPts =
    `0,${H} ${pts} ${(W).toFixed(1)},${H}`
  const cls = trend === 'up' ? 'vc-spark vc-spark--up'
    : trend === 'down' ? 'vc-spark vc-spark--down'
    : 'vc-spark'
  return (
    <svg className={cls} viewBox={`0 0 ${W} ${H}`}>
      <polygon className="vc-spark__fill" points={fillPts} />
      <polyline className="vc-spark__line" points={pts} />
    </svg>
  )
}

function SpeedView({ data, sort, type, search }) {
  const [selectedTeam, setSelectedTeam] = useState(null)
  const org = data.org || {}
  let teams = (data.teams || []).slice()
  if (search?.trim()) {
    const q = search.trim().toLowerCase()
    teams = teams.filter(t =>
      (t.name || '').toLowerCase().includes(q) ||
      (t.team_id || '').toLowerCase().includes(q)
    )
  }
  const get = (t) => t.by_type?.[type] || {}
  if (sort === 'fastest_median') {
    teams.sort((a, b) =>
      (get(a).median_hours ?? Infinity) - (get(b).median_hours ?? Infinity))
  } else if (sort === 'fastest_p90') {
    teams.sort((a, b) =>
      (get(a).p90_hours ?? Infinity) - (get(b).p90_hours ?? Infinity))
  } else if (sort === 'cheapest') {
    teams.sort((a, b) =>
      (get(a).dollar_per_pr ?? Infinity) - (get(b).dollar_per_pr ?? Infinity))
  }

  const selectedTeamObj = selectedTeam
    ? (data.teams || []).find(t => t.team_id === selectedTeam)
    : null

  return (
    <>
      <div className="vc-summary">
        <SummaryCard
          label="org median"
          value={fmtDays(org.median_hours)}
        />
        <SummaryCard
          label="org P90"
          value={fmtDays(org.p90_hours)}
        />
        <SummaryCard
          label="issues w/ PR"
          value={`${org.with_pr_pct || 0}%`}
        />
        <SummaryCard
          label="tracked"
          value={(org.total_issues || 0).toLocaleString()}
        />
      </div>

      {selectedTeam ? (
        <>
          <div className="vc-breadcrumb">
            <button
              className="vc-breadcrumb__back"
              onClick={() => setSelectedTeam(null)}
            >
              ← All teams
            </button>
            <span className="vc-breadcrumb__sep"> / </span>
            <span className="vc-breadcrumb__current">
              {selectedTeamObj?.name || selectedTeam}
            </span>
          </div>
          <UserDrillDown
            tab="speed" team_id={selectedTeam}
            window_={data.window} type={type}
          />
        </>
      ) : (
        <Card className="vc-table-card">
          <table className="vc-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Team</th>
                <th className="num">Median</th>
                <th className="num">P90</th>
                <th className="num">$ / PR</th>
                <th>Cycle trend</th>
              </tr>
            </thead>
            <tbody>
              {teams.length === 0 && (
                <tr>
                  <td colSpan="6" style={{
                    padding: '24px', textAlign: 'center',
                    fontSize: '13px', color: 'var(--ink-4)',
                  }}>
                    No cycle-time data yet.{' '}
                    Wait for the next sync (~30 min)
                    or trigger one from{' '}
                    <a href="#/integrations" className="vc-link">
                      Integrations
                    </a>.
                  </td>
                </tr>
              )}
              {teams.map((t, i) => {
                const b = get(t)
                return (
                  <tr key={t.team_id}>
                    <td className="vc-rank">{i + 1}</td>
                    <td>
                      <button
                        className="vc-team-link"
                        onClick={() => setSelectedTeam(t.team_id)}
                        title={`View users in ${t.name}`}
                      >
                        <div className="vc-team-name">{t.name}</div>
                      </button>
                      <div className="vc-team-sub">
                        {t.devs} devs · {t.repos?.length || 0} repos
                      </div>
                    </td>
                    <td className="num">{fmtDays(b.median_hours)}</td>
                    <td className="num">{fmtDays(b.p90_hours)}</td>
                    <td className="num">{fmtMoney(b.dollar_per_pr)}</td>
                    <td>
                      <BandSpark byType={t.by_type} activeType={type} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </Card>
      )}
    </>
  )
}

/* Stacked-band sparkline showing cycle-time per class.
   Story (indigo) on baseline, Task (neutral) above, Bug (red) on top.
   When `activeType` is one of the three classes the other bands
   dim to 16% opacity; the active band stays at 96%. When it's
   'all', all bands sit at 60% so the relative volume reads.
   Mirrors the time.html ~lines 491–540 mockup. */
function BandSpark({ byType, activeType }) {
  if (!byType) return null
  const W = 88, H = 24
  const story = byType.story?.sparkline || []
  const bug = byType.bug?.sparkline || []
  const task = byType.task?.sparkline || []
  const n = Math.max(story.length, bug.length, task.length)
  if (n === 0) return null
  // Stack heights (ascending Story→Task→Bug to keep colors balanced).
  const stacks = []
  let max = 0.0001
  for (let i = 0; i < n; i++) {
    const s = story[i] || 0
    const t = task[i] || 0
    const b = bug[i] || 0
    stacks.push({ s, t, b, total: s + t + b })
    if (s + t + b > max) max = s + t + b
  }
  const stepX = n > 1 ? W / (n - 1) : W
  const yScale = (v) => H - 2 - (v / max) * (H - 4)

  function bandPolygon(layer) {
    // layer = 'story' | 'task' | 'bug'
    const pts = []
    const back = []
    stacks.forEach((s, i) => {
      let bot, top
      if (layer === 'story') {
        bot = 0
        top = s.s
      } else if (layer === 'task') {
        bot = s.s
        top = s.s + s.t
      } else {
        bot = s.s + s.t
        top = s.total
      }
      const x = i * stepX
      pts.push(`${x.toFixed(1)},${yScale(top).toFixed(1)}`)
      back.unshift(`${x.toFixed(1)},${yScale(bot).toFixed(1)}`)
    })
    return [...pts, ...back].join(' ')
  }

  const opacity = (cls) => {
    if (activeType === 'all') return 0.6
    return cls === activeType ? 0.96 : 0.16
  }
  return (
    <svg className="vc-bandspark" viewBox={`0 0 ${W} ${H}`}>
      <polygon
        points={bandPolygon('story')}
        fill="var(--type-story, #4f46e5)"
        opacity={opacity('story')}
      />
      <polygon
        points={bandPolygon('task')}
        fill="var(--type-task, #525252)"
        opacity={opacity('task')}
      />
      <polygon
        points={bandPolygon('bug')}
        fill="var(--type-bug, #dc2626)"
        opacity={opacity('bug')}
      />
    </svg>
  )
}

function SummaryCard({ label, value }) {
  return (
    <Card className="vc-summary__card">
      <div className="vc-summary__label">{label}</div>
      <div className="vc-summary__value">{value}</div>
    </Card>
  )
}

// #561: the period-vs-period "vs prev period" TrendChip column was
// dropped from both speed tables (team + user drill-down) — it was
// confusing with no comparison data shown to reconcile it. The
// TrendChip / DEFAULT_TREND_TITLE helpers were its only consumers,
// so they were removed too. The in-window "Cycle trend" sparkline
// (BandSpark) stays.

function fmtDays(hours) {
  if (hours == null) return '—'
  return `${(hours / 24).toFixed(1)}d`
}


// ── Jira-aware view (#365) ─────────────────────────────
function JiraView({ window_, selectedTeam }) {
  const [sprints, setSprints] = useState({ linked: false, sprints: [] })
  const [epics, setEpics] = useState({ linked: false, epics: [] })
  const [series, setSeries] = useState(null)
  const [sprintId, setSprintId] = useState('')
  const [epicKey, setEpicKey] = useState('')
  const [fixVersion, setFixVersion] = useState('')
  const [pr_class, setPrClass] = useState('all')
  const [err, setErr] = useState(null)
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState(null)

  useEffect(() => {
    setLoading(true); setErr(null)
    Promise.all([
      api.vcJiraSprints(window_),
      api.vcJiraEpics(window_),
    ]).then(([s, e]) => {
      setSprints(s); setEpics(e)
    }).catch(e => setErr(String(e)))
  }, [window_])

  useEffect(() => {
    setLoading(true); setErr(null)
    api.vcJiraSeries({
      window: window_, team: selectedTeam,
      sprint_id: sprintId || undefined,
      epic_key: epicKey || undefined,
      fix_version: fixVersion || undefined,
      pr_class,
    })
      .then(setSeries)
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false))
  }, [window_, selectedTeam, sprintId, epicKey, fixVersion, pr_class])

  useEffect(() => {
    if (!sprintId) { setDetail(null); return }
    api.vcJiraSprint(sprintId)
      .then(setDetail)
      .catch(() => setDetail(null))
  }, [sprintId])

  if (err) {
    return (
      <Card className="vc-empty">
        <div className="vc-empty__title">Error</div>
        <div className="vc-empty__sub">{err}</div>
      </Card>
    )
  }

  if (!sprints.linked) {
    return (
      <Card className="vc-empty">
        <div className="vc-empty__title">No Jira site linked</div>
        <div className="vc-empty__sub">
          Connect a Jira site in{' '}
          <a href="#/integrations" className="vc-link">
            Integrations
          </a>
          {' '}to surface Sprint, Epic, and $/SP views.
        </div>
      </Card>
    )
  }

  const totals = series?.totals
  const weeks = series?.weeks || []
  const haveSp = totals && totals.cost_per_story_point != null

  // Fix-version dropdown options come from the live series
  // — every distinct fix_version we've seen in the rollup.
  const fixVersionOptions = []
  if (weeks.length > 0) {
    const seen = new Set()
    weeks.forEach(w => {
      // (rollup denormalises fix_version onto each row;
      // we don't have it on the series response, so for
      // v1 the dropdown shows nothing until #347b adds
      // the per-row fix_version. The control still
      // exists so the layout matches the mockup.)
    })
  }

  return (
    <>
      <Card>
        <div className="vc-jira-controls">
          <label>
            Sprint
            <select
              value={sprintId}
              onChange={e => setSprintId(e.target.value)}
            >
              <option value="">All sprints</option>
              {sprints.sprints.map(s => (
                <option key={s.id} value={s.id}>
                  {s.name}{s.active ? ' · active' : ''}
                </option>
              ))}
            </select>
          </label>
          <label>
            Epic
            <select
              value={epicKey}
              onChange={e => setEpicKey(e.target.value)}
            >
              <option value="">All epics</option>
              {epics.epics.map(e => (
                <option key={e.key} value={e.key}>
                  {e.key} — {e.summary?.slice(0, 40)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Fix version
            <input
              type="text"
              placeholder="e.g. v1.2.0"
              value={fixVersion}
              onChange={e => setFixVersion(e.target.value)}
            />
          </label>
          <label>
            Class
            <select
              value={pr_class}
              onChange={e => setPrClass(e.target.value)}
            >
              <option value="all">All</option>
              <option value="story">Story</option>
              <option value="bug">Bug</option>
              <option value="task">Task</option>
            </select>
          </label>
        </div>
      </Card>

      <div className="vc-tiles">
        <Tile label="PRs merged"   value={totals?.prs_merged ?? '—'} />
        <Tile label="Spend"        value={fmtMoney(totals?.spend_usd)} />
        <Tile label="Story points" value={totals?.story_points ?? '—'} />
        <Tile
          label="Cost / story point"
          value={haveSp ? fmtMoney(totals.cost_per_story_point) : '—'}
          sub={haveSp ? null : 'No SP data in window'}
        />
      </div>

      {sprintId && detail && (
        <Card>
          <header className="vc-card__head">
            <h2>{detail.sprint_name}</h2>
          </header>
          <div className="vc-tiles">
            <Tile
              label="Stories shipped"
              value={`${detail.shipped_stories}/${detail.committed_stories}`}
              sub={`${detail.carry_over_stories} carry-over`}
            />
            <Tile
              label="SP shipped"
              value={`${detail.story_points_shipped}/${detail.story_points_committed}`}
            />
            <Tile
              label="$/SP"
              value={detail.cost_per_story_point != null
                ? fmtMoney(detail.cost_per_story_point) : '—'}
            />
            <Tile label="Spend" value={fmtMoney(detail.spend_usd)} />
          </div>
          <table className="vc-table">
            <thead>
              <tr>
                <th>Epic</th>
                <th>Shipped</th>
                <th>Committed</th>
                <th>SP shipped</th>
              </tr>
            </thead>
            <tbody>
              {detail.by_epic.map(e => (
                <tr key={e.epic_key}>
                  <td><code>{e.epic_key}</code></td>
                  <td>{e.shipped}</td>
                  <td>{e.committed}</td>
                  <td>{e.story_points_shipped}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      <Card>
        <header className="vc-card__head">
          <h2>Weekly</h2>
        </header>
        {weeks.length === 0 && (
          <p className="vc-muted">
            No Jira-linked PRs in the selected window yet.
            Reference a Jira key (e.g. <code>PROJ-123</code>) in
            a PR title or branch and the next sync will populate
            this table.
          </p>
        )}
        {weeks.length > 0 && (
          <table className="vc-table">
            <thead>
              <tr>
                <th>Week</th>
                <th>PRs</th>
                <th>Spend</th>
                <th>SP</th>
                <th>$/SP</th>
              </tr>
            </thead>
            <tbody>
              {weeks.map(w => (
                <tr key={w.week_start}>
                  <td>{new Date(w.week_start).toLocaleDateString()}</td>
                  <td>{w.prs_merged}</td>
                  <td>{fmtMoney(w.spend_usd)}</td>
                  <td>{w.story_points ?? '—'}</td>
                  <td>{w.cost_per_story_point != null
                    ? fmtMoney(w.cost_per_story_point)
                    : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}

function Tile({ label, value, sub }) {
  return (
    <div className="vc-tile">
      <div className="vc-tile__label">{label}</div>
      <div className="vc-tile__value">{value}</div>
      {sub && <div className="vc-tile__sub">{sub}</div>}
    </div>
  )
}

export function JiraBadge({ issueKey, issueType, summary, status, source }) {
  const colorByType = {
    Story: 'var(--type-story, #4f46e5)',
    Bug: 'var(--type-bug, #dc2626)',
    Task: 'var(--type-task, #525252)',
  }
  const dot = colorByType[issueType] || 'var(--ink-5)'
  const tip = [
    issueType ? `Type: ${issueType}` : null,
    status ? `Status: ${status}` : null,
    summary ? `Summary: ${summary}` : null,
    source ? `Linked via PR ${source}` : null,
  ].filter(Boolean).join('\n')
  return (
    <span className="vc-jira-badge" title={tip}>
      <span className="vc-jira-badge__dot" style={{ background: dot }} />
      <code>{issueKey}</code>
    </span>
  )
}
