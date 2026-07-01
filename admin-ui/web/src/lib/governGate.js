// #946: shared model for the "can't govern yet — no IAM role ARN"
// lifecycle. Govern does a one-time AttachRolePolicy of
// tg-BedrockQuotaDeny to the principal's IAM role; a pre-registered
// user has no role ARN yet, so there's nothing to attach to. The
// blocker is the ARN, NOT Bedrock spend — an admin who knows the role
// can record it and govern immediately (no waiting for CUR to observe
// the user at Bedrock).
//
// This module is the SINGLE SOURCE for the copy that the three
// surfaces share so they can never drift (#946 acceptance): the
// disabled-Govern reason (UserDetail + Users row), the "awaiting AWS
// principal" status chip, and the post-pre-register informational
// notice. Pure + framework-free so it's unit-testable and importable
// by either page.

// The server (users.py _role_name_from_arn / _ROLE_FROM_ARN_RE)
// accepts ONLY `arn:aws:iam::<acct>:role/<name>` — the shape the
// aggregator rebuilds for assumed-role / service principals. Mirror
// it here so the UI validates before the round-trip.
export const ROLE_ARN_RE = /^arn:aws:iam::\d+:role\/.+/

// IDC permission-set roles (AWSReservedSSO_*) are surface-only — a
// deny attached directly is wiped on the next IDC re-provision, so tg
// never governs them. An admin must not be able to "record" one as a
// governable ARN either.
const _IDC_ROLE_RE = /:role\/(?:.*\/)?AWSReservedSSO_/

export function isRoleArn(s) {
  return ROLE_ARN_RE.test((s || '').trim())
}

export function isIdcRoleArn(s) {
  return _IDC_ROLE_RE.test((s || '').trim())
}

// The account segment of an `arn:aws:iam::<acct>:role/...` ARN, or ''.
export function arnAccountOf(s) {
  const m = /^arn:aws:iam::(\d+):/.exec((s || '').trim())
  return m ? m[1] : ''
}

// #946: the ONE copy string the disabled-Govern reason, the chip
// tooltip, and the pre-register notice all reference, so they stay
// consistent. Names the real blocker (no role ARN) AND both paths out
// (record it now, or it fills in on first Bedrock activity) — and
// never implies spend is *required*.
export const NO_ROLE_ARN_REASON =
  'Can’t govern yet — no IAM role ARN on record. tg attaches the '
  + 'spend cap / deny to the principal’s IAM role; there’s nothing to '
  + 'attach to until a role ARN is known. Add the role ARN below (or '
  + 'it’s filled in automatically once the principal is observed at '
  + 'Bedrock).'

// The short status-chip label + its accessible tooltip (shares the
// reason copy). Used on the Users row + UserDetail while ARN-less.
export const AWAITING_PRINCIPAL_CHIP = {
  label: '⏳ awaiting AWS principal',
  title: NO_ROLE_ARN_REASON,
}

// The pre-register informational notice (neutral, dismissible — an
// expected lifecycle state, never an error). Shares the same framing.
export const PREREGISTER_NOTICE_TITLE =
  'Governance pending an AWS principal'
export const PREREGISTER_NOTICE_BODY =
  'This user is tracked but not yet governable — tg enforces a spend '
  + 'cap / deny by attaching a policy to their IAM role, which isn’t '
  + 'known until they have an AWS principal. Add the role ARN now to '
  + 'govern immediately, or it fills in automatically after their '
  + 'first Bedrock activity.'

// isAwaitingPrincipal — a non-IDC, non-service user that has no role
// ARN yet (pre-registered, or an IAM-user/root that can't be
// governed). The chip + notice show for the ARN-less governable-once-
// it-has-an-ARN case: principal_arn is empty. (A non-role ARN — IAM
// user / root — is a different, terminal gate handled by its own
// reason copy, so it does NOT get the hopeful "awaiting" chip.)
export function isAwaitingPrincipal(user) {
  if (!user) return false
  if ((user.role_type || 'iam') === 'idc') return false
  if (user.is_service) return false
  return !user.principal_arn
}

// #1011: an IDC permission-set user IS governable now, but the deny
// is advisory until it reaches a role the user actually uses. tg sets
// governed=true + emits the per-person QuotaDeny; it bites once the
// policy is attached to a role in the user's call path — either they
// assume tg-consumer (tg attaches that itself), OR the IDC admin
// referenced tg-BedrockQuotaDeny on this user's permission set (the
// tg-QuotaDenyPermissionSet). tg cannot see the IDC management
// account, so the UI states the precondition rather than gating on
// it. These copy strings are shared by the list + detail surfaces.
export const IDC_GOVERN_NOTICE_TITLE =
  'IAM Identity Center permission-set user'
export const IDC_GOVERN_NOTICE_BODY =
  'Governing sets the cap / deny intent. It takes effect once the '
  + 'deny reaches a role this user actually uses — either they assume '
  + 'tg-consumer (governed automatically), or your IDC admin has '
  + 'referenced tg-BedrockQuotaDeny on this user’s permission set '
  + '(see tg-QuotaDenyPermissionSet). tg can’t see the IDC management '
  + 'account, so enforcement here is advisory until one of those is '
  + 'in place.'
// The governed-state sub-label for an IDC user.
export const IDC_GOVERNED_NOTE =
  'Governed (IDC — enforced via the permission-set policy reference, '
  + 'or tg-consumer).'
