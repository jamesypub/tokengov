import React, { useEffect, useMemo, useState } from 'react'
import { getAnalyticsQueries, runAnalyticsQuery } from '../api'
import { Button } from '../ui/Button'
import { RefreshCw } from 'lucide-react'

const QUERY_DISPLAY_NAMES = {
  'tg-bedrock-daily-trend':                'Daily Spend Trend',
  'tg-bedrock-monthly-history':            'Monthly History',
  'tg-bedrock-spend-by-model':             'Spend by Model',
  'tg-bedrock-spend-by-user':              'Spend by User',
  'tg-bedrock-tokens-spend-by-user-model': 'Spend by user model',
  'tg-bedrock-top-spenders':               'Top Spenders',
}

function displayName(query) {
  return QUERY_DISPLAY_NAMES[query.name] || query.name
}

// Part 2: the date-range control. Pure helpers (exported for unit
// tests). A range is "active" only when BOTH ends are set; a partial
// range is invalid (the API rejects start-without-end). end < start is
// invalid. Both blank = default month-to-date.
const _ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/

export function rangeState(start, end) {
  const s = (start || '').trim()
  const e = (end || '').trim()
  if (!s && !e) return { active: false, valid: true, mtd: true }
  if (!s || !e) return { active: false, valid: false, mtd: false }
  if (!_ISO_DATE_RE.test(s) || !_ISO_DATE_RE.test(e)) {
    return { active: false, valid: false, mtd: false }
  }
  if (e < s) return { active: false, valid: false, mtd: false }
  return { active: true, valid: true, mtd: false }
}

export function rangeLabel(start, end) {
  const st = rangeState(start, end)
  if (!st.valid) return 'Invalid range'
  return `${start} – ${end}`
}

