import React, { useEffect, useState } from 'react'
import { Plus, Pencil, Trash2, Users, Loader2, X, ShieldCheck, ShieldOff } from 'lucide-react'
import {
  api, getTeams, createTeam, updateTeam, deleteTeam,
  getTeamMembers, addTeamMember, removeTeamMember, formatTokens,
} from '../api'
import { useTeamScope } from '../TeamScope'
import { Card } from '../ui/Card'
import { Button } from '../ui/Button'
import { Input } from '../ui/Input'
import { Badge } from '../ui/Badge'
import SpendAsOf from '../components/SpendAsOf'

// ── Modal wrapper ──────────────────────────────────────────────────────────

function Modal({ onClose, children, wide }) {
  return (
    <div className="fixed inset-0 bg-black/45 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className={
          'relative bg-white rounded-xl shadow-xl p-6 max-h-[90vh] overflow-y-auto w-full ' +
          (wide ? 'max-w-xl' : 'max-w-md')
        }
        onClick={e => e.stopPropagation()}
      >
        <button
          onClick={onClose}
          aria-label="Close"
          className="absolute top-3 right-3 opacity-60 hover:opacity-100 transition-opacity"
        >
          <X size={16} />
        </button>
        {children}
      </div>
    </div>
  )
}

function ErrorBox({ children }) {
  if (!children) return null
  return (
    <div className="bg-red-50 border border-red-300 text-red-800 px-3 py-2 rounded text-sm my-2">
      {children}
    </div>
  )
}

// ── Team create/edit modal ─────────────────────────────────────────────────

