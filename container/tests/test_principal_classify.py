"""
Tests for worker/principal_classify — the shared Bedrock
principal-ARN classifiers extracted from metrics_aggregator
(#723). Pure functions; no DB/AWS. Moved verbatim from
test_metrics_aggregator.py, repointed to the new module +
public names (classify_principal / classify_role_type).
"""
import pytest

from worker.principal_classify import (
    classify_principal, classify_role_type, resolve_principal,
)


# ── #345 principal classifier ────────────────────────────

@pytest.mark.parametrize("arn,expected", [
    # human assumed-role with email session
    (
        "arn:aws:sts::123:assumed-role/tg-consumer/"
        "alice@test.com",
        (
            "alice@test.com",
            "alice@test.com",
            "assumed_role",
            "arn:aws:iam::123:role/tg-consumer",
        ),
    ),
    # machine assumed-role: ECS task with non-email session.
    # #810: keyed on the role-session-name VERBATIM (the last
    # `/`-segment), NOT collapsed to `role:<RoleName>`. principal_arn
    # still carries the rebuilt role ARN for Manage/deny-attach.
    (
        "arn:aws:sts::123:assumed-role/MyEcsTaskRole/"
        "ecs-task-abc-1234",
        (
            "ecs-task-abc-1234",
            None,
            "service",
            "arn:aws:iam::123:role/MyEcsTaskRole",
        ),
    ),
    # machine assumed-role: Lambda with request-id session
    (
        "arn:aws:sts::123:assumed-role/MyLambdaRole/"
        "5e1c0d1a-aaaa-bbbb-cccc-deadbeef",
        (
            "5e1c0d1a-aaaa-bbbb-cccc-deadbeef",
            None,
            "service",
            "arn:aws:iam::123:role/MyLambdaRole",
        ),
    ),
    # service-linked role
    (
        "arn:aws:sts::123:assumed-role/aws-service-role/"
        "ecs.amazonaws.com/AWSServiceRoleForECS/some-session",
        (
            "slr:ecs.amazonaws.com",
            None,
            "service_linked",
            "arn:aws:sts::123:assumed-role/aws-service-role/"
            "ecs.amazonaws.com/AWSServiceRoleForECS/some-session",
        ),
    ),
    # iam_user (long-lived keys)
    (
        "arn:aws:iam::123:user/bob",
        (
            "bob", None, "iam_user",
            "arn:aws:iam::123:user/bob",
        ),
    ),
    # iam_user with email-shaped name
    (
        "arn:aws:iam::123:user/carol@test.com",
        (
            "carol@test.com",
            "carol@test.com",
            "iam_user",
            "arn:aws:iam::123:user/carol@test.com",
        ),
    ),
    # federated
    (
        "arn:aws:sts::123:federated-user/dan@test.com",
        (
            "dan@test.com",
            "dan@test.com",
            "federated",
            "arn:aws:sts::123:federated-user/dan@test.com",
        ),
    ),
    # root
    (
        "arn:aws:iam::123:root",
        (
            "root:123", None, "root",
            "arn:aws:iam::123:root",
        ),
    ),
])
def test_classify_principal(arn, expected):
    """#345: every Bedrock invocation ARN shape lands in
    one of seven principal_type buckets with the right
    identity_key + principal_arn."""
    assert classify_principal(arn) == expected


# ── #810 last-segment keying ──────────────────────────────

@pytest.mark.parametrize("arn,expected_key", [
    # +ops / +dev sessions are DISTINCT identities — the whole
    # session name is the key, never collapsed to a base email.
    (
        "arn:aws:sts::123:assumed-role/"
        "tg-install-from-123456789012/tg-org-admin+ops@example.com",
        "tg-org-admin+ops@example.com",
    ),
    (
        "arn:aws:sts::123:assumed-role/"
        "tg-install-from-123456789012/tg-org-admin+dev@example.com",
        "tg-org-admin+dev@example.com",
    ),
    # plain email session, no suffix.
    (
        "arn:aws:sts::123:assumed-role/tg-consumer/"
        "tg-org-admin@example.com",
        "tg-org-admin@example.com",
    ),
])
def test_email_session_keyed_verbatim_no_base_collapse(
    arn, expected_key
):
    """#810: an email-shaped session is keyed on the session name
    VERBATIM — `+ops`/`+dev` are kept, NOT folded to a base email.
    The spend key and the deny `aws:userid` key are this same
    string, so a base-email collapse would silently break caps."""
    identity_key, email, ptype, _ = classify_principal(arn)
    assert identity_key == expected_key
    assert email == expected_key
    assert ptype == "assumed_role"


def test_machine_instance_id_session_fragments_per_instance():
    """#810 (accepted consequence, owner eyes-open): a machine role
    whose session is an ephemeral instance-id keys on the
    instance-id itself, NOT `role:<RoleName>`. Two instances of the
    same role are two distinct identity_keys — the #627 role
    collapse is dropped."""
    k1, e1, t1, arn1 = classify_principal(
        "arn:aws:sts::123:assumed-role/dev-machine-role/i-0819dd4c")
    k2, _, _, _ = classify_principal(
        "arn:aws:sts::123:assumed-role/dev-machine-role/i-0aaaaaaa")
    assert k1 == "i-0819dd4c"
    assert k2 == "i-0aaaaaaa"
    assert k1 != k2                    # fragments per instance
    assert e1 is None and t1 == "service"
    # principal_arn still resolves to the role for display/Manage.
    assert arn1 == "arn:aws:iam::123:role/dev-machine-role"


def test_classify_principal_unknown_arn():
    """An ARN we don't recognize shouldn't drop the row;
    it goes to 'unknown' so the admin sees it on the
    Users page."""
    arn = "arn:aws:something:weird:not-real"
    identity_key, email, ptype, parn = classify_principal(arn)
    assert ptype == "unknown"
    assert identity_key.startswith("unknown:")
    assert email is None
    assert parn == arn

