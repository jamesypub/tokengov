import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { Card } from '../ui/Card'
import { Badge } from '../ui/Badge'
import { ExternalLink } from 'lucide-react'

// The public tokengov repo's new-issue endpoint, deep-linked to the
// low-friction issue forms (bug.yml / feature_request.yml) so the
// right form is pre-selected. Customers see the PUBLIC tokengov repo,
// never the private dev repo. Configurable via a build-time constant
// (Vite env) so a fork/rename doesn't dangle; defaults to tokengov.
const TOKENGOV_REPO_URL =
  (import.meta.env && import.meta.env.VITE_TOKENGOV_REPO_URL) ||
  'https://github.com/jamesypub/tokengov'
const NEW_ISSUE = (template) =>
  `${TOKENGOV_REPO_URL}/issues/new?template=${template}`

// Read-only diagnostics, relocated out of Org Settings into its own
// top-level page: the CUR spend source + health and newly-seen models,
// plus a "Report an issue" block pointing at the public tokengov repo.
export default function Diagnostics() {
  const [curHealth, setCurHealth] = useState(null)
  const [curSource, setCurSource] = useState(null)
  const [newModels, setNewModels] = useState([])
  const [saml, setSaml] = useState(null)

  useEffect(() => {
    let cancelled = false
    api.curHealth()
      .then(d => { if (!cancelled) setCurHealth(d) })
      .catch(() => { if (!cancelled) setCurHealth(null) })
    // cur_source + new-models ride on the admin-config payload (written
    // by the installer); render only when present.
    api.getAdminConfig()
      .then(d => {
        if (cancelled) return
        setCurSource(d?.cur_source || null)
        setNewModels(d?.cur_new_models || [])
      })
      .catch(() => {})
    // The internal Cognito SAML provider name is no longer an admin
    // setting; ops reads it here (read-only) to inspect the Cognito
    // console when troubleshooting a broken SSO connection. Reuses the
    // existing GET /settings/saml — no new endpoint.
    api.getSamlSettings()
      .then(d => { if (!cancelled) setSaml(d) })
      .catch(() => { if (!cancelled) setSaml(null) })
    return () => { cancelled = true }
  }, [])

  return (
    <div className="max-w-3xl mx-auto px-4 py-6 flex flex-col gap-4">
      <h1 className="text-xl font-semibold text-[var(--ink-1)]">
        Diagnostics
      </h1>

      {/* Spend source & models — read-only (moved from Org Settings). */}
      <Card className="px-5 py-4">
        <div className="flex items-center gap-2 flex-wrap">
          <h2 className="text-base font-semibold text-[var(--ink-2)]">
            Spend source &amp; models
          </h2>
          <Badge variant="outline" aria-label="Read only">
            Read-only
          </Badge>
        </div>
        <div className="text-sm text-[var(--ink-4)] mt-1 mb-3">
          Per-user spend is sourced from AWS Cost &amp; Usage Reports
          (CUR&nbsp;2.0) via Athena — actual billed cost, not a token
          estimate. New users/spend can take up to ~24h to appear
          (billed-data delay); the current billing month self-heals on
          each delivery.
        </div>
        {curHealth && (
          <div
            className={
              'px-3 py-2 rounded border text-sm mb-3 ' +
              (curHealth.status === 'healthy'
                ? 'bg-green-50 border-green-200 text-[var(--green)]'
                : 'bg-amber-50 border-amber-300 text-amber-900')
            }
          >
            <strong>
              {curHealth.status === 'healthy'
                ? 'CUR healthy'
                : 'CUR attention needed'}
            </strong>
            {curHealth.detail ? ` — ${curHealth.detail}` : ''}
          </div>
        )}
        {curSource && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]">
            {curSource.glue_database && (<>
              <dt className="text-[var(--ink-4)]">Glue database</dt>
              <dd className="font-mono">{curSource.glue_database}</dd>
            </>)}
            {curSource.glue_table && (<>
              <dt className="text-[var(--ink-4)]">Glue table</dt>
              <dd className="font-mono">{curSource.glue_table}</dd>
            </>)}
            {curSource.athena_workgroup && (<>
              <dt className="text-[var(--ink-4)]">Athena workgroup</dt>
              <dd className="font-mono">{curSource.athena_workgroup}</dd>
            </>)}
            {curSource.s3_path && (<>
              <dt className="text-[var(--ink-4)]">S3 path</dt>
              <dd className="font-mono break-all">{curSource.s3_path}</dd>
            </>)}
            {curSource.data_through && (<>
              <dt className="text-[var(--ink-4)]">Data through</dt>
              <dd className="font-mono">{curSource.data_through}</dd>
            </>)}
          </dl>
        )}
        {/* Newly-discovered models — informational; no pricing action. */}
        {newModels && newModels.length > 0 && (
          <div className="mt-4 pt-3 border-t border-[var(--border)]">
            <div className="text-sm font-medium text-[var(--ink-2)] mb-1">
              Newly-seen models
            </div>
            <div className="text-sm text-[var(--ink-4)] mb-2">
              Models first observed in CUR recently. Informational —
              spend for these is already billed via CUR.
            </div>
            <ul className="text-[13px] font-mono space-y-0.5">
              {newModels.map(m => (
                <li key={m.model_id}>{m.model_id}</li>
              ))}
            </ul>
          </div>
        )}
      </Card>

      {/* SAML provider (Cognito) — read-only. The internal Cognito IdP
          name is tg-owned (auto-generated on save, not an admin setting);
          surfaced here so ops can find it to inspect the Cognito console
          when troubleshooting SSO. Only shown when SAML is configured. */}
      {saml?.configured && saml?.provider_name && (
        <Card className="px-5 py-4">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="text-base font-semibold text-[var(--ink-2)]">
              SAML provider (Cognito)
            </h2>
            <Badge variant="outline" aria-label="Read only">
              Read-only
            </Badge>
          </div>
          <div className="text-sm text-[var(--ink-4)] mt-1 mb-2">
            Managed by tg — the internal Cognito IdP name, for
            troubleshooting.
          </div>
          <div className="text-[13px] font-mono break-all text-[var(--ink-2)]">
            {saml.provider_name}
          </div>
        </Card>
      )}

      {/* Report an issue — Issues is the sole support channel (no email
          mailbox). Links to the PUBLIC tokengov repo's new-issue forms;
          opens in a new tab. */}
      <Card className="px-5 py-4">
        <h2 className="text-base font-semibold text-[var(--ink-2)]">
          Report an issue
        </h2>
        <div className="text-sm text-[var(--ink-4)] mt-1 mb-3">
          Found a bug or have a feature request? File it in the public
          tokengov repository — that's where bugs and requests are
          tracked. Include what happened and what you expected; steps to
          reproduce help. Reports are public and searchable.
        </div>
        <div className="flex flex-wrap gap-2">
          <a
            href={NEW_ISSUE('bug.yml')}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 h-8 px-3 rounded border text-sm text-[var(--ink-2)] hover:bg-[var(--surface-2)]"
          >
            Report a bug
            <ExternalLink size={14} aria-label="opens in a new tab" />
          </a>
          <a
            href={NEW_ISSUE('feature_request.yml')}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 h-8 px-3 rounded border text-sm text-[var(--ink-2)] hover:bg-[var(--surface-2)]"
          >
            Request a feature
            <ExternalLink size={14} aria-label="opens in a new tab" />
          </a>
        </div>
      </Card>
    </div>
  )
}