function TeamModal({ team, allTeams = [], onClose, onSaved }) {
  const isEdit = !!team
  const [form, setForm] = useState({
    name:           team?.name || '',
    description:    team?.description || '',
    parent_team_id: team?.parent_team_id || '',
    budget_usd:     team?.budget_usd ?? '',
    enabled:        team?.enabled ?? true,
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const disallowedParents = (() => {
    if (!isEdit) return new Set()
    const byParent = new Map()
    for (const t of allTeams) {
      const p = t.parent_team_id || ''
      if (!byParent.has(p)) byParent.set(p, [])
      byParent.get(p).push(t.team_id)
    }
    const out = new Set([team.team_id])
    const stack = [team.team_id]
    while (stack.length) {
      for (const child of (byParent.get(stack.pop()) || [])) {
        if (!out.has(child)) { out.add(child); stack.push(child) }
      }
    }
    return out
  })()

  const parentOptions = allTeams
    .filter(t => !disallowedParents.has(t.team_id))
    .sort((a, b) => a.name.localeCompare(b.name))

  async function handleSave() {
    if (!form.name.trim()) { setError('Team name is required'); return }
    setSaving(true); setError('')
    try {
      const budgetRaw = String(form.budget_usd).trim()
      const budgetVal = budgetRaw === '' ? null : Number(budgetRaw)
      if (budgetVal !== null &&
          (!Number.isFinite(budgetVal) || budgetVal < 0)) {
        setError('Budget must be a non-negative number')
        setSaving(false)
        return
      }
      const payload = {
        name:           form.name.trim(),
        description:    form.description.trim(),
        parent_team_id: form.parent_team_id || null,
        budget_usd:     budgetVal,
        enabled:        form.enabled,
      }
      if (isEdit) await updateTeam(team.team_id, payload)
      else        await createTeam(payload)
      onSaved()
    } catch (e) { setError(e.message) }
    finally { setSaving(false) }
  }

  return (
    <Modal onClose={onClose}>
      <h2 className="m-0 mb-4 text-lg font-bold">
        {isEdit ? 'Edit Team' : 'New Team'}
      </h2>

      <Field label="Team Name">
        <Input
          placeholder="e.g. Data Science"
          value={form.name}
          onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
        />
      </Field>

      <Field label="Description">
        <Input
          placeholder="Optional description"
          value={form.description}
          onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
        />
      </Field>

      <Field
        label="Parent team"
        helper="A team admin of a parent automatically sees aggregate data across child teams."
      >
        <select
          className="h-9 px-3 rounded border border-[var(--border-2)] text-sm bg-white"
          value={form.parent_team_id}
          onChange={e => setForm(f => ({ ...f, parent_team_id: e.target.value }))}
        >
          <option value="">(no parent — top-level team)</option>
          {parentOptions.map(t => (
            <option key={t.team_id} value={t.team_id}>{t.name}</option>
          ))}
        </select>
      </Field>

      <Field
        label="Budget (USD / month)"
        helper="Reference cap for visibility. Per-user caps do the actual blocking."
      >
        <Input
          type="number"
          min="0"
          step="0.01"
          placeholder="leave blank for unlimited"
          value={form.budget_usd}
          onChange={e => setForm(f => ({ ...f, budget_usd: e.target.value }))}
        />
      </Field>

      <label className="flex items-center gap-2 text-sm my-3">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={e => setForm(f => ({ ...f, enabled: e.target.checked }))}
        />
        Team enabled
      </label>

      <ErrorBox>{error}</ErrorBox>

      <div className="flex justify-end gap-2 border-t border-[var(--border)] pt-3 mt-2">
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button variant="primary" onClick={handleSave} disabled={saving}>
          {saving ? 'Saving…' : (isEdit ? 'Update Team' : 'Create Team')}
        </Button>
      </div>
    </Modal>
  )
}

function Field({ label, children, helper }) {
  return (
    <div className="flex flex-col gap-1 my-3">
      <span className="text-[11px] font-bold uppercase tracking-wider text-[var(--ink-3)]">{label}</span>
      {children}
      {helper && (
        <span className="text-[11px] text-[var(--ink-4)] mt-0.5">{helper}</span>
      )}
    </div>
  )
}

// ── Members modal ──────────────────────────────────────────────────────────

function MembersModal({ team, onClose }) {
  const [members, setMembers] = useState([])
  const [loading, setLoading] = useState(true)
  const [newEmail, setNewEmail] = useState('')
  const [adding, setAdding] = useState(false)
  const [roleBusy, setRoleBusy] = useState(null)
  const [error, setError] = useState('')
  const { persona, selectedTeam: _st } = useTeamScope()

  // Callers who can manage roles: org_admin, team_admin
  const canManageRoles = ['org_admin', 'team_admin'].includes(persona)
  const viewerEmail = null // server enforces self-grant; we just hide the button

  const load = () => {
    setLoading(true)
    getTeamMembers(team.team_id)
      .then(r => { setMembers(r.members); setLoading(false) })
      .catch(e => { setError(e.message); setLoading(false) })
  }
  useEffect(() => { load() }, [team.team_id])

  async function handleAdd() {
    const email = newEmail.trim()
    if (!email) return
    setAdding(true); setError('')
    try {
      await addTeamMember(team.team_id, email)
      setNewEmail('')
      load()
    } catch (e) { setError(e.message) }
    finally { setAdding(false) }
  }

  async function handleRemove(email) {
    try { await removeTeamMember(team.team_id, email); load() }
    catch (e) { setError(e.message) }
  }

  async function handlePromote(email) {
    setRoleBusy(email); setError('')
    try {
      await api.grantRole({ email, role: 'team_admin', team_id: team.team_id })
      load()
    } catch (e) { setError(e.message) }
    finally { setRoleBusy(null) }
  }

  async function handleDemote(email) {
    setRoleBusy(email); setError('')
    try {
      await api.revokeRole(email, team.team_id, 'team_admin')
      load()
    } catch (e) { setError(e.message) }
    finally { setRoleBusy(null) }
  }

  return (
    <Modal onClose={onClose} wide>
      <div className="flex items-center justify-between mb-4">
        <h2 className="m-0 text-lg font-bold">Members — {team.name}</h2>
        <span className="text-sm text-[var(--ink-4)]">{members.length} total</span>
      </div>

      <div className="flex gap-2 mb-3 items-center">
        <Input
          placeholder="user@company.com"
          value={newEmail}
          onChange={e => setNewEmail(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleAdd()}
          className="flex-1"
        />
        <Button variant="primary" onClick={handleAdd} disabled={adding || !newEmail.trim()}>
          {adding ? 'Adding…' : 'Add'}
        </Button>
      </div>

      <ErrorBox>{error}</ErrorBox>

      <div className="max-h-[360px] overflow-y-auto border border-[var(--border)] rounded">
        {loading && (
          <div className="flex justify-center p-6">
            <Loader2 size={20} className="text-[var(--ink-4)] animate-spin" />
          </div>
        )}
        {!loading && members.length === 0 && (
          <div className="p-6 text-center text-[var(--ink-4)] text-sm">
            No members yet.
          </div>
        )}
        {!loading && members.map((m, i) => {
          const isAdmin = m.role === 'team_admin'
          const busy = roleBusy === m.email
          return (
            <div
              key={m.email}
              className={
                'flex items-center gap-3 px-3 py-2 border-b border-[var(--border)] ' +
                (i % 2 === 1 ? 'bg-[var(--surface-2)]' : 'bg-white')
              }
            >
              <span className="flex-1 text-sm font-semibold truncate">{m.email}</span>
              <Badge variant={isAdmin ? 'success' : 'default'}>{m.role || 'member'}</Badge>
              {canManageRoles && (
                isAdmin ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handleDemote(m.email)}
                    disabled={busy}
                  >
                    {busy ? <Loader2 size={12} className="animate-spin mr-1" /> : <ShieldOff size={12} className="mr-1" />}
                    Demote
                  </Button>
                ) : (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handlePromote(m.email)}
                    disabled={busy}
                  >
                    {busy ? <Loader2 size={12} className="animate-spin mr-1" /> : <ShieldCheck size={12} className="mr-1" />}
                    Make admin
                  </Button>
                )
              )}
              <Button
                variant="ghost"
                size="icon"
                onClick={() => handleRemove(m.email)}
                aria-label={`Remove ${m.email}`}
                className="text-[var(--red)]"
              >
                <Trash2 size={14} />
              </Button>
            </div>
          )
        })}
      </div>

      <div className="flex justify-end border-t border-[var(--border)] pt-3 mt-3">
        <Button variant="secondary" onClick={onClose}>Close</Button>
      </div>
    </Modal>
  )
}