# ── #625 role-type classifier ────────────────────────────

@pytest.mark.parametrize("arn,expected", [
    # IDC permission-set role: path collapses into the
    # AWSReservedSSO_* role-name segment of the assumed-role ARN.
    (
        "arn:aws:sts::123:assumed-role/"
        "AWSReservedSSO_BedrockDeveloper_abc123/alice@test.com",
        "idc",
    ),
    # IDC role carrying the full reserved path.
    (
        "arn:aws:sts::123:assumed-role/aws-reserved/"
        "sso.amazonaws.com/AWSReservedSSO_Admin_x/bob@test.com",
        "idc",
    ),
    # normal human assumed-role via the tg chokepoint → iam.
    (
        "arn:aws:sts::123:assumed-role/tg-consumer/"
        "alice@test.com",
        "iam",
    ),
    # machine role → iam.
    (
        "arn:aws:sts::123:assumed-role/MyEcsTaskRole/"
        "ecs-task-abc",
        "iam",
    ),
    # iam user → iam.
    ("arn:aws:iam::123:user/bob", "iam"),
    # root → iam.
    ("arn:aws:iam::123:root", "iam"),
    # empty / unparseable → iam (safe default).
    ("", "iam"),
])
def test_classify_role_type(arn, expected):
    """#625: AWSReservedSSO_* roles (IDC permission sets) are
    classified `idc`; every other principal is `iam`. The UI
    disables Manage on idc rows."""
    assert classify_role_type(arn) == expected


# ── #1065 IDC principal_arn is the VALID path-form ──

@pytest.mark.parametrize("arn,expected_parn", [
    # collapsed assumed-role ARN (role capture is the bare
    # AWSReservedSSO_*) → rebuild the path-form role ARN.
    (
        "arn:aws:sts::123:assumed-role/"
        "AWSReservedSSO_BedrockDeveloper_abc123/alice@test.com",
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_BedrockDeveloper_abc123",
    ),
    # full-path assumed-role ARN (role capture is aws-reserved, the SSO
    # role rides in the session) → same path-form role ARN.
    (
        "arn:aws:sts::123:assumed-role/aws-reserved/"
        "sso.amazonaws.com/AWSReservedSSO_Admin_x/bob@test.com",
        "arn:aws:iam::123:role/aws-reserved/sso.amazonaws.com/"
        "AWSReservedSSO_Admin_x",
    ),
])
def test_idc_principal_arn_is_path_form(arn, expected_parn):
    """#1065: an IDC principal's stored principal_arn must be the VALID
    path-form role ARN (not the bare role/AWSReservedSSO_… form, which
    is an Invalid principal when written to a trust policy — the same
    defect #1064 fixed in the installer). The deny/trust wiring depends
    on it."""
    _ident, _email, ptype, parn = classify_principal(arn)
    assert ptype in ("assumed_role", "service")
    assert parn == expected_parn
    # and it round-trips through classify_role_type as idc
    assert classify_role_type(arn) == "idc"


# ── resolve_principal — email↔Bedrock-key attribution ──

_KEY_ARN = "arn:aws:iam::123456789012:user/MantleApiKey-uhbhn79a"
_KEY_MAP = {"MantleApiKey-uhbhn79a": ("dev@corp.com", "dev@corp.com")}


def test_resolve_maps_key_user_to_owner():
    """The crux: a `user/<mapped-name>` key principal re-attributes to
    the owning developer — identity_key+email become the owner's, type
    stays iam_user, and the ORIGINAL ARN is preserved (only the
    identity the spend lands on changes)."""
    ik, em, ptype, parn = resolve_principal(_KEY_ARN, _KEY_MAP)
    assert (ik, em, ptype) == ("dev@corp.com", "dev@corp.com", "iam_user")
    assert parn == _KEY_ARN


def test_resolve_no_map_is_classify_passthrough():
    """No map (or None) → identical to classify_principal (a mapped-in
    call with an empty map is a pure no-op)."""
    assert resolve_principal(_KEY_ARN, None) == classify_principal(_KEY_ARN)
    assert resolve_principal(_KEY_ARN, {}) == classify_principal(_KEY_ARN)


def test_resolve_unmapped_key_user_unchanged():
    """Regression guard: a key IAM-user with NO mapping keeps its raw
    iam_user classification (keyed on the raw name, no email) — exactly
    as before this feature."""
    other = "arn:aws:iam::123456789012:user/BedrockAPIKey-toqd"
    assert resolve_principal(other, _KEY_MAP) == classify_principal(other)
    ik, em, ptype, _ = resolve_principal(other, _KEY_MAP)
    assert (ik, em, ptype) == ("BedrockAPIKey-toqd", None, "iam_user")


@pytest.mark.parametrize("arn", [
    # a machine assumed-role (service) — NOT a key, must be untouched
    "arn:aws:sts::123:assumed-role/MyEcsTaskRole/ecs-task-abc-1234",
    # an IDC permission-set session — NOT a key, must be untouched
    "arn:aws:sts::123:assumed-role/AWSReservedSSO_Dev_x/bob@corp.com",
    # a human SSO session whose email happens to differ — not a key
    "arn:aws:sts::123:assumed-role/tg-consumer/alice@test.com",
])
def test_resolve_matching_rule_only_touches_mapped_keys(arn):
    """THE matching rule: the map is the discriminator. A principal
    that is NOT a mapped iam_user key (service role, AWSReservedSSO_*
    session, human SSO) is left EXACTLY as classify_principal returns
    — never rewritten to any email, even with a populated map."""
    assert resolve_principal(arn, _KEY_MAP) == classify_principal(arn)

