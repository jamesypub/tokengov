"""
#432: pin the OIDC client-secret wiring so the
`UsePreviousValue=true`-literal regression can't come back.

The original bug (#398/#403 → #431 stage login outage):
  - the installer emitted `OidcClientSecret=UsePreviousValue=true`
    to `deploy --parameter-overrides` when the env var was unset,
  - `--parameter-overrides` does NOT honor that directive — it set
    the parameter to the literal string,
  - the template wired the secret as a plaintext env `Value`, so the
    literal landed in the api task-def env → Cognito 400.

These are structural assertions over the CFN template + installer
script (no live AWS). They fail loudly if anyone reintroduces the
plaintext env Value or the literal emission.
"""
from __future__ import annotations

import pathlib

import pytest

# Repo root: container/tests/<this>.py → ../../
_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TEMPLATE = _ROOT / "cfn" / "tg-container-stack.yaml"
_INSTALLER = _ROOT / "scripts" / "tg-ecs-install.sh"


def _load_cfn(path: pathlib.Path) -> dict:
    """Parse a CFN template, collapsing `!Ref`/`!Sub`/`!If`/… short
    tags to plain Python so we can introspect structure."""
    yaml = pytest.importorskip("yaml")

    class _Loader(yaml.SafeLoader):
        pass

    def _ctor(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {tag_suffix: loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {tag_suffix: loader.construct_sequence(node)}
        return {tag_suffix: loader.construct_mapping(node)}

    _Loader.add_multi_constructor("!", _ctor)
    return yaml.load(_TEMPLATE.read_text(), Loader=_Loader)


@pytest.fixture(scope="module")
def cfn():
    return _load_cfn(_TEMPLATE)


def _api_container(cfn: dict) -> dict:
    cdefs = (
        cfn["Resources"]["ApiTaskDefinition"]
        ["Properties"]["ContainerDefinitions"]
    )
    return cdefs[0]


def test_oidc_secret_is_not_a_stack_parameter(cfn):
    """The OIDC secret must no longer be a CFN parameter — that
    was the channel the UsePreviousValue literal travelled through."""
    assert "OidcClientSecret" not in cfn["Parameters"]


def test_oidc_secret_resource_exists_and_is_gated(cfn):
    res = cfn["Resources"]["OidcClientSecretManaged"]
    assert res["Type"] == "AWS::SecretsManager::Secret"
    # Only created when login is enabled.
    assert res.get("Condition") == "RequireLoginEnabled"


def test_oidc_secret_not_plaintext_env_value(cfn):
    """No `TG_OIDC_CLIENT_SECRET` entry may appear in the api
    container's plaintext Environment block (that was the
    plaintext-in-task-def weakness + the literal landing spot)."""
    env = _api_container(cfn).get("Environment", [])
    names = {e.get("Name") for e in env if isinstance(e, dict)}
    assert "TG_OIDC_CLIENT_SECRET" not in names


def test_oidc_secret_injected_via_secrets_ref(cfn):
    """TG_OIDC_CLIENT_SECRET must be injected via the `Secrets:`
    block as a Secrets Manager ValueFrom — and that ref must
    point at the managed secret resource."""
    secrets = _api_container(cfn)["Secrets"]
    # Secrets is an `!If [RequireLoginEnabled, <with>, <without>]`.
    branches = secrets["If"]
    with_login = branches[1]
    names = {s["Name"] for s in with_login}
    assert "TG_OIDC_CLIENT_SECRET" in names
    assert "DB_PASSWORD" in names
    entry = next(
        s for s in with_login
        if s["Name"] == "TG_OIDC_CLIENT_SECRET"
    )
    assert entry["ValueFrom"]["Ref"] == "OidcClientSecretManaged"
    # Login-off branch keeps only DB_PASSWORD.
    without_login = branches[2]
    assert {s["Name"] for s in without_login} == {"DB_PASSWORD"}


def test_exec_role_can_read_oidc_secret(cfn):
    """The task execution role must be able to read the OIDC
    secret at launch, else the task fails to start."""
    role = cfn["Resources"]["EcsTaskExecutionRole"]
    statements = (
        role["Properties"]["Policies"][0]
        ["PolicyDocument"]["Statement"]
    )
    # The OIDC read statement is wrapped in an !If for gating.
    sids = []
    for st in statements:
        if isinstance(st, dict) and "If" in st:
            sids.append(st["If"][1].get("Sid"))
        elif isinstance(st, dict):
            sids.append(st.get("Sid"))
    assert "ReadOidcSecret" in sids


def test_installer_emits_no_use_previous_value_literal():
    """The installer must never pass the OidcClientSecret literal
    `UsePreviousValue=true` to --parameter-overrides again."""
    text = _INSTALLER.read_text()
    # Allowed: the explanatory comment. Forbidden: the parameter
    # override emission. Assert no `OidcClientSecret=` override and
    # no `printf 'UsePreviousValue=true'` emission survive.
    assert "OidcClientSecret=" not in text
    assert "UsePreviousValue=true')" not in text


def test_installer_writes_secret_to_secrets_manager():
    """The installer must write the real secret via
    put-secret-value, targeting the stack's secret ARN."""
    text = _INSTALLER.read_text()
    assert "put-secret-value" in text
    assert "OidcClientSecretArn" in text


# ───────────────────── #782: ECS Cognito-login path ─────────────────


def test_installer_defaults_provider_to_cognito():
    """#782: the ECS installer must default TG_AUTH_PROVIDER to
    cognito (the always-on base login) — NOT force Okta up-front.
    With no OIDC issuer supplied it picks cognito; bring-your-own
    OIDC issuer flips it to okta."""
    text = _INSTALLER.read_text()
    assert "TG_AUTH_PROVIDER=cognito" in text
    # the okta branch is keyed off a pre-supplied OIDC issuer
    assert "TG_AUTH_PROVIDER=okta" in text


def test_installer_deploys_cognito_pool():
    """#782: the ECS installer must deploy tg-cognito-pool (it
    previously had ZERO references — the whole bug)."""
    text = _INSTALLER.read_text()
    assert "tg-cognito-pool" in text
    assert "cfn/tg-cognito-pool.yaml" in text
    # the pool needs a CallbackUrl derived from the ALB DNS
    assert "CallbackUrl=" in text
    assert "/auth/callback" in text


def test_installer_cognito_does_not_hardfail_on_missing_okta():
    """#782: the OIDC-trio hard-fail must be scoped to the okta
    provider only — the cognito path derives those values from the
    pool, so requiring them up-front would re-break the bug."""
    text = _INSTALLER.read_text()
    # the hard-fail block is now gated on provider == okta
    assert 'TG_AUTH_PROVIDER" == "okta"' in text


def test_installer_wires_cognito_provisioning_into_stack():
    """#782: when provider=cognito the scale-to-1 deploy must turn
    on EnableCognitoAdminProvisioning and pass the pool id + arn so
    the container gets TG_AUTH_PROVIDER=cognito + the user-pool id."""
    text = _INSTALLER.read_text()
    assert "EnableCognitoAdminProvisioning=" in text
    assert "CognitoUserPoolId=" in text
    assert "CognitoUserPoolArn=" in text


def test_installer_reads_cognito_client_secret_for_sm():
    """#782: Cognito doesn't expose the app-client secret as a CFN
    output, so the installer must read it live (describe-user-pool-
    client) before writing it to Secrets Manager in step 7b."""
    text = _INSTALLER.read_text()
    assert "describe-user-pool-client" in text


# ──────────────── #797: teardown must not strand DELETE_FAILED ──────
#
# tg-container-stack delete left DELETE_FAILED on two resources, which
# then blocked the next install's changeset. The ECR repo couldn't
# delete while it held pushed images; ApiTargetGroup couldn't delete
# while the (DeletionPolicy: Retain) listener still forwarded to it.


def test_ecr_repo_empties_on_delete(cfn):
    """EcrRepository must set EmptyOnDelete so CFN purges its images on
    stack delete instead of stranding DELETE_FAILED."""
    props = cfn["Resources"]["EcrRepository"]["Properties"]
    assert props.get("EmptyOnDelete") is True


def test_alb_deletes_before_target_group(cfn):
    """Both ALBs must DependsOn ApiTargetGroup so CFN deletes the ALB
    (cascading its retained listeners) BEFORE the target group — else
    the TG is 'in use by a listener' and the delete fails. _load_cfn
    collapses !Ref etc., but DependsOn is a plain scalar/list."""
    def deps(name):
        d = cfn["Resources"][name].get("DependsOn", [])
        return [d] if isinstance(d, str) else list(d)
    assert "ApiTargetGroup" in deps("Alb")
    assert "ApiTargetGroup" in deps("AlbByo")


# ──────── #813 (#809): grant for the detach-orphan reconcile ─────────

def test_ecs_task_role_can_list_quota_deny_attachments(cfn):
    """#813: the EcsTaskRole must grant iam:ListEntitiesForPolicy on
    the deny-policy ARN — the deny_reconciler's #809 detach-orphan
    pass needs it (else it degrades to attach-only, never cleaning up
    stale attachments). Read-only; scoped to the policy resource."""
    role = cfn["Resources"]["EcsTaskRole"]
    statements = (
        role["Properties"]["Policies"][0]
        ["PolicyDocument"]["Statement"]
    )
    stmt = next(
        (s for s in statements
         if isinstance(s, dict) and s.get("Sid") ==
         "ListQuotaDenyAttachments"),
        None,
    )
    assert stmt is not None, "ListQuotaDenyAttachments Sid missing"
    actions = stmt["Action"]
    actions = [actions] if isinstance(actions, str) else actions
    assert "iam:ListEntitiesForPolicy" in actions
    # scoped to the deny-policy ARN (a !Sub → {"Sub": "...DenyPolicyName"}),
    # NOT a blanket Resource: '*'.
    res = stmt["Resource"]
    res = [res] if not isinstance(res, list) else res
    assert "DenyPolicyName" in str(res)
    assert res != ["*"]