// ── Team tree builder ──────────────────────────────────────────────────────

function buildTeamTree(teams) {
  const byParent = new Map()
  for (const t of teams) {
    const p = t.parent_team_id || ''
    if (!byParent.has(p)) byParent.set(p, [])
    byParent.get(p).push(t)
  }
  for (const list of byParent.values()) list.sort((a, b) => a.name.localeCompare(b.name))
  const out = []
  function walk(parentId, depth) {
    for (const t of (byParent.get(parentId) || [])) {
      out.push({ ...t, depth })
      walk(t.team_id, depth + 1)
    }
  }
  walk('', 0)
  const seen = new Set(out.map(t => t.team_id))
  for (const t of teams) if (!seen.has(t.team_id)) out.push({ ...t, depth: 0 })
  return out
}

// ── TeamRow ───────────────────────────────────────────────────────────────

function TeamRow({ team, depth, rowIndex, onEdit, onDelete, onMembers, canEdit }) {
  return (
    <div
      className={
        'flex items-center gap-4 px-4 py-3 border-b border-[var(--border)] ' +
        (rowIndex % 2 === 1 ? 'bg-[var(--surface-2)]' : 'bg-white')
      }
    >
      <div className="flex-1 flex items-center gap-2 min-w-0" style={{ paddingLeft: depth * 20 }}>
        {depth > 0 && <span className="text-[var(--ink-4)] text-xs select-none">└</span>}
        <Users size={14} className="text-[var(--ink-4)] flex-shrink-0" />
        <div className="min-w-0">
          <div className="text-sm font-bold truncate">{team.name}</div>
          {team.description && (
            <div className="text-xs text-[var(--ink-3)] truncate">{team.description}</div>
          )}
        </div>
      </div>
      <div className="w-16 text-right text-sm flex-shrink-0">{team.member_count}</div>
      <div className="w-32 text-right text-sm text-[var(--ink-3)] flex-shrink-0 font-mono">
        {team.budget_usd != null
          ? `$${(team.spend_usd || 0).toFixed(2)} / $${Number(team.budget_usd).toLocaleString()}`
          : `$${(team.spend_usd || 0).toFixed(2)}`}
      </div>
      <div className="w-40 flex justify-end items-center gap-1 flex-shrink-0">
        <Button variant="secondary" size="sm" onClick={() => onMembers(team)}>Members</Button>
        {canEdit && (
          <Button variant="ghost" size="icon" onClick={() => onEdit(team)} aria-label="Edit">
            <Pencil size={14} />
          </Button>
        )}
        {canEdit && !team.protected && (
          <Button
            variant="ghost"
            size="icon"
            onClick={() => onDelete(team)}
            aria-label="Delete"
            className="text-[var(--red)]"
          >
            <Trash2 size={14} />
          </Button>
        )}
      </div>
    </div>
  )
}

