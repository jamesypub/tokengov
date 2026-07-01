#!/usr/bin/env python3
"""Rendered-template assertion for the unconditional SSO trust in
cfn/tg-bedrock-role.yaml — no AWS, no deploy.

cfn-lint proves the template is structurally valid but does NOT resolve
!If / !Sub, so it can't prove the SSO ArnLike comes out as the
name-agnostic wildcard. This loads the template with a CFN-tag-aware
YAML loader, locates the unconditional SSO statement in the
TokenConsumerRole assume-role policy, and asserts:

  1. The SSO statement is emitted UNCONDITIONALLY — it is a plain dict
     (Effect/Principal/Action/Condition), NOT wrapped in an !If on
     HasSsoPrincipals (that gate moved to the ArnLike VALUE only).
  2. With an EMPTY TrustedSsoPrincipalArnLike, the ArnLike value
     resolves to exactly the name-agnostic
     arn:aws:iam::${TargetAccountId}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_*
  3. With a NON-EMPTY param, the ArnLike value is the operator's Ref.
  4. Principal stays AWS=<acct>:root (same-account scope), untouched.
  5. There is no bare-account-root fallback statement left.
"""
from __future__ import annotations

import sys
import yaml


# Minimal CFN intrinsic representation: load !Ref/!Sub/!If/!GetAtt as
# tagged dicts so we can introspect structure without resolving.
class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, tag_suffix, node):
    name = "Fn::" + tag_suffix if tag_suffix != "Ref" else "Ref"
    if isinstance(node, yaml.ScalarNode):
        val = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        val = loader.construct_sequence(node)
    else:
        val = loader.construct_mapping(node)
    return {name: val}


_CfnLoader.add_multi_constructor("!", _tag)


def load(path):
    with open(path) as f:
        # PyYAML sees the short tags (!Ref) via the "!" multi-constructor.
        return yaml.load(f, Loader=_CfnLoader)


def fail(msg):
    print(f"  FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def ok(msg):
    print(f"  ok: {msg}")


NAME_AGNOSTIC = (
    "arn:aws:iam::${TargetAccountId}:role/aws-reserved/"
    "sso.amazonaws.com/AWSReservedSSO_*"
)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "cfn/tg-bedrock-role.yaml"
    tpl = load(path)
    role = tpl["Resources"]["TokenConsumerRole"]["Properties"]
    statements = role["AssumeRolePolicyDocument"]["Statement"]

    print("== rendered-template: unconditional SSO trust ==")

    # Find the SSO statement: a plain dict (not an Fn::If) whose
    # Condition has an ArnLike on aws:PrincipalArn.
    sso = None
    if_wrapped_sso = False
    for st in statements:
        if "Fn::If" in st:
            # An !If-wrapped statement. If its true-branch carries the
            # ArnLike, the SSO trust is still gated on emission (the bug
            # this ticket removes).
            branch = st["Fn::If"][1] if len(st["Fn::If"]) > 1 else {}
            cond = branch.get("Condition", {}) if isinstance(branch, dict) else {}
            if "ArnLike" in cond:
                if_wrapped_sso = True
            continue
        cond = st.get("Condition", {})
        if isinstance(cond, dict) and "ArnLike" in cond:
            sso = st

    if if_wrapped_sso:
        fail("SSO statement is still wrapped in an Fn::If "
             "(emission gated) — must be unconditional")
    if sso is None:
        fail("no unconditional SSO ArnLike statement found")
    ok("SSO statement emitted unconditionally (plain dict, no Fn::If)")

    # Principal stays AWS=<acct>:root.
    principal = sso.get("Principal", {})
    aws_p = principal.get("AWS")
    if isinstance(aws_p, dict) and "Fn::Sub" in aws_p \
       and aws_p["Fn::Sub"].endswith(":root"):
        ok("Principal stays AWS=<acct>:root (same-account scope)")
    else:
        fail(f"Principal:AWS must be <acct>:root, got {aws_p!r}")

    # The ArnLike value is an Fn::If on HasSsoPrincipals choosing
    # [Ref override, Sub name-agnostic default].
    arnlike = sso["Condition"]["ArnLike"]["aws:PrincipalArn"]
    if not (isinstance(arnlike, dict) and "Fn::If" in arnlike):
        fail("ArnLike value must be an Fn::If selecting override vs "
             f"default, got {arnlike!r}")
    cond_name, true_branch, false_branch = arnlike["Fn::If"]
    if cond_name != "HasSsoPrincipals":
        fail(f"ArnLike Fn::If must key on HasSsoPrincipals, got {cond_name}")
    ok("ArnLike value selects on HasSsoPrincipals (value, not emission)")

    # NON-EMPTY param branch → operator's Ref.
    if true_branch == {"Ref": "TrustedSsoPrincipalArnLike"}:
        ok("non-empty param → operator's TrustedSsoPrincipalArnLike Ref")
    else:
        fail(f"true-branch must be Ref TrustedSsoPrincipalArnLike, "
             f"got {true_branch!r}")

    # EMPTY param branch → the name-agnostic !Sub default.
    if isinstance(false_branch, dict) and "Fn::Sub" in false_branch \
       and false_branch["Fn::Sub"] == NAME_AGNOSTIC:
        ok("empty param → name-agnostic AWSReservedSSO_* default")
    else:
        fail(f"false-branch must be the name-agnostic !Sub default, "
             f"got {false_branch!r}")

    # No bare-account-root fallback statement: every remaining plain
    # (non-Fn::If) statement must have a Condition (the SSO one does);
    # a fallback would be a plain Allow on :root with NO ArnLike.
    for st in statements:
        if "Fn::If" in st:
            continue
        if st is sso:
            continue
        cond = st.get("Condition", {})
        princ = st.get("Principal", {})
        aws_p = princ.get("AWS") if isinstance(princ, dict) else None
        is_root = isinstance(aws_p, dict) and \
            str(aws_p.get("Fn::Sub", "")).endswith(":root")
        if is_root and "ArnLike" not in (cond or {}):
            fail("a bare-account-root fallback statement remains")
    ok("no bare-account-root fallback statement remains")

    print("\nALL RENDERED-TEMPLATE CHECKS PASSED")


if __name__ == "__main__":
    main()
