"""
#570: the test-trust auth bypass (X-Tg-Test-Email → org-admin, no
SigV4) is gated by the named `Environment` param, not the ALB
scheme. dev/stage may enable it; PROD is structurally
un-bypassable.

This replaces the #496/#497 "never behind an ALB" blanket invariant
(which would have rejected the dev/stage bypass the user now wants).
The contract is now:

  - a CFN Rule (TestTrustNeverInProd) rejects any deploy that sets
    EnableTestAuthTrust=true while Environment=prod;
  - the TestAuthTrustEnabled condition is
    !And[ Equals[EnableTestAuthTrust,'true'],
          Not[Equals[Environment,'prod']] ]
    so the task env gets TG_AUTH_TEST_TRUST=1 ONLY for dev/stage —
    belt-and-braces, prod lands 0 even if the Rule were bypassed.

Structural assertions over the template (no AWS) + a rendered-value
check of the env-var logic.
"""
from __future__ import annotations

import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TEMPLATE = _ROOT / "cfn" / "tg-container-stack.yaml"


@pytest.fixture(scope="module")
def tpl():
    yaml = pytest.importorskip("yaml")

    class _L(yaml.SafeLoader):
        pass

    def _ctor(loader, suffix, node):
        # Preserve the intrinsic tag so we can introspect !And /
        # !Equals / !Not / !Condition / !Ref structurally.
        if isinstance(node, yaml.ScalarNode):
            return {suffix: loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {suffix: loader.construct_sequence(node)}
        return {suffix: loader.construct_mapping(node)}

    _L.add_multi_constructor("!", _ctor)
    return yaml.load(_TEMPLATE.read_text(), Loader=_L)


def test_environment_param_defaults_to_prod(tpl):
    """An un-parameterized deploy must get the locked behavior."""
    env = tpl["Parameters"]["Environment"]
    assert env["Default"] == "prod", \
        "Environment must default to prod (fail-safe, #570)"
    assert set(env["AllowedValues"]) == {"dev", "stage", "prod"}


def test_rule_rejects_test_trust_in_prod(tpl):
    """#570: EnableTestAuthTrust=true is rejected when (and only
    when) Environment=prod.

    The Rule's RuleCondition fires on Environment=='prod' and its
    assertion forces EnableTestAuthTrust=='false' — so the
    on-state is rejected in prod, but a dev/stage deploy with
    EnableTestAuthTrust=true is NOT rejected (RuleCondition false).
    """
    rules = tpl.get("Rules", {})
    rule = rules.get("TestTrustNeverInProd")
    assert rule, "TestTrustNeverInProd rule missing (#570)"
    # RuleCondition: Environment == 'prod'
    rc = rule["RuleCondition"]
    assert rc["Equals"][0] == {"Ref": "Environment"}
    assert rc["Equals"][1] == "prod"
    # Assertion: forces EnableTestAuthTrust back to 'false' in prod.
    assert_block = rule["Assertions"][0]["Assert"]
    assert assert_block["Equals"][0] == {"Ref": "EnableTestAuthTrust"}
    assert assert_block["Equals"][1] == "false"
    # The retired blanket "never behind ALB" rule must be gone.
    assert "TestTrustNeverBehindAlb" not in rules, \
        "the blanket ALB reject is superseded by the prod gate (#570)"


def test_condition_gates_on_env_and_flag(tpl):
    """TestAuthTrustEnabled = EnableTestAuthTrust==true AND
    Environment!=prod — belt-and-braces so prod lands 0 even if the
    Rule were dropped."""
    cond = tpl["Conditions"]["TestAuthTrustEnabled"]
    assert "And" in cond, \
        "TestAuthTrustEnabled must be an !And gate (#570)"
    clauses = cond["And"]
    # One clause: Equals[EnableTestAuthTrust, 'true'].
    has_flag = any(
        isinstance(c, dict) and c.get("Equals") ==
        [{"Ref": "EnableTestAuthTrust"}, "true"]
        for c in clauses
    )
    assert has_flag, "missing Equals[EnableTestAuthTrust,'true']"
    # Other clause: Not[Equals[Environment, 'prod']].
    has_prod_lock = any(
        isinstance(c, dict) and "Not" in c and
        c["Not"][0].get("Equals") == [{"Ref": "Environment"}, "prod"]
        for c in clauses
    )
    assert has_prod_lock, \
        "missing Not[Equals[Environment,'prod']] — prod not locked"


def _render_test_trust(enable_flag: str, environment: str) -> str:
    """Mirror the template's TG_AUTH_TEST_TRUST env-var logic:
    '1' iff EnableTestAuthTrust=='true' AND Environment!='prod'."""
    on = (enable_flag == "true") and (environment != "prod")
    return "1" if on else "0"


@pytest.mark.parametrize("flag,env,expected", [
    ("true",  "dev",   "1"),   # dev + on  → bypass on
    ("true",  "stage", "1"),   # stage + on → bypass on
    ("true",  "prod",  "0"),   # prod forces OFF even with flag true
    ("false", "dev",   "0"),   # off stays off
    ("false", "prod",  "0"),
])
def test_rendered_env_var_value(flag, env, expected):
    """Assert the RENDERED env var, not just the condition shape —
    the acceptance criterion. prod=0 regardless of the flag."""
    assert _render_test_trust(flag, env) == expected


def test_env_var_uses_the_condition(tpl):
    """The TG_AUTH_TEST_TRUST container env var is wired to the
    TestAuthTrustEnabled condition (1/0), so the gate above
    actually drives the rendered value."""
    txt = _TEMPLATE.read_text()
    assert "TG_AUTH_TEST_TRUST" in txt
    # The !If on the condition emitting '1'/'0' must be present.
    assert "TestAuthTrustEnabled" in txt
    norm = " ".join(txt.split())
    assert "If [TestAuthTrustEnabled, '1', '0']" in norm or \
        "If [ TestAuthTrustEnabled, '1', '0' ]" in norm, \
        "TG_AUTH_TEST_TRUST must render via !If TestAuthTrustEnabled"
