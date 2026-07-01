import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { Card } from '../ui/Card'
import { Button } from '../ui/Button'
import { Github, RefreshCw, AlertCircle, CheckCircle2, Trash2, Briefcase } from 'lucide-react'
import { getTeams } from '../api'
import { normalizeRepo, GITHUB_HOST } from '../lib/repoUrl'

const VENDORS = [
  { key: 'github', label: 'GitHub', Icon: Github, active: true },
  { key: 'jira',   label: 'Jira',   Icon: Briefcase, active: true },
  // Future: { key: 'linear', label: 'Linear', Icon: Activity },
]

const SUB_TABS_GH = [
  { key: 'token', label: 'Token' },
  { key: 'repos', label: 'Repos' },
  { key: 'rules', label: 'Labeling rules' },
]

const SUB_TABS_JIRA = [
  { key: 'sites', label: 'Sites' },
]

export default function Integrations() {
  const [vendor, setVendor] = useState('github')
  const [sub, setSub] = useState('token')
  const subTabs = vendor === 'jira' ? SUB_TABS_JIRA : SUB_TABS_GH

  // When switching vendor, snap sub-tab to the first valid one
  useEffect(() => {
    if (!subTabs.some(t => t.key === sub)) {
      setSub(subTabs[0].key)
    }
  }, [vendor])

  return (
    <div className="vc-page">
      <header className="vc-page__head">
        <div>
          <h1 className="vc-page__title">Integrations</h1>
          <p className="vc-page__sub">
            Connect upstream sources of truth so the leaderboard
            knows which work merged and who shipped it.
          </p>
        </div>
      </header>

      <div className="vc-strip">
        {VENDORS.map(v => (
          <a
            key={v.key}
            href="#/integrations"
            className={
              'vc-strip__tab ' +
              (vendor === v.key ? 'is-active' : '') +
              (v.active ? '' : ' is-disabled')
            }
            onClick={e => {
              e.preventDefault()
              if (v.active) setVendor(v.key)
            }}
          >
            <span className="vc-strip__icon">
              <v.Icon size={16} />
            </span>
            <span>
              <span className="vc-strip__label">{v.label}</span>
              <span className="vc-strip__sub">
                {v.active ? 'Connected source' : 'Coming soon'}
              </span>
            </span>
          </a>
        ))}
      </div>

      <div className="vc-controls">
        <div className="vc-seg">
          {subTabs.map(t => (
            <button
              key={t.key}
              className={
                'vc-seg__btn ' + (sub === t.key ? 'is-active' : '')
              }
              onClick={() => setSub(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {vendor === 'github' && sub === 'token' && <GithubToken />}
      {vendor === 'github' && sub === 'repos' && <GithubRepos />}
      {vendor === 'github' && sub === 'rules' && <LabelingRules />}
      {vendor === 'jira'   && sub === 'sites' && <JiraSites />}
    </div>
  )
}

function JiraSites() {
  const [sites, setSites] = useState(null)
  const [err, setErr] = useState(null)
  const [showAdd, setShowAdd] = useState(false)
  const [busy, setBusy] = useState(false)
  const [form, setForm] = useState({
    site_url: '', auth_email: '', token: '', projects: '',
  })
  const [okMsg, setOkMsg] = useState(null)

  function load() {
    setErr(null)
    api.jiraListSites()
      .then(setSites)
      .catch(e => setErr(String(e)))
  }
  useEffect(load, [])

  async function add() {
    setBusy(true); setErr(null); setOkMsg(null)
    try {
      const projects = form.projects
        .split(',').map(s => s.trim()).filter(Boolean)
      await api.jiraAddSite({
        site_url:   form.site_url.trim(),
        auth_email: form.auth_email.trim(),
        token:      form.token.trim(),
        projects,
      })
      setForm({ site_url: '', auth_email: '', token: '', projects: '' })
      setShowAdd(false)
      setOkMsg('Site added.')
      load()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function test(id) {
    setBusy(true); setErr(null); setOkMsg(null)
    try {
      const r = await api.jiraTestSite(id)
      setOkMsg(`Connected as ${r.display_name || r.account_id || 'unknown'}`)
      load()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function syncNow(id) {
    setBusy(true); setErr(null); setOkMsg(null)
    try {
      const r = await api.jiraSyncSite(id)
      setOkMsg(r.detail || 'Sync triggered.')
      load()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove(id) {
    if (!confirm('Disconnect this Jira site? Issues already mirrored will remain.')) return
    setBusy(true); setErr(null); setOkMsg(null)
    try {
      await api.jiraDeleteSite(id)
      setOkMsg('Site removed.')
      load()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      {err && (
        <Card>
          <div className="vc-alert vc-alert--err">
            <AlertCircle size={16} /> {err}
          </div>
        </Card>
      )}
      {okMsg && (
        <Card>
          <div className="vc-alert vc-alert--ok">
            <CheckCircle2 size={16} /> {okMsg}
          </div>
        </Card>
      )}

      <Card>
        <header className="vc-card__head">
          <h2>Jira sites</h2>
          <Button
            variant="primary"
            size="sm"
            onClick={() => setShowAdd(s => !s)}
          >
            {showAdd ? 'Cancel' : 'Add site'}
          </Button>
        </header>

        {showAdd && (
          <div className="vc-form">
            <label>
              Site URL
              <input
                type="url"
                value={form.site_url}
                placeholder="https://acme.atlassian.net"
                onChange={e => setForm(f => ({ ...f, site_url: e.target.value }))}
              />
            </label>
            <label>
              Auth email
              <input
                type="email"
                value={form.auth_email}
                placeholder="ci@acme.com"
                onChange={e => setForm(f => ({ ...f, auth_email: e.target.value }))}
              />
            </label>
            <label>
              API token
              <input
                type="password"
                value={form.token}
                placeholder="ATATT…"
                onChange={e => setForm(f => ({ ...f, token: e.target.value }))}
              />
            </label>
            <label>
              Project keys (comma-separated)
              <input
                type="text"
                value={form.projects}
                placeholder="PROJ, DATA, WEB"
                onChange={e => setForm(f => ({ ...f, projects: e.target.value }))}
              />
            </label>
            <Button
              variant="primary"
              size="sm"
              onClick={add}
              disabled={busy}
            >
              {busy ? 'Saving…' : 'Save & test'}
            </Button>
          </div>
        )}

        {!sites && !err && <p className="vc-muted">Loading…</p>}
        {sites && sites.length === 0 && (
          <p className="vc-muted">
            No Jira sites configured yet. Add one above.
          </p>
        )}
        {sites && sites.length > 0 && (
          <table className="vc-table">
            <thead>
              <tr>
                <th>Host</th>
                <th>Email</th>
                <th>Projects</th>
                <th>Status</th>
                <th>Issues</th>
                <th>Last sync</th>
                <th>Token</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {sites.map(s => (
                <tr key={s.id}>
                  <td><code>{s.host}</code></td>
                  <td>{s.auth_email}</td>
                  <td>{(s.projects || []).join(', ') || '—'}</td>
                  <td>
                    <span className={`vc-pill vc-pill--${s.sync_status === 'ok' ? 'ok' : (s.sync_status === 'paused' ? 'warn' : 'err')}`}>
                      {s.sync_status}
                    </span>
                  </td>
                  <td>{s.issue_count}</td>
                  <td>
                    {s.last_sync_at
                      ? new Date(s.last_sync_at).toLocaleString()
                      : '—'}
                  </td>
                  <td>{s.token_storage}</td>
                  <td>
                    <Button size="sm" variant="secondary"
                      onClick={() => test(s.id)} disabled={busy}>
                      <RefreshCw size={12} /> Test
                    </Button>
                    {' '}
                    <Button size="sm" variant="secondary"
                      onClick={() => syncNow(s.id)} disabled={busy}>
                      Sync now
                    </Button>
                    {' '}
                    <Button size="sm" variant="ghost"
                      onClick={() => remove(s.id)} disabled={busy}>
                      <Trash2 size={12} />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card>
        <p className="vc-muted">
          Jira links activate once a configured site has at least
          one merged PR whose title, body, branch, or first commit
          mentions a project key (e.g. <code>PROJ-123</code>).
          Full Velocity & Cost surfaces (sprint/epic filters,
          $/story-point) ship in <code>#347b</code>.
        </p>
      </Card>
    </>
  )
}

function GithubToken() {
  const [status, setStatus] = useState(null)
  const [err, setErr] = useState(null)
  const [token, setToken] = useState('')
  const [saving, setSaving] = useState(false)
  const [okMsg, setOkMsg] = useState(null)

  function load() {
    setErr(null)
    api.ghTokenStatus()
      .then(setStatus)
      .catch(e => setErr(String(e)))
  }
  useEffect(load, [])

  async function save() {
    if (!token) { setErr('Paste a token first'); return }
    setSaving(true); setErr(null); setOkMsg(null)
    try {
      await api.ghPutToken(token)
      setToken('')
      setOkMsg('Token saved and probed successfully.')
      load()
    } catch (e) {
      setErr(String(e))
    } finally {
      setSaving(false)
    }
  }

  async function clear() {
    if (!confirm('Remove the stored GitHub token?')) return
    setSaving(true); setErr(null); setOkMsg(null)
    try {
      await api.ghDeleteToken()
      setOkMsg('Token cleared.')
      load()
    } catch (e) {
      setErr(String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card className="vc-card">
      <div className="vc-card__head">
        <h2 className="vc-card__title">Default GitHub PAT</h2>
        <p className="vc-card__sub">
          Used for API calls to fetch PR + issue metadata.
          Token is probed against <code>/user</code> on save and
          stored in AWS Secrets Manager when available.
        </p>
      </div>

      <div className="vc-card__body">
        {status && (
          <div className="vc-status">
            {status.connected ? (
              <span className="vc-status__on">
                <CheckCircle2 size={14} /> Connected
                {status.last4 && (
                  <span className="vc-status__scopes">
                    ending …{status.last4}
                  </span>
                )}
                {status.connected_at && (
                  <span className="vc-status__scopes">
                    since {new Date(status.connected_at)
                      .toLocaleDateString()}
                  </span>
                )}
                {status.is_seed_placeholder && (
                  <span className="vc-status__scopes" style={{ color: 'var(--amber)' }}>
                    seed placeholder — rotate before prod
                  </span>
                )}
                {' · '}
                <span className="vc-status__scopes">
                  {status.repo_count || 0} repos tracked
                </span>
              </span>
            ) : (
              <span className="vc-status__off">
                <AlertCircle size={14} /> No org token configured —
                public repos still sync anonymously (60 req/hr shared);
                private repos show “needs token” until you add one
                here or per repo.
              </span>
            )}
          </div>
        )}

        <label className="vc-label">
          New token (ghp_…)
        </label>
        <input
          type="password"
          value={token}
          onChange={e => setToken(e.target.value)}
          placeholder="ghp_xxxxxxxxxxxxxxxxxxxxxxxx"
          className="vc-input"
          autoComplete="off"
        />

        {err && <div className="vc-err">{err}</div>}
        {okMsg && <div className="vc-ok">{okMsg}</div>}

        <div className="vc-actions">
          <Button
            variant="primary"
            size="sm"
            onClick={save}
            disabled={saving || !token}
          >
            {saving ? 'Saving…' : 'Save & probe'}
          </Button>
          {status?.connected && (
            <Button
              variant="secondary"
              size="sm"
              onClick={clear}
              disabled={saving}
            >
              Rotate / Remove
            </Button>
          )}
        </div>
      </div>
    </Card>
  )
}

// #1043: map token_kind → a human Auth badge (text + intent, never
// color alone). sync_status=paused with kind=missing reads "needs
// token"; auth_failed/rate_limited surface their own state.
const AUTH_LABELS = {
  public:   { text: 'public',     hint: 'synced anonymously' },
  org:      { text: 'org default', hint: 'uses the org PAT' },
  override: { text: 'repo token',  hint: 'per-repo PAT' },
  missing:  { text: 'needs token', hint: 'private — no token resolves' },
  unprobed: { text: 'unprobed',    hint: 'classified on next sync' },
}
function authBadge(r) {
  if (r.sync_status === 'auth_failed') return { text: 'auth failed', hint: 'token rejected' }
  if (r.sync_status === 'rate_limited') return { text: 'rate limited', hint: 'GitHub 60 req/hr' }
  return AUTH_LABELS[r.token_kind] || { text: r.token_kind || '—', hint: '' }
}

function GithubRepos() {
  const [data, setData] = useState(null)
  const [teams, setTeams] = useState([])
  const [err, setErr] = useState(null)
  const [syncing, setSyncing] = useState(null)
  const [showAdd, setShowAdd] = useState(false)
  // #1051: resolve a team_id (FK UUID) to its name from the already-
  // loaded teams list, matching the edit dropdown. Falls back to the
  // UUID for a stale/deleted team_id so the cell is never blank.
  const teamName = (id) =>
    teams.find(t => t.team_id === id)?.name || id
  const [addRepo, setAddRepo] = useState('')
  const [addTeam, setAddTeam] = useState('')
  const [addErr, setAddErr] = useState(null)
  const [adding, setAdding] = useState(false)
  const [editingTeam, setEditingTeam] = useState(null)
  // #1043: per-repo token drawer state.
  const [tokenRepo, setTokenRepo] = useState(null)   // repo key or null
  const [tokenMode, setTokenMode] = useState('auto')
  const [tokenVal, setTokenVal] = useState('')
  const [tokenSaving, setTokenSaving] = useState(false)
  const [tokenErr, setTokenErr] = useState(null)

  function openTokenDrawer(r) {
    setTokenRepo(r.repo)
    setTokenMode(r.token_mode || 'auto')
    setTokenVal('')
    setTokenErr(null)
  }

  async function saveToken() {
    setTokenSaving(true); setTokenErr(null)
    try {
      const patch = { token_mode: tokenMode }
      if (tokenMode === 'override' && tokenVal.trim()) {
        patch.token = tokenVal.trim()
      }
      await api.ghUpdateRepo(tokenRepo, patch)
      setTokenRepo(null); setTokenVal('')
      load()
    } catch (e) {
      setTokenErr(String(e))
    } finally {
      setTokenSaving(false)
    }
  }

  function load() {
    setErr(null)
    api.ghListRepos()
      .then(setData)
      .catch(e => setErr(String(e)))
  }
  useEffect(() => {
    load()
    getTeams().then(ts => setTeams(Array.isArray(ts) ? ts : (ts.teams || [])))
              .catch(() => {})
  }, [])

  async function sync(repo) {
    setSyncing(repo || '*'); setErr(null)
    try {
      await api.ghSync(repo)
      load()
    } catch (e) {
      setErr(String(e))
    } finally {
      setSyncing(null)
    }
  }

  // #1042: URL-first — normalize on every keystroke for the live
  // preview, and submit the canonical identity (backend re-normalizes
  // as the authority). The button stays enabled; a bad value shows a
  // specific inline error on submit (no silent-disabled).
  const parsed = normalizeRepo(addRepo)

  async function handleAdd(e) {
    e.preventDefault()
    if (!parsed.ok) {
      setAddErr(parsed.error)
      return
    }
    setAddErr(null); setAdding(true)
    try {
      await api.ghAddRepo(parsed.canonical, addTeam || null)
      setAddRepo(''); setAddTeam(''); setShowAdd(false)
      load()
    } catch (e) {
      setAddErr(String(e))
    } finally {
      setAdding(false)
    }
  }

  async function handleTeamChange(repo, teamId) {
    setEditingTeam(null)
    try {
      await api.ghUpdateRepo(repo, { team_id: teamId || null })
      load()
    } catch (e) {
      setErr(String(e))
    }
  }

  async function handleDelete(repo) {
    if (!window.confirm(`Untrack ${repo}? History is kept.`)) return
    try {
      await api.ghDeleteRepo(repo)
      load()
    } catch (e) {
      setErr(String(e))
    }
  }

  if (err) return (
    <Card className="vc-empty">
      <div className="vc-empty__title">Error</div>
      <div className="vc-empty__sub">{err}</div>
    </Card>
  )
  if (!data) return (
    <Card className="vc-empty">
      <div className="vc-empty__title">Loading…</div>
    </Card>
  )

  const repos = Array.isArray(data) ? data : (data.repos || [])
  return (
    <Card className="vc-table-card">
      <div className="vc-card__head vc-card__head--row">
        <div>
          <h2 className="vc-card__title">Tracked repositories</h2>
          <p className="vc-card__sub">
            Each row's Sync triggers an immediate fetch.
          </p>
        </div>
        <div className="vc-actions">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setShowAdd(v => !v)}
          >
            + Add repo
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => sync(null)}
            disabled={syncing === '*'}
          >
            <RefreshCw size={12} />
            {syncing === '*' ? ' Syncing…' : ' Sync all'}
          </Button>
        </div>
      </div>

      {showAdd && (
        <form
          onSubmit={handleAdd}
          style={{ padding: '8px 0' }}
        >
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              className="vc-input"
              placeholder="https://github.com/NVIDIA/SkillSpector"
              value={addRepo}
              onChange={e => { setAddRepo(e.target.value); setAddErr(null) }}
              aria-describedby="add-repo-hint add-repo-err"
              aria-invalid={addRepo.trim() !== '' && !parsed.ok}
              style={{
                flex: 1,
                borderColor: addRepo.trim() === ''
                  ? undefined
                  : (parsed.ok
                      ? 'var(--color-success, green)'
                      : 'var(--color-error, red)'),
              }}
            />
            <select
              className="vc-input"
              value={addTeam}
              onChange={e => setAddTeam(e.target.value)}
            >
              <option value="">No team</option>
              {teams.map(t => (
                <option key={t.team_id} value={t.team_id}>
                  {t.name || t.team_id}
                </option>
              ))}
            </select>
            <Button
              variant="primary"
              size="sm"
              type="submit"
              disabled={adding}
            >
              {adding ? 'Adding…' : 'Add'}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              type="button"
              onClick={() => { setShowAdd(false); setAddErr(null) }}
            >
              Cancel
            </Button>
          </div>
          {/* Live host/path preview + inline error. aria-live so the
              parsed identity is announced as the admin types. */}
          <div
            id="add-repo-hint"
            aria-live="polite"
            style={{ fontSize: 12, marginTop: 6, lineHeight: 1.5 }}
          >
            {addRepo.trim() === '' && (
              <span className="vc-empty__sub">
                Accepts a full URL (GitHub, self-hosted GitLab) or{' '}
                <code>owner/name</code>.
              </span>
            )}
            {addRepo.trim() !== '' && parsed.ok && (
              <span>
                Will track{' '}
                <code>{parsed.host}/{parsed.path}</code>
                {!parsed.isGithub && (
                  <span style={{ color: 'var(--color-warning, #b8860b)' }}>
                    {' '}— saved for future sync; automated sync
                    currently runs for {GITHUB_HOST} only.
                  </span>
                )}
              </span>
            )}
            {addRepo.trim() !== '' && !parsed.ok && (
              <span style={{ color: 'var(--color-error, red)' }}>
                {parsed.error}
              </span>
            )}
          </div>
          {addErr && (
            <div
              id="add-repo-err"
              style={{ color: 'var(--color-error, red)', fontSize: 12, marginTop: 4 }}
            >
              {addErr}
            </div>
          )}
        </form>
      )}

      {repos.length === 0 ? (
        <div className="vc-empty__sub" style={{ padding: 24 }}>
          No repos tracked yet. Use + Add repo or seed via{' '}
          <code>scripts/tg-test-data-populate.sh</code>.
        </div>
      ) : (
        <table className="vc-table">
          <thead>
            <tr>
              <th>Repo</th>
              <th>Team</th>
              <th>Auth</th>
              <th>Status</th>
              <th className="num">PRs (30d)</th>
              <th className="num">Last sync</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {repos.map(r => (
              <tr key={r.repo}>
                <td><code>{r.repo}</code></td>
                <td>
                  {editingTeam === r.repo ? (
                    <select
                      className="vc-input"
                      defaultValue={r.team_id || ''}
                      autoFocus
                      onBlur={e => handleTeamChange(r.repo, e.target.value)}
                      onChange={e => handleTeamChange(r.repo, e.target.value)}
                    >
                      <option value="">No team</option>
                      {teams.map(t => (
                        <option key={t.team_id} value={t.team_id}>
                          {t.name || t.team_id}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <span
                      style={{ cursor: 'pointer' }}
                      onClick={() => setEditingTeam(r.repo)}
                      title="Click to edit team"
                    >
                      {r.team_id ? teamName(r.team_id) : '—'}
                    </span>
                  )}
                </td>
                <td>
                  {r.is_github === false ? (
                    <span className="vc-badge" title="sync not wired yet for non-github hosts">
                      n/a
                    </span>
                  ) : (
                    <span
                      className={'vc-badge vc-badge--' + (r.token_kind || 'unknown')}
                      title={authBadge(r).hint}
                    >
                      {authBadge(r).text}
                    </span>
                  )}
                </td>
                <td>
                  <span className={'vc-badge vc-badge--' + (r.sync_status || 'unknown')}>
                    {r.sync_status || 'unknown'}
                  </span>
                </td>
                <td className="num">{r.pr_count_30d || r.prs_30d || 0}</td>
                <td className="num">
                  {r.last_sync_at
                    ? new Date(r.last_sync_at).toLocaleString()
                    : '—'}
                </td>
                <td style={{ display: 'flex', gap: 4 }}>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => sync(r.repo)}
                    disabled={syncing === r.repo}
                  >
                    <RefreshCw size={12} />
                    {syncing === r.repo ? ' …' : ' Sync'}
                  </Button>
                  {/* #1043: Token affordance — github rows only (a
                      non-github host can't be fetched, so prompting
                      for a token is misleading). */}
                  {r.is_github !== false && (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => openTokenDrawer(r)}
                      title={`Token tier for ${r.repo}`}
                    >
                      Token
                    </Button>
                  )}
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handleDelete(r.repo)}
                    title={`Untrack ${r.repo}`}
                  >
                    <Trash2 size={12} />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* #1043: per-repo token drawer (real labelled dialog). */}
      {tokenRepo && (
        <div
          role="dialog"
          aria-label={`Token tier for ${tokenRepo}`}
          aria-modal="true"
          style={{
            marginTop: 12, padding: 16,
            border: '1px solid var(--color-border, #ccc)',
            borderRadius: 8,
          }}
        >
          <h3 style={{ marginTop: 0, fontSize: 14 }}>
            Token for <code>{tokenRepo}</code>
          </h3>
          <p className="vc-empty__sub" style={{ fontSize: 12 }}>
            Set the org PAT once (Token tab) — it covers every private
            repo it can see. Only override when a repo needs its own.
          </p>
          <fieldset style={{ border: 'none', padding: 0, margin: 0 }}>
            <label style={{ display: 'block', marginBottom: 6 }}>
              <input
                type="radio" name="tokenmode"
                checked={tokenMode === 'auto'}
                onChange={() => setTokenMode('auto')}
              />{' '}
              Auto (recommended) — probe; public syncs anonymously,
              private uses the org default.
            </label>
            <label style={{ display: 'block', marginBottom: 6 }}>
              <input
                type="radio" name="tokenmode"
                checked={tokenMode === 'override'}
                onChange={() => setTokenMode('override')}
              />{' '}
              Use a token just for this repo
            </label>
            {tokenMode === 'override' && (
              <input
                className="vc-input"
                type="password"
                autoComplete="off"
                placeholder="ghp_… (stored encrypted; shown …last4)"
                value={tokenVal}
                onChange={e => setTokenVal(e.target.value)}
                aria-label="Per-repo personal access token"
                style={{ display: 'block', margin: '4px 0 8px', width: '100%' }}
              />
            )}
            <label style={{ display: 'block', marginBottom: 6 }}>
              <input
                type="radio" name="tokenmode"
                checked={tokenMode === 'public'}
                onChange={() => setTokenMode('public')}
              />{' '}
              Force anonymous (public repo, never use a token)
            </label>
          </fieldset>
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <Button
              variant="primary" size="sm"
              onClick={saveToken} disabled={tokenSaving}
            >
              {tokenSaving ? 'Saving…' : 'Save'}
            </Button>
            <Button
              variant="secondary" size="sm" type="button"
              onClick={() => { setTokenRepo(null); setTokenErr(null) }}
            >
              Cancel
            </Button>
            {tokenErr && (
              <span style={{ color: 'var(--color-error, red)', fontSize: 12, alignSelf: 'center' }}>
                {tokenErr}
              </span>
            )}
          </div>
        </div>
      )}
    </Card>
  )
}

function _emptyMap() {
  return { story: [], bug: [], task: [] }
}

function _normalize(m) {
  if (!m) return _emptyMap()
  return {
    story: Array.isArray(m.story) ? [...m.story] : [],
    bug:   Array.isArray(m.bug)   ? [...m.bug]   : [],
    task:  Array.isArray(m.task)  ? [...m.task]  : [],
  }
}

function _diffCount(a, b) {
  let n = 0
  for (const cls of ['story', 'bug', 'task']) {
    const sa = new Set(a[cls] || [])
    const sb = new Set(b[cls] || [])
    for (const x of sa) if (!sb.has(x)) n++
    for (const x of sb) if (!sa.has(x)) n++
  }
  return n
}

function LabelingRules() {
  const [saved, setSaved] = useState(null)   // server-known
  const [draft, setDraft] = useState(null)   // local edits
  const [coverage, setCoverage] = useState(null)
  const [preview, setPreview] = useState(null)
  const [err, setErr] = useState(null)
  const [saving, setSaving] = useState(false)
  const [adding, setAdding] = useState({ story: '', bug: '', task: '' })

  useEffect(() => {
    api.ghGetLabelMap().then(m => {
      const norm = _normalize(m.label_map || m)
      setSaved(norm); setDraft(norm)
    }).catch(e => setErr(String(e)))
    api.ghCoverage().then(setCoverage).catch(() => {})
  }, [])

  // Debounced live preview against current draft.
  useEffect(() => {
    if (!draft) return
    const h = setTimeout(() => {
      api.ghPreviewClassification({ label_map: draft })
        .then(r => setPreview(r.traces || []))
        .catch(() => {})
    }, 350)
    return () => clearTimeout(h)
  }, [draft])

  if (err) return (
    <Card className="vc-empty">
      <div className="vc-empty__title">Error</div>
      <div className="vc-empty__sub">{err}</div>
    </Card>
  )
  if (!draft || !saved) return (
    <Card className="vc-empty">
      <div className="vc-empty__title">Loading…</div>
    </Card>
  )

  const dirty = _diffCount(saved, draft)

  function addLabel(cls) {
    const v = (adding[cls] || '').trim()
    if (!v) return
    if ((draft[cls] || []).includes(v)) {
      setAdding(a => ({ ...a, [cls]: '' }))
      return
    }
    setDraft({ ...draft, [cls]: [...(draft[cls] || []), v] })
    setAdding(a => ({ ...a, [cls]: '' }))
  }
  function removeLabel(cls, lbl) {
    setDraft({
      ...draft,
      [cls]: (draft[cls] || []).filter(l => l !== lbl),
    })
  }
  function discard() {
    setDraft(_normalize(saved))
  }
  async function save(reclassify) {
    setSaving(true); setErr(null)
    try {
      await api.ghPutLabelMap({
        label_map: draft,
        reclassify: !!reclassify,
      })
      setSaved(_normalize(draft))
      // refresh coverage if reclassify ran
      if (reclassify) {
        api.ghCoverage().then(setCoverage).catch(() => {})
      }
    } catch (e) {
      setErr(String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <Card className="vc-card">
        <div className="vc-card__head">
          <h2 className="vc-card__title">Label → class mapping</h2>
          <p className="vc-card__sub">
            How PRs without a linked issue are bucketed. Edits
            preview live against the 25 most-recent merged PRs;
            <strong> Save &amp; reclassify </strong> rewrites
            historical verdicts to match.
          </p>
        </div>
        <div className="vc-card__body">
          {['story', 'bug', 'task'].map(cls => (
            <div key={cls} className="vc-rule-row">
              <span className={'vc-dot vc-dot--' + cls} />
              <span className="vc-rule-class">{cls}</span>
              <div className="vc-rule-labels">
                {(draft[cls] || []).map(lbl => (
                  <span key={lbl} className="vc-rule-label">
                    {lbl}
                    <button
                      className="vc-rule-x"
                      onClick={() => removeLabel(cls, lbl)}
                      title="Remove"
                    >×</button>
                  </span>
                ))}
                {(draft[cls] || []).length === 0 && (
                  <span className="vc-rule-empty">no labels mapped</span>
                )}
                <input
                  className="vc-rule-add"
                  placeholder="add label…"
                  value={adding[cls]}
                  onChange={e => setAdding(a => ({
                    ...a, [cls]: e.target.value,
                  }))}
                  onKeyDown={e => {
                    if (e.key === 'Enter') addLabel(cls)
                  }}
                />
              </div>
            </div>
          ))}
        </div>
      </Card>

      <Card className="vc-card">
        <div className="vc-card__head">
          <h2 className="vc-card__title">Live preview</h2>
          <p className="vc-card__sub">
            Verdicts produced by the current (unsaved) rules
            against the 25 most-recent merged PRs.
          </p>
        </div>
        <div className="vc-card__body">
          {!preview && (
            <div className="vc-rule-empty">Loading preview…</div>
          )}
          {preview && preview.length === 0 && (
            <div className="vc-rule-empty">
              No PRs in <code>github_activity</code> yet — sync a repo
              from the Repos tab first.
            </div>
          )}
          {preview && preview.length > 0 && (
            <table className="vc-table">
              <thead>
                <tr>
                  <th>PR</th>
                  <th>Title</th>
                  <th>Class</th>
                  <th>Probe</th>
                  <th>Labels</th>
                </tr>
              </thead>
              <tbody>
                {preview.slice(0, 12).map(t => (
                  <tr key={t.repo + '#' + t.pr_number}>
                    <td><code>{t.repo}#{t.pr_number}</code></td>
                    <td className="vc-trunc">{t.title || '—'}</td>
                    <td>
                      <span className={'vc-dot vc-dot--' + t.pr_class} />
                      {' '}{t.pr_class}
                    </td>
                    <td>
                      <span className={
                        'vc-badge vc-badge--' +
                        (t.classified_by === 'fallback'
                          ? 'pending' : 'ok')
                      }>{t.classified_by}</span>
                    </td>
                    <td className="vc-trunc">
                      {(t.labels || []).join(', ') || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Card>

      {coverage && (
        <Card className="vc-card">
          <div className="vc-card__head">
            <h2 className="vc-card__title">Classification coverage</h2>
            <p className="vc-card__sub">
              How PRs were classified over the last 30 days.
            </p>
          </div>
          <div className="vc-card__body vc-coverage">
            <CoverageStat
              label="total PRs (30d)"
              value={coverage.total_30d || 0}
            />
            <CoverageStat
              label="classified"
              value={coverage.classified_30d || 0}
            />
            <CoverageStat
              label="coverage %"
              value={(coverage.coverage_pct || 0) + '%'}
            />
            {(coverage.probe_attribution || []).map(p => (
              <CoverageStat
                key={p.probe}
                label={p.probe}
                value={`${p.count} (${p.pct}%)`}
              />
            ))}
          </div>
        </Card>
      )}

      {dirty > 0 && (
        <div className="vc-savebar">
          <span className="vc-savebar__hint">
            {dirty} unsaved {dirty === 1 ? 'edit' : 'edits'}
          </span>
          <div className="vc-savebar__actions">
            <Button
              variant="secondary"
              size="sm"
              onClick={discard}
              disabled={saving}
            >Discard</Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => save(false)}
              disabled={saving}
            >Save only</Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => save(true)}
              disabled={saving}
            >
              {saving ? 'Saving…' : 'Save & reclassify'}
            </Button>
          </div>
        </div>
      )}
    </>
  )
}

function CoverageStat({ label, value }) {
  return (
    <div className="vc-cov-stat">
      <div className="vc-cov-stat__v">{value}</div>
      <div className="vc-cov-stat__k">{label}</div>
    </div>
  )
}