// ── Main Teams page ────────────────────────────────────────────────────────

export default function Teams() {
  const [teams, setTeams] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)
  const [editingTeam, setEditingTeam] = useState(null)
  const [addingTeam, setAddingTeam] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(null)
  const [membersTeam, setMembersTeam] = useState(null)
  const { persona, selectedTeam } = useTeamScope()
  const isOrgAdmin = persona === 'org_admin'

  const load = () => {
    setLoading(true)
    getTeams()
      .then(r => { setTeams(r.teams); setLoading(false) })
      .catch(e => { setLoadError(e.message); setLoading(false) })
  }
  useEffect(() => { load() }, [selectedTeam])

  const handleSaved = () => {
    setEditingTeam(null)
    setAddingTeam(false)
    load()
  }

  const handleDeleteConfirmed = async () => {
    if (!confirmDelete) return
    try {
      await deleteTeam(confirmDelete.team_id)
      setConfirmDelete(null)
      load()
    } catch (e) {
      alert(`Delete failed: ${e.message}`)
    }
  }

  const tree = buildTeamTree(teams)

  return (
    <div className="p-8">
      <header className="flex items-center justify-between border-b border-[var(--border)] pb-3 mb-5">
        <h1 className="m-0 text-2xl font-semibold">Teams</h1>
        <div className="flex items-center gap-4">
          <span className="text-sm text-[var(--ink-4)]">{teams.length} teams</span>
          {isOrgAdmin && (
            <Button variant="primary" onClick={() => setAddingTeam(true)}>
              <Plus size={14} /> New Team
            </Button>
          )}
        </div>
      </header>

      <SpendAsOf className="-mt-3 mb-4" />

      {loading && (
        <div className="text-[var(--ink-4)] p-8 text-center">Loading teams…</div>
      )}

      {loadError && (
        <ErrorBox>{loadError}</ErrorBox>
      )}

      {!loading && !loadError && (
        <Card className="overflow-hidden">
          <div className="flex items-center gap-4 px-4 py-3 bg-[var(--surface)] border-b-2 border-[var(--border)]">
            <span className="flex-1 text-[11px] font-bold uppercase tracking-wider text-[var(--ink-3)]">Team</span>
            <span className="w-16 text-right text-[11px] font-bold uppercase tracking-wider text-[var(--ink-3)]">Members</span>
            <span className="w-32 text-right text-[11px] font-bold uppercase tracking-wider text-[var(--ink-3)]">Spend / Budget</span>
            <span className="w-40 text-right text-[11px] font-bold uppercase tracking-wider text-[var(--ink-3)]">Actions</span>
          </div>
          {tree.map((t, i) => (
            <TeamRow
              key={t.team_id}
              team={t}
              depth={t.depth}
              rowIndex={i}
              onEdit={setEditingTeam}
              onDelete={setConfirmDelete}
              onMembers={setMembersTeam}
              canEdit={isOrgAdmin}
            />
          ))}
          {teams.length === 0 && (
            <div className="p-8 text-center text-[var(--ink-4)] text-sm">
              No teams yet. Click "New Team" to create one.
            </div>
          )}
        </Card>
      )}

      {(editingTeam || addingTeam) && (
        <TeamModal
          team={editingTeam || null}
          allTeams={teams}
          onClose={() => { setEditingTeam(null); setAddingTeam(false) }}
          onSaved={handleSaved}
        />
      )}

      {membersTeam && (
        <MembersModal team={membersTeam} onClose={() => setMembersTeam(null)} />
      )}

      {confirmDelete && (
        <Modal onClose={() => setConfirmDelete(null)}>
          <h3 className="m-0 mb-3 text-base font-bold">
            Delete "{confirmDelete.name}"?
          </h3>
          <p className="text-sm text-[var(--ink-3)] mb-4">
            This removes the team and cannot be undone. Users are not deleted.
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setConfirmDelete(null)}>Cancel</Button>
            <Button variant="destructive" onClick={handleDeleteConfirmed}>Delete</Button>
          </div>
        </Modal>
      )}
    </div>
  )
}
