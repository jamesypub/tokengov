#!/usr/bin/env python3
"""Rendered-template assertion for the CUR reuse-or-create wiring in
cfn/tg-cur-athena.yaml — no AWS, no deploy.

cfn-lint proves structural validity but doesn't resolve !If/!Condition.
This loads the template with a CFN-tag-aware YAML loader and asserts:

  1. The AWS::BCMDataExports::Export resource is gated on
     Condition: ShouldCreateExport — so CreateExport=false drops it
     (no duplicate/colliding export on the reuse path).
  2. The ShouldCreateExport + ReuseExternalCur conditions exist with
     the expected shape.
  3. The Glue table's StorageDescriptor.Location is an !If on
     ReuseExternalCur — external URL on reuse, tg's own bucket on
     create — and carries NO hard DependsOn on CurExport (which would
     error when CurExport is conditionally absent).
  4. The ReusedCurS3Url + CreateExport params exist.
"""
from __future__ import annotations

import sys
import yaml


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


def fail(msg):
    print(f"  FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def ok(msg):
    print(f"  ok: {msg}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "cfn/tg-cur-athena.yaml"
    with open(path) as f:
        tpl = yaml.load(f, Loader=_CfnLoader)

    print("== rendered-template: CUR reuse-or-create ==")

    params = tpl.get("Parameters", {})
    conds = tpl.get("Conditions", {})
    res = tpl.get("Resources", {})

    # (4) params present.
    for p in ("CreateExport", "ReusedCurS3Url"):
        if p in params:
            ok(f"param {p} present")
        else:
            fail(f"param {p} missing")

    # (2) conditions present.
    for c in ("ShouldCreateExport", "ReuseExternalCur"):
        if c in conds:
            ok(f"condition {c} present")
        else:
            fail(f"condition {c} missing")

    # (1) Export resource gated on ShouldCreateExport.
    export = res.get("CurExport", {})
    if export.get("Type") != "AWS::BCMDataExports::Export":
        fail("CurExport resource not found / wrong type")
    if export.get("Condition") == "ShouldCreateExport":
        ok("CurExport gated on Condition: ShouldCreateExport "
           "(dropped when CreateExport=false)")
    else:
        fail("CurExport must carry Condition: ShouldCreateExport")

    # CurExport MUST carry Retain on both delete and update-replace.
    # A conditioned data resource with no Retain is deleted whenever its
    # condition flips false (a redeploy that re-detects an existing
    # export). Retain makes that flip non-destructive — the load-bearing
    # safety net for the export-less-on-redeploy regression. (CUR is the
    # sole spend + governance source, so a dropped export silently kills
    # spend tracking + deny enforcement.)
    if export.get("DeletionPolicy") == "Retain" \
            and export.get("UpdateReplacePolicy") == "Retain":
        ok("CurExport carries DeletionPolicy + UpdateReplacePolicy Retain "
           "(condition flip can't delete the export)")
    else:
        fail("CurExport must carry DeletionPolicy: Retain AND "
             "UpdateReplacePolicy: Retain (got "
             f"Deletion={export.get('DeletionPolicy')!r} "
             f"UpdateReplace={export.get('UpdateReplacePolicy')!r})")

    # (3) Glue table: no hard DependsOn on CurExport; Location is an
    #     !If on ReuseExternalCur.
    glue = res.get("CurGlueTable", {})
    if not glue:
        fail("CurGlueTable resource not found")
    dep = glue.get("DependsOn")
    dep_list = dep if isinstance(dep, list) else ([dep] if dep else [])
    if "CurExport" in dep_list:
        fail("CurGlueTable still has DependsOn: CurExport "
             "(errors when CurExport is conditionally absent)")
    ok("CurGlueTable has no hard DependsOn on CurExport")

    sd = glue.get("Properties", {}).get("TableInput", {}) \
        .get("StorageDescriptor", {})
    loc = sd.get("Location")
    if isinstance(loc, dict) and "Fn::If" in loc:
        cond_name = loc["Fn::If"][0]
        if cond_name == "ReuseExternalCur":
            ok("Glue Location is !If ReuseExternalCur "
               "(external URL on reuse, own bucket on create)")
        else:
            fail(f"Glue Location !If keys on {cond_name}, "
                 "expected ReuseExternalCur")
    else:
        fail("Glue Location must be an !If on ReuseExternalCur")

    # The reuse branch must reference ReusedCurS3Url.
    true_branch = loc["Fn::If"][1]
    if true_branch == {"Ref": "ReusedCurS3Url"}:
        ok("reuse branch points Location at ReusedCurS3Url")
    else:
        fail(f"reuse branch must be Ref ReusedCurS3Url, "
             f"got {true_branch!r}")

    print("\nALL RENDERED-TEMPLATE CHECKS PASSED")


if __name__ == "__main__":
    main()