// Default window = month-start .. today, as explicit ISO dates so the
// inputs show the REAL window being queried (no hidden "blank = MTD"
// state). Equivalent to the API's empty-default month-to-date. `now`
// is injectable for tests.
export function monthStartISO(now = new Date()) {
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  return `${y}-${m}-01`
}
export function todayISO(now = new Date()) {
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  const d = String(now.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

// A column is a token COUNT (raw integer, thousands-separated, 0 dp) vs
// a money column (USD, 2 dp, <$0.01 for sub-cent non-zero). Driven off
// the column NAME so the formatter is data-shape-agnostic. Token cols
// from the saved queries end in "tokens"; money cols carry "$" / spend
// / cost / usd.
function _isTokenCol(name) {
  return /tokens?$/i.test((name || '').trim())
}
function _isMoneyCol(name) {
  return /\$|spend|cost|usd/i.test(name || '')
}

// Format ONE result cell for display. Never emit exponent notation
// (Athena returns tiny doubles as "4.0E-4"); token counts get thousands
// separators + 0 decimals; money gets 2 decimals with a "<$0.01" floor
// for sub-cent non-zero spend (a bare "$0.00" would hide real spend).
// Non-numeric / unparseable values pass through verbatim.
export function formatCell(value, colName) {
  const s = (value ?? '').toString().trim()
  if (s === '') return ''
  // Parse as a number — Number() handles E-notation ("4.0E-4"), which
  // isNumericCell deliberately doesn't match (so Athena's tiny doubles
  // reach a numeric column here and still get formatted, never printed
  // raw as "4.0E-4"). Strip $/,/% first.
  const cleaned = s.replace(/[$,%\s]/g, '')
  const n = Number(cleaned)
  // Non-numeric (a model id, an email) → verbatim.
  if (cleaned === '' || !Number.isFinite(n)) return s
  if (_isTokenCol(colName)) {
    return Math.round(n).toLocaleString('en-US')
  }
  if (_isMoneyCol(colName)) {
    if (n > 0 && n < 0.01) return '<$0.01'
    return '$' + n.toLocaleString('en-US',
      { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  }
  // a plain number column (e.g. line_items): integer with separators,
  // still never exponent.
  if (Number.isInteger(n)) return n.toLocaleString('en-US')
  return s
}

// Header label with a unit suffix: token cols → "… tokens" (already
// named that way in the SQL), money cols → "($)". Cosmetic only.
export function headerLabel(name) {
  if (_isMoneyCol(name) && !/\$/.test(name)) return `${name} ($)`
  return name
}

function consoleAthenaQuery(region, executionId) {
  return `https://${region}.console.aws.amazon.com/athena/home?region=${region}#/query-editor/history/${executionId}`
}

function fmtAge(seconds) {
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  if (m < 60) return `${m} min`
  return `${Math.floor(m / 60)} hr`
}

function QueryCard({ query, active, onClick }) {
  return (
    <button
      onClick={onClick}
      className={
        'w-full text-left px-4 py-3 rounded-lg border transition-colors ' +
        (active
          ? 'border-[var(--accent)] bg-[var(--accent)]/5 text-[var(--accent)]'
          : 'border-[var(--border)] bg-white hover:border-[var(--accent)]/40 text-[var(--ink)]')
      }
    >
      <div className="text-sm font-semibold leading-tight">{displayName(query)}</div>
      {query.description && (
        <div className="text-xs text-[var(--ink-4)] mt-0.5 leading-snug">
          {query.description}
        </div>
      )}
    </button>
  )
}

function ErrBanner({ err, label }) {
  if (!err) return null
  const code = err.code
  const msg = err.message || String(err)
  if (code === 'creds_expired') {
    return (
      <div className="text-sm bg-amber-50 border border-amber-300 rounded px-3 py-2 text-amber-900">
        <div className="font-semibold">
          Container AWS credentials expired
        </div>
        <div className="mt-0.5 text-amber-800">
          Run <code>scripts/tg-creds-refresh.sh</code> on the
          host or enable the systemd timer
          (<code>docs/creds-refresh.md</code>), then reload.
        </div>
      </div>
    )
  }
  return (
    <div className="text-sm text-[var(--red)] px-1">
      {label ? `${label}: ` : ''}{msg}
    </div>
  )
}

// #830: a non-empty cell that is purely digits/decimal/sign/$/,/%/
// space reads as numeric — same predicate the alignment uses, so
// sort and right-align agree. Blank strings are NOT numeric (an
// all-blank column shouldn't sort numerically / right-align).
export function isNumericCell(v) {
  const s = (v ?? '').toString()
  return s.trim() !== '' && /^[\d.\-$,%\s]*$/.test(s)
}

const _DATE_RE = /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$/

// #830: type-aware cell comparator for client-side column sort.
// A naive string sort is wrong for currency ("$1,041.86" < "$581.70"
// lexically), so:
//   - numeric/currency/percent → strip $ , % and whitespace, compare
//     as numbers;
//   - date-shaped (YYYY-MM-DD[ HH:MM[:SS]]) → compare chronologically;
//   - otherwise → case-insensitive string compare.
// Blank cells sort last (ascending) regardless of the column's type.
export function compareCells(a, b) {
  const sa = (a ?? '').toString().trim()
  const sb = (b ?? '').toString().trim()
  if (sa === '' || sb === '') {
    // blanks last: empty is "greater" so it sinks in ascending order
    if (sa === '' && sb === '') return 0
    return sa === '' ? 1 : -1
  }
  // Date check MUST precede the numeric check: a date like
  // "2026-06-01" also matches isNumericCell (digits + '-' are in the
  // class), and parseFloat("2026-06-01") is 2026 for every row in the
  // column → all-equal. Match the full date shape first.
  if (_DATE_RE.test(sa) && _DATE_RE.test(sb)) {
    const da = Date.parse(sa.replace(' ', 'T'))
    const db = Date.parse(sb.replace(' ', 'T'))
    if (!Number.isNaN(da) && !Number.isNaN(db)) return da - db
  }
  if (isNumericCell(sa) && isNumericCell(sb)) {
    const na = parseFloat(sa.replace(/[$,%\s]/g, ''))
    const nb = parseFloat(sb.replace(/[$,%\s]/g, ''))
    if (!Number.isNaN(na) && !Number.isNaN(nb)) return na - nb
  }
  return sa.toLowerCase().localeCompare(sb.toLowerCase())
}

// #830: sort `rows` (array-of-arrays) by column index + direction,
// using the type-aware comparator. dir 'asc' | 'desc'. Pure + stable
// (Array.prototype.sort is stable in modern engines); col === null
// returns the rows unchanged (server order).
export function sortRows(rows, col, dir) {
  if (col === null || col === undefined) return rows
  const out = rows.map((r, i) => [r, i])
  out.sort((x, y) => {
    const c = compareCells(x[0][col], y[0][col])
    if (c !== 0) return dir === 'desc' ? -c : c
    return x[1] - y[1]  // stable tiebreak on original index
  })
  return out.map(([r]) => r)
}

export function ResultTable({ columns, rows }) {
  // sort = { col: <index>, dir: 'asc'|'desc' } | null (server order).
  const [sort, setSort] = useState(null)

  // Reset to server order whenever a different report/query is run
  // (columns/rows identity changes on each fetch — see CostReports.run).
  useEffect(() => { setSort(null) }, [columns, rows])

  const sortedRows = useMemo(
    () => (sort ? sortRows(rows, sort.col, sort.dir) : rows),
    [rows, sort],
  )

  function toggleSort(col) {
    setSort(prev => {
      if (!prev || prev.col !== col) return { col, dir: 'asc' }
      if (prev.dir === 'asc') return { col, dir: 'desc' }
      return null  // third activation clears back to server order
    })
  }

  if (!columns.length) return (
    <div className="p-6 text-center text-sm text-[var(--ink-4)]">No rows returned.</div>
  )
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="bg-[var(--surface)] border-b-2 border-[var(--border)]">
            {columns.map((c, ci) => {
              const active = sort && sort.col === ci
              const ariaSort = active
                ? (sort.dir === 'asc' ? 'ascending' : 'descending')
                : 'none'
              return (
                <th
                  key={c}
                  aria-sort={ariaSort}
                  className="px-3 py-2 text-left text-[11px] font-bold uppercase tracking-wider text-[var(--ink-3)] whitespace-nowrap"
                >
                  <button
                    type="button"
                    onClick={() => toggleSort(ci)}
                    className="inline-flex items-center gap-1 uppercase tracking-wider hover:text-[var(--ink-1)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                    title={`Sort by ${c}`}
                  >
                    {headerLabel(c)}
                    <span aria-hidden="true" className="text-[var(--ink-4)]">
                      {active ? (sort.dir === 'asc' ? '▲' : '▼') : '↕'}
                    </span>
                  </button>
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {sortedRows.length === 0 && (
            <tr>
              <td colSpan={columns.length} className="p-4 text-center text-[var(--ink-4)]">
                No rows.
              </td>
            </tr>
          )}
          {sortedRows.map((row, i) => (
            <tr key={i} className={'border-b border-[var(--border)] ' + (i % 2 === 1 ? 'bg-[var(--surface-2)]' : '')}>
              {row.map((v, j) => {
                const numeric = isNumericCell(v)
                return (
                  <td key={j} className={'px-3 py-2 ' + (numeric ? 'font-mono tabular-nums text-right' : '')}>
                    {formatCell(v, columns[j])}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function CostReports() {
  const [queries, setQueries]     = useState(null)
  const [loadErr, setLoadErr]     = useState(null)
  const [curNotConfigured, setCurNotConfigured] = useState(false)
  const [selectedId, setSelectedId] = useState(null)
  const [result, setResult]       = useState(null)
  const [running, setRunning]     = useState(false)
  const [runErr, setRunErr]       = useState(null)
  const [region]                  = useState('us-east-1')
  // Part 2: a date range shared by ALL reports. Blank = month-to-date
  // (the historic default). Applies to every report whose SQL carries
  // the {{DATE_FILTER}} token (the MTD-class reports + the new one);
  // the windowed daily-trend / monthly-history reports keep their own
  // window (they have no token, so the API substitution is a no-op).
  // Prefill the REAL default window (month-start .. today) so the user
  // sees the actual dates being queried — no hidden "blank = month to
  // date" state. Equivalent to the API's empty-default MTD semantics.
  const [startDate, setStartDate] = useState(monthStartISO())
  const [endDate, setEndDate]     = useState(todayISO())

  useEffect(() => {
    getAnalyticsQueries()
      .then(d => {
        const qs = d.queries || []
        setQueries(qs)
        setCurNotConfigured(!!d.cur_not_configured)
        if (qs.length) setSelectedId(qs[0].query_id)
      })
      .catch(e => setLoadErr(e))
  }, [])

  const range = rangeState(startDate, endDate)

  async function run({ refresh = false, start, end } = {}) {
    if (!selectedId) return
    // Allow explicit start/end overrides so a caller that just cleared
    // the range (e.g. the reset-to-this-month button) can run the new
    // window in the same click without waiting for the setState to
    // flush. Fall back to the current range state otherwise.
    const s = start !== undefined ? start : startDate
    const e = end !== undefined ? end : endDate
    const r0 = rangeState(s, e)
    // Don't run an invalid range — surface an inline error instead of
    // a server 400 (the API also rejects it; this is the fast path).
    if (!r0.valid) {
      setRunErr({ message:
        'Invalid date range: pick both start and end, with start on '
        + 'or before end (or clear both for month-to-date).' })
      return
    }
    setRunning(true); setRunErr(null); setResult(null)
    try {
      const r = await runAnalyticsQuery(selectedId, {
        refresh, start: s, end: e,
      })
      setResult(r)
    } catch (e) {
      setRunErr(e)
    } finally {
      setRunning(false)
    }
  }

  // "Reset to this month" — re-set From/To to the real month-start ..
  // today window AND run in the same click (run with explicit dates so
  // it doesn't read the stale pre-reset state), so resetting is one
  // action. (Replaces the old blank-means-MTD reset; the window is now
  // always explicit dates.)
  function resetRange() {
    const s = monthStartISO()
    const e = todayISO()
    setStartDate(s); setEndDate(e)
    run({ start: s, end: e })
  }

  function select(id) {
    if (id === selectedId) return
    setSelectedId(id)
    setResult(null)
    setRunErr(null)
  }

  const selected = queries?.find(q => q.query_id === selectedId)

  return (
    <div className="p-8 flex flex-col gap-5 h-full">
      <div className="border-b border-[var(--border)] pb-3">
        <h1 className="m-0 text-2xl font-semibold">Cost Reports</h1>
      </div>

      {loadErr && (
        <ErrBanner err={loadErr} label="Failed to load queries" />
      )}

      {!loadErr && !queries && (
        <div className="text-sm text-[var(--ink-4)]">Loading queries…</div>
      )}

      {queries && queries.length === 0 && curNotConfigured && (
        <div className="rounded border border-[var(--border)] p-4 text-sm">
          <div className="font-medium mb-1">CUR not configured</div>
          <p className="m-0 text-[var(--ink-4)]">
            The <code>tg-cur-athena</code> stack is optional and isn't deployed in
            this account. Cost Reports needs it for per-user spend reconciliation
            (Activity page already shows real-time spend from Postgres).
          </p>
          <p className="m-0 mt-2 text-[var(--ink-4)]">
            To enable, run <code>scripts/tg-cur-deploy.sh</code>. See{' '}
            <a
              href="https://github.com/jamesypub/tokengov/blob/main/INSTALL.md#cur--cost--usage-reports-optional"
              target="_blank"
              rel="noreferrer"
              className="underline"
            >INSTALL.md § CUR</a>.
          </p>
          <p className="m-0 mt-2 text-[var(--ink-4)]">
            After deploy, CUR data takes 24-48h to backfill before queries return rows.
          </p>
        </div>
      )}

      {queries && queries.length === 0 && !curNotConfigured && (
        <div className="text-sm text-[var(--ink-4)]">
          No saved queries found in the <code>tg-cur-analytics</code> Athena workgroup.
        </div>
      )}

      {queries && queries.length > 0 && (
        <div className="flex gap-5 flex-1 min-h-0">
          {/* Left: query list */}
          <div className="flex flex-col gap-2 w-64 shrink-0">
            {queries.map(q => (
              <QueryCard
                key={q.query_id}
                query={q}
                active={q.query_id === selectedId}
                onClick={() => select(q.query_id)}
              />
            ))}
          </div>

          {/* Right: run panel + results */}
          <div className="flex-1 flex flex-col gap-4 min-w-0">
            {selected && (
              <div className="bg-white border border-[var(--border)] rounded-lg p-4 flex flex-col gap-3">
                <div className="flex items-start justify-between gap-3 flex-wrap">
                  <div>
                    <div className="font-semibold">{displayName(selected)}</div>
                    {selected.description && (
                      <div className="text-sm text-[var(--ink-4)] mt-0.5">
                        {selected.description}
                      </div>
                    )}
                  </div>
                </div>

                {/* Part 2: date-range picker, shared by ALL reports.
                    Blank = month-to-date. Run + force-refresh live in
                    THIS row, adjacent to the From/To inputs they
                    parameterize (an action button belongs with its
                    inputs, not in the far top-right corner). */}
                <div className="flex items-end gap-2 flex-wrap border-t border-[var(--border)] pt-3">
                  <label className="flex flex-col gap-1 text-xs text-[var(--ink-3)]">
                    From
                    <input
                      type="date"
                      aria-label="Start date"
                      value={startDate}
                      max={endDate || undefined}
                      onChange={e => setStartDate(e.target.value)}
                      className="border border-[var(--border)] rounded px-2 py-1 text-sm"
                    />
                  </label>
                  <label className="flex flex-col gap-1 text-xs text-[var(--ink-3)]">
                    To
                    <input
                      type="date"
                      aria-label="End date"
                      value={endDate}
                      min={startDate || undefined}
                      onChange={e => setEndDate(e.target.value)}
                      className="border border-[var(--border)] rounded px-2 py-1 text-sm"
                    />
                  </label>
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={() => run({ refresh: false })}
                    disabled={running}
                  >
                    {running ? 'Running…' : 'Run'}
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={resetRange}
                    disabled={running}
                    title="Reset From/To to this month (month-start to today)"
                  >
                    Reset to this month
                  </Button>
                  {result && (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => run({ refresh: true })}
                      disabled={running}
                      title="Force refresh (bypasses cache)"
                    >
                      <RefreshCw size={13} />
                    </Button>
                  )}
                  {/* The window is now always explicit From/To dates
                      (no hidden blank-means-MTD), so show only the
                      invalid-range warning — a valid range is already
                      visible in the date inputs. */}
                  {!range.valid && (
                    <span className="text-xs px-2 py-1 text-[var(--red)] font-semibold">
                      {rangeLabel(startDate, endDate)}
                    </span>
                  )}
                </div>

                <details>
                  <summary className="text-xs text-[var(--ink-3)] cursor-pointer select-none">
                    Show SQL
                  </summary>
                  <pre className="mt-2 p-3 bg-[var(--border)] rounded text-xs overflow-auto leading-relaxed">
                    {selected.query_string}
                  </pre>
                </details>
              </div>
            )}

            {runErr && (
              <ErrBanner err={runErr} />
            )}

            {/* #483: CUR exists but lacks IAM-principal data —
                warn instead of showing a silently-unattributed
                report. */}
            {result && !result.still_running &&
             result.principal_data_present === false && (
              <div className="bg-amber-50 border border-amber-300 rounded-lg p-4 text-sm">
                <div className="font-medium mb-1">
                  Per-user attribution unavailable
                </div>
                <p className="m-0 text-[var(--ink-4)]">
                  This report ran, but the CUR has no
                  {' '}<code>line_item_iam_principal</code> data, so
                  spend can't be attributed per user. Enable it in
                  the console: <strong>Billing → Data Exports →
                  your CUR 2.0 export → Include caller identity
                  (IAM principal) allocation data</strong>, then
                  wait for the next CUR delivery (hours–~a day)
                  before re-running.
                </p>
              </div>
            )}

            {result && !result.still_running && (
              <div className="bg-white border border-[var(--border)] rounded-lg overflow-hidden flex flex-col">
                <div className="px-4 py-2 border-b border-[var(--border)] flex items-center justify-between gap-2 flex-wrap">
                  <div className="text-sm text-[var(--ink-3)]">
                    {result.row_count} row{result.row_count === 1 ? '' : 's'}
                    {result.cached && result.cache_age_sec != null && (
                      <span className="ml-2 px-2 py-0.5 rounded-full text-xs font-semibold bg-amber-100 text-amber-800">
                        Cached · {fmtAge(result.cache_age_sec)} ago
                      </span>
                    )}
                    {!result.cached && (
                      <span className="ml-2 px-2 py-0.5 rounded-full text-xs font-semibold bg-emerald-50 text-emerald-700">
                        Live
                      </span>
                    )}
                  </div>
                  {result.execution_id && (
                    <a
                      href={consoleAthenaQuery(region, result.execution_id)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-[var(--accent)] font-semibold hover:underline"
                    >
                      View in Athena ↗
                    </a>
                  )}
                </div>
                <ResultTable columns={result.columns} rows={result.rows} />
              </div>
            )}

            {result?.still_running && (
              <div className="bg-amber-50 border border-amber-300 rounded-lg p-4 text-sm">
                Query is still running on Athena (exceeded Lambda timeout).{' '}
                <a
                  href={consoleAthenaQuery(region, result.execution_id)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[var(--accent)] font-semibold hover:underline"
                >
                  Watch it in the console ↗
                </a>
              </div>
            )}

            {!result && !running && !runErr && (
              <div className="flex-1 flex items-center justify-center text-sm text-[var(--ink-4)]">
                Select a query and click Run
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
