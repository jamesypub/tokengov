import React, { useEffect, useMemo, useState } from 'react'
import { AlertCircle } from 'lucide-react'
import { getSummary, getUsage, getTeams, fmtUsd, fmtTokens } from '../api'
import { useTeamScope } from '../TeamScope'
import { Card } from '../ui/Card'
import { SkeletonBlock } from '../ui/Skeleton'
import SpendAsOf from '../components/SpendAsOf'
import {
  flexRender, getCoreRowModel, getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table'

// The summary cards (Budget bar / Hero KPI / stat tiles) that used to
// sit above this table moved to the Users page — Activity is
// now the usage table only. The Status + Cap columns also moved to the
// Users page; Activity is a pure spend/usage view (token counts +
// total spend).

// ── Usage table (TanStack) ──────────────────────────────────────────────────

function UsageTable({ rows, search, modelFilter, pathFilter, teamMap }) {
  const data = useMemo(() => rows.filter(r => {
    if (search && !r.email.toLowerCase().includes(search.toLowerCase())) return false
    if (modelFilter && r.model !== modelFilter) return false
    // #436: managed/unmanaged principal filter
    if (pathFilter === 'managed' && !r.managed) return false
    if (pathFilter === 'unmanaged' && r.managed) return false
    return true
  }), [rows, search, modelFilter, pathFilter])

  const [sorting, setSorting] = useState([])

  const columns = useMemo(() => [
    {
      accessorKey: 'email',
      header: 'User',
      cell: info => (
        <a
          href={`#/users/${encodeURIComponent(info.getValue())}`}
          className="text-[var(--accent)] font-semibold hover:underline"
        >
          {info.getValue()}
        </a>
      ),
    },
    {
      accessorKey: 'team_id',
      header: 'Team',
      cell: info => {
        const v = info.getValue()
        return (teamMap[v] || v) || <span className="text-[var(--ink-4)] italic">—</span>
      },
    },
    {
      accessorKey: 'model',
      header: 'Model',
      cell: info => (
        <span className="font-mono text-[12px] text-[var(--ink-3)]">{info.getValue()}</span>
      ),
    },
    {
      accessorKey: 'spend_usd',
      header: () => <div className="text-right">Spend</div>,
      cell: info => {
        const v = info.getValue() || 0
        const row = info.row.original
        // Spend-estimate warn mode: when the org runs the spend
        // estimate in Warn and this user's projected (billed + estimated)
        // crosses their cap while billed alone is under, flag it — a
        // warning only, no block. Per-user signal shown on the user's
        // rows.
        const warn = row.estimate_enforcement === 'warn'
          && row.projected_over_cap
        return (
          <div
            className="text-right font-mono font-bold tabular-nums"
            style={{ color: v > 0 ? 'var(--ink)' : 'var(--ink-4)' }}
          >
            {fmtUsd(v)}
            {warn && (
              <span
                className="ml-1 font-sans font-semibold text-amber-700"
                title={
                  'Projected (billed + estimated unbilled) is over this '
                  + "user's cap, though billed alone is still under. "
                  + 'Warning only — no block.'
                }
              >
                ⚠
              </span>
            )}
          </div>
        )
      },
    },
    // Activity is a spend/usage view: token counts + total spend.
    // Cap + Status belong to the Users page, not here. Token headers
    // name the unit; cache read/write match the CUR per-dimension split
    // the worker mirrors into CurUserSpend.
    { accessorKey: 'input_tokens', header: () => <div className="text-right">Input tokens</div>,
      cell: info => <div className="text-right font-mono tabular-nums">{fmtTokens(info.getValue())}</div> },
    { accessorKey: 'output_tokens', header: () => <div className="text-right">Output tokens</div>,
      cell: info => <div className="text-right font-mono tabular-nums">{fmtTokens(info.getValue())}</div> },
    { accessorKey: 'cache_read_tokens', header: () => <div className="text-right">Cache read tokens</div>,
      cell: info => <div className="text-right font-mono tabular-nums">{fmtTokens(info.getValue())}</div> },
    { accessorKey: 'cache_write_tokens', header: () => <div className="text-right">Cache write tokens</div>,
      cell: info => <div className="text-right font-mono tabular-nums">{fmtTokens(info.getValue())}</div> },
    // teamMap is read by the Team cell — it MUST be a dep, or the memo
    // captures the initial empty map and the Team column shows raw ids
    // forever (the stale-closure bug). With teamMap in deps the column
    // re-renders to the team NAME once getTeams() resolves.
  ], [teamMap])

  const table = useReactTable({
    data, columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })

  if (data.length === 0) {
    return <div className="p-8 text-center text-[var(--ink-4)]">No usage data matches these filters.</div>
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          {table.getHeaderGroups().map(hg => (
            <tr key={hg.id} className="border-b-2 border-[var(--border)]">
              {hg.headers.map(h => {
                const sort = h.column.getIsSorted()
                return (
                  <th
                    key={h.id}
                    onClick={h.column.getToggleSortingHandler()}
                    className="p-2 text-left text-[11px] uppercase tracking-wider font-bold text-[var(--ink-3)] whitespace-nowrap cursor-pointer select-none"
                  >
                    <span className="inline-flex items-center gap-1">
                      {flexRender(h.column.columnDef.header, h.getContext())}
                      <span className="text-[10px] opacity-60">
                        {sort === 'asc' ? '▲' : sort === 'desc' ? '▼' : ''}
                      </span>
                    </span>
                  </th>
                )
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map(row => (
            <tr key={row.id} className="border-b border-[var(--border)] hover:bg-[var(--surface-2)]">
              {row.getVisibleCells().map(c => (
                <td key={c.id} className="p-2 align-middle">
                  {flexRender(c.column.columnDef.cell, c.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── Main page ───────────────────────────────────────────────────────────────

export default function Activity() {
  const [summary, setSummary] = useState(null)
  const [usage, setUsage]     = useState(null)
  const [teamMap, setTeamMap] = useState({})
  const [error, setError]     = useState(null)
  const [search, setSearch]   = useState('')
  const [modelFilter, setModelFilter] = useState('')
  const [pathFilter, setPathFilter]   = useState('')
  const { selectedTeam } = useTeamScope()

  useEffect(() => {
    setSummary(null); setUsage(null); setError(null)
    // #895: only the member's OWN data (summary + usage) is fatal —
    // those are the 200 endpoints a member is entitled to. getTeams
    // (/api/teams) is org-admin / team-admin only and 403s a plain
    // member ("Insufficient permissions"); it was inside this
    // Promise.all, so its 403 rejected the whole chain and blanked the
    // page, discarding the member's own activity. teamMap is purely
    // decorative (maps team_id → display name in the table), so a 403
    // there must degrade to an empty map, never blank the page. Mirror
    // the #703/#868 persona-gating pattern: handle the admin-only
    // secondary call non-fatally. (Same applies to the analytics-query
    // feature, which lives on Cost Reports, not here.)
    Promise.all([
      getSummary(selectedTeam),
      getUsage(selectedTeam),
    ]).then(([summaryData, usageData]) => {
      setSummary(summaryData)
      // Stamp the org-wide estimate-enforcement mode onto each row so
      // the Spend cell can gate the warn marker (single org-level value).
      const estEnf = usageData?.estimate_enforcement || 'off'
      setUsage(usageData ? {
        ...usageData,
        rows: (usageData.rows || []).map(
          r => ({ ...r, estimate_enforcement: estEnf })),
      } : usageData)
    }).catch(e => setError(e.message))
    // Team-name labels: best-effort, non-fatal. A member 403 → no
    // labels (the table falls back to the raw id / no team column),
    // but the page still renders their own activity.
    getTeams()
      .then(teamsData => {
        const m = {}
        for (const t of (teamsData.teams || [])) m[t.team_id] = t.name
        setTeamMap(m)
      })
      .catch(() => setTeamMap({}))
  }, [selectedTeam])

  if (error) return (
    <div className="p-8 flex flex-col items-center gap-2">
      <AlertCircle size={24} className="text-[var(--red)]" />
      <p className="font-bold text-[var(--red)] m-0">Failed to load activity</p>
      <p className="text-sm text-[var(--ink-3)] m-0">{error}</p>
    </div>
  )

  const rows = usage?.rows || []
  const month = summary?.month || usage?.month || '—'

  const totals = rows.reduce((acc, r) => {
    acc.spend          += r.spend_usd || 0
    acc.input_tokens   += r.input_tokens || 0
    acc.output_tokens  += r.output_tokens || 0
    acc.cache_read     += r.cache_read_tokens || 0
    acc.cache_write    += r.cache_write_tokens || 0
    return acc
  }, { spend: 0, input_tokens: 0, output_tokens: 0, cache_read: 0, cache_write: 0 })

  // #436: 'synthetic' is a placeholder model_id written by the
  // vc_seed_synthetic demo seed job, not a real Bedrock model —
  // drop it from the filter so it never shows as a choice.
  const models = [...new Set(rows.map(r => r.model))]
    .filter(m => m && m !== 'synthetic')
    .sort()

  // The summary cards (active users / quota / cache) moved to the Users
  // page — Activity is the usage table only now. Keep `month`
  // for the table header.

  return (
    <div className="p-8 flex flex-col gap-5">
      <div className="border-b border-[var(--border)] pb-3">
        <h1 className="m-0 text-2xl font-semibold">Activity</h1>
        <p className="m-0 mt-1 text-sm text-[var(--ink-4)]">
          Token usage · spend · quota · {month}
        </p>
        <SpendAsOf className="mt-1" />
      </div>

      <Card className="p-5">
        <div className="flex justify-between items-center mb-4 flex-wrap gap-3">
          <div className="text-xs uppercase tracking-wider font-bold text-[var(--ink-3)]">
            Usage by user · model — {month}
          </div>
          <div className="flex gap-2 flex-wrap items-center">
            <input
              placeholder="Search user…"
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="px-3 py-1.5 rounded border border-[var(--border)] text-sm min-w-[180px]"
            />
            <select
              value={modelFilter}
              onChange={e => setModelFilter(e.target.value)}
              className="px-3 py-1.5 rounded border border-[var(--border)] text-sm bg-white"
            >
              <option value="">All models</option>
              {models.map(m => <option key={m} value={m}>{m}</option>)}
            </select>
            {/* #436/#856: filter by Bedrock access path. This is the
                #345 chokepoint-role heuristic (does the principal reach
                Bedrock via the tg-consumer role?) — a DIFFERENT concept
                from the governed flag, so the labels deliberately do
                NOT say Governed/Ungoverned. Option values stay
                managed/unmanaged (the r.managed field is unchanged);
                only the display copy is relabelled. */}
            <select
              value={pathFilter}
              onChange={e => setPathFilter(e.target.value)}
              className="px-3 py-1.5 rounded border border-[var(--border)] text-sm bg-white"
            >
              <option value="">All principals</option>
              <option value="managed">Via tg-consumer role</option>
              <option value="unmanaged">Other path</option>
            </select>
            <span className="text-sm text-[var(--ink-4)] whitespace-nowrap">
              {rows.filter(r =>
                (!search || r.email.toLowerCase().includes(search.toLowerCase())) &&
                (!modelFilter || r.model === modelFilter) &&
                (pathFilter !== 'managed' || r.managed) &&
                (pathFilter !== 'unmanaged' || !r.managed)
              ).length} rows
            </span>
          </div>
        </div>

        {!usage ? (
          <div className="space-y-2">
            <SkeletonBlock className="h-8 rounded" />
            <SkeletonBlock className="h-8 rounded" />
            <SkeletonBlock className="h-8 rounded" />
          </div>
        ) : (
          <UsageTable rows={rows} search={search} modelFilter={modelFilter} pathFilter={pathFilter} teamMap={teamMap} />
        )}
      </Card>
    </div>
  )
}
