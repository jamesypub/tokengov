import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { Card } from '../ui/Card'
import { Button } from '../ui/Button'
import { Input } from '../ui/Input'

// Settings control for Bedrock invocation-logging capture (the
// analytics stream — separate from CUR spend). Reads/writes the
// region catalog via the slice-2 API ({regions:[{region,bucket,
// enabled,text_on}], updated_at}; PUT returns {regions, apply}).
//
// Copy is plain business language (per the standing UI directive): it
// describes the OUTCOME + the privacy implication, not internal
// bucket/policy names. The Text-capture privacy warning is unmissable
// and conveyed by TEXT (not color alone) for a11y.

const REGION_RE = /^[a-z]{2}-[a-z]+-\d+$/

// applyOutcomeLabel — plain-language summary of a per-region apply
// result the PUT returns, so the admin sees what actually happened.
export function applyOutcomeLabel(outcome) {
  switch (outcome) {
    case 'enabled': return 'capturing'
    case 'already_enabled':
      return 'left as-is (another logging config is already active here)'
    case 'disabled': return 'stopped'
    case 'not_ours':
      return 'left as-is (a different logging config is active here)'
    case 'noop': return 'no change'
    case 'failed': return 'not applied — retry'
    default: return outcome || ''
  }
}

export default function InvocationLogsSection() {
  const [rows, setRows] = useState(null)      // [{region,bucket,enabled,text_on}]
  const [loadErr, setLoadErr] = useState(null)
  const [addRegion, setAddRegion] = useState('')
  const [addErr, setAddErr] = useState(null)
  const [saving, setSaving] = useState(false)
  const [applyResult, setApplyResult] = useState(null)   // [{region,outcome}]
  const [savedMsg, setSavedMsg] = useState(null)

  async function load() {
    try {
      setLoadErr(null)
      const d = await api.getInvocationLogs()
      setRows(d.regions || [])
    } catch (e) { setLoadErr(e.message); setRows([]) }
  }
  useEffect(() => { load() }, [])

  function addChip() {
    const r = addRegion.trim().toLowerCase()
    if (!REGION_RE.test(r)) {
      setAddErr('Enter a valid AWS region (e.g. us-east-1).')
      return
    }
    if (rows.some(x => x.region === r)) {
      setAddErr('That region is already listed.')
      return
    }
    setAddErr(null)
    setRows([...rows, { region: r, enabled: true, text_on: true }])
    setAddRegion('')
  }

  function removeChip(region) {
    setRows(rows.filter(x => x.region !== region))
  }
  function setEnabled(region, on) {
    setRows(rows.map(x => x.region === region ? { ...x, enabled: on } : x))
  }
  function setTextOn(region, on) {
    setRows(rows.map(x => x.region === region ? { ...x, text_on: on } : x))
  }

  async function save() {
    setSaving(true); setSavedMsg(null); setApplyResult(null)
    try {
      const d = await api.setInvocationLogs(
        rows.map(x => ({
          region: x.region, enabled: !!x.enabled, text_on: !!x.text_on,
        })))
      setRows(d.regions || [])
      setApplyResult(d.apply || [])
      setSavedMsg('Saved.')
    } catch (e) {
      setSavedMsg(`Save failed: ${e.message}`)
    } finally {
      setSaving(false)
    }
  }

  const anyTextOn = rows && rows.some(x => x.enabled && x.text_on)

  return (
    <Card className="px-5 py-4">
      <h2 className="text-[11px] font-bold uppercase tracking-wider text-[var(--ink-3)] m-0">
        Bedrock invocation logs
      </h2>
      <div className="text-sm text-[var(--ink-4)] mt-1 mb-3">
        Optionally capture Bedrock invocation data — token counts and
        (when Text capture is on) the prompt and response text — for
        future model-selection analytics. The data is stored in{' '}
        <strong>your own AWS account’s S3</strong> and protected by{' '}
        <strong>your account’s security</strong> — it never leaves your
        account. This is <strong>separate</strong> from cost tracking
        and is <strong>off unless you add a region</strong> below. You
        can turn it off any time.
      </div>

      {loadErr && (
        <div role="alert" className="text-sm text-[var(--red,#b42318)] mb-2">
          Couldn’t load the current setting: {loadErr}
        </div>
      )}

      {rows === null ? (
        <div className="text-sm text-[var(--ink-4)]">Loading…</div>
      ) : (
        <>
          {/* Unmissable privacy notice — TEXT, not color alone (a11y).
              Shown whenever any enabled region has Text capture on. */}
          {anyTextOn && (
            <div
              role="note"
              className="text-[13px] border border-amber-300 bg-amber-50 text-amber-900 rounded px-3 py-2 mb-3"
            >
              ⚠ <strong>Privacy:</strong> Text capture stores the full
              prompt and response — your <strong>source code and AI
              output</strong> — to encrypted storage. Turn Text off for
              a region to capture usage counts only.
            </div>
          )}

          {rows.length === 0 ? (
            <div className="text-sm text-[var(--ink-4)] mb-3">
              No regions — invocation logging is off.
            </div>
          ) : (
            <ul className="flex flex-col gap-2 mb-3 list-none p-0 m-0">
              {rows.map(x => (
                <li
                  key={x.region}
                  className="flex flex-wrap items-center gap-3 border border-[var(--border)] rounded px-3 py-2"
                >
                  <span className="font-mono text-[13px]">{x.region}</span>
                  <label className="text-[13px] flex items-center gap-1">
                    <input
                      type="checkbox" checked={!!x.enabled}
                      onChange={e => setEnabled(x.region, e.target.checked)}
                      aria-label={`Capture in ${x.region}`}
                    />
                    Capture
                  </label>
                  <label className="text-[13px] flex items-center gap-1">
                    <input
                      type="checkbox" checked={!!x.text_on}
                      disabled={!x.enabled}
                      onChange={e => setTextOn(x.region, e.target.checked)}
                      aria-label={`Capture prompt and response text in ${x.region} (stores source code and AI output)`}
                    />
                    Text (prompt + response)
                  </label>
                  <button
                    type="button"
                    className="ml-auto text-[12px] text-[var(--ink-4)] underline hover:text-[var(--ink-2)]"
                    onClick={() => removeChip(x.region)}
                    aria-label={`Remove ${x.region}`}
                  >
                    Remove
                  </button>
                  {/* Full S3 path for an enabled region — where the
                      logs land in the customer's own account. Sourced
                      from the API (server-derived); hidden for off
                      regions and for a just-added-but-unsaved region
                      (no path until the save round-trip resolves it). */}
                  {x.enabled && x.s3_uri && (
                    <div className="basis-full text-[12px] text-[var(--ink-4)]">
                      Stored in your account at{' '}
                      <span className="font-mono break-all">{x.s3_uri}</span>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}

          <div className="flex items-end gap-2 mb-1">
            <div className="flex flex-col">
              <label htmlFor="invlogs-add" className="text-[12px] text-[var(--ink-3)]">
                Add a region
              </label>
              <Input
                id="invlogs-add" value={addRegion}
                placeholder="us-east-1"
                onChange={e => setAddRegion(e.target.value)}
                aria-describedby={addErr ? 'invlogs-add-err' : undefined}
              />
            </div>
            <Button variant="secondary" onClick={addChip} disabled={saving}>
              Add
            </Button>
          </div>
          {addErr && (
            <div id="invlogs-add-err" role="alert"
              className="text-[12px] text-[var(--red,#b42318)] mb-2">
              {addErr}
            </div>
          )}

          <div className="flex items-center gap-3 mt-3">
            <Button variant="primary" onClick={save} disabled={saving}>
              {saving ? 'Applying…' : 'Save & apply'}
            </Button>
            {savedMsg && (
              <span role="status" className="text-[13px] text-[var(--ink-3)]">
                {savedMsg}
              </span>
            )}
          </div>

          {applyResult && applyResult.length > 0 && (
            <ul role="status" className="text-[12px] text-[var(--ink-4)] mt-2 list-none p-0 m-0">
              {applyResult.map(a => (
                <li key={a.region}>
                  <span className="font-mono">{a.region}</span>:{' '}
                  {applyOutcomeLabel(a.outcome)}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </Card>
  )
}
