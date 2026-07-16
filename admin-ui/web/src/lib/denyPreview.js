// #746 (reverses #630/#626): build the illustrative DENYLIST deny
// statement the Org Settings blocked-model editor previews. It
// MIRRORS the reconciler's DenyBlockedModels statement
// (worker/jobs/deny_reconciler.py) so the admin sees exactly what
// their selection compiles into tg-BedrockQuotaDeny.
//
// The preview is illustrative only — the reconciler is the source
// of truth (it reads the saved blocked-model list each cycle).
// Kept as pure functions so they're unit-testable + reused by the UI.

// The four Bedrock invoke actions a model-restriction deny MUST
// list. Converse / ConverseStream are distinct IAM actions — NOT
// auto-blocked by an InvokeModel deny — so a blocked model stays
// reachable via Converse unless denied explicitly.
// (Epic #618; matches _INVOKE_ACTIONS in the reconciler.)
export const INVOKE_ACTIONS = [
  'bedrock:InvokeModel',
  'bedrock:InvokeModelWithResponseStream',
  'bedrock:Converse',
  'bedrock:ConverseStream',
]

// #746: CRIS geo prefixes the catalog model_ids carry. A blocked
// model must match in EVERY region/profile, so we strip the geo
// prefix to a region/account/profile-agnostic token and wildcard
// it into the resource ARNs. Mirrors _CRIS_PREFIXES / _agnostic_token
// in the reconciler.
const CRIS_PREFIXES = ['us.', 'global.', 'apac.', 'eu.']

export function agnosticToken(modelId) {
  const s = (modelId || '').trim()
  for (const p of CRIS_PREFIXES) {
    if (s.startsWith(p)) return s.slice(p.length)
  }
  return s
}

// blockedResources(modelIds) — the deduped Resource ARN list a
// block-list compiles into: per model, both an inference-profile
// and a foundation-model wildcard on the agnostic token. The `*`
// spans '/', so us.* / global.* / every region match the one token.
export function blockedResources(modelIds) {
  const out = []
  const seen = new Set()
  for (const id of (modelIds || [])) {
    const tok = agnosticToken(id)
    if (!tok) continue
    for (const res of [
      `arn:aws:bedrock:*:*:inference-profile/*${tok}*`,
      `arn:aws:bedrock:*::foundation-model/*${tok}*`,
    ]) {
      if (!seen.has(res)) { seen.add(res); out.push(res) }
    }
  }
  return out
}

// denyPreviewStatement(blocked) — the DenyBlockedModels statement
// for the given blocked model_ids, or null when the list is empty
// (an empty block-list emits NO statement → every model allowed,
// fail-open; the preview reflects that exact behavior).
export function denyPreviewStatement(blocked) {
  const resources = blockedResources(blocked)
  if (resources.length === 0) return null
  return {
    Sid: 'DenyBlockedModels',
    Effect: 'Deny',
    Action: [...INVOKE_ACTIONS],
    Resource: resources,
  }
}

// denyPreviewDocument(blocked) — the full policy doc shape (or
// null when no block-list is configured), pretty-printed by the
// caller for the preview panel.
export function denyPreviewDocument(blocked) {
  const stmt = denyPreviewStatement(blocked)
  if (!stmt) return null
  return { Version: '2012-10-17', Statement: [stmt] }
}
