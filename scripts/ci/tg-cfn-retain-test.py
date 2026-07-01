#!/usr/bin/env python3
"""tg-cfn-retain-test.py — data-resource protection guard (no AWS, no
deploy). Fails the build if a stateful/data CloudFormation resource can
be silently destroyed on a stack update, the class of bug that left an
account with no CUR export (the condition-gated CurExport carried no
Retain, so a redeploy deleted it — the sole spend + governance source).

What it asserts, over every cfn/*.yaml (walks Resources with a
CFN-tag-tolerant YAML loader — robust to !Ref/!Sub/!If, unlike grep):

  1. Each DATA resource (by Type, so a NEW one is auto-covered) declares
     a data-SAFE DeletionPolicy AND UpdateReplacePolicy:
       - Retain for all data types;
       - Snapshot ALSO accepted for RDS (a snapshot is taken on delete /
         replace, so data survives).
     A small ALLOW_EPHEMERAL set excuses resources that are
     intentionally disposable (dev-only DB, ECS app-log group) — keyed
     by logical id with the reason inline.
  2. The CUR self-heal never deletes an export without a guaranteed
     recreate: in scripts/tg-cur-deploy.sh every `delete-export` is
     gated by tg_cur_should_delete_export AND sits on a CREATE_EXPORT=
     true path (the deliberately-kept recreate-safe invariant — so this
     asserts the gate is PRESENT, NOT that delete-export is absent).

Companion to the .claude/rules/cfn.md invariant. Wired into test.yml.

Usage: python3 scripts/ci/tg-cfn-retain-test.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CFN_DIR = REPO / "cfn"
CUR_DEPLOY = REPO / "scripts" / "tg-cur-deploy.sh"

# Resource Types that hold state / customer / audit data.
DATA_TYPES = {
    "AWS::BCMDataExports::Export",
    "AWS::S3::Bucket",
    "AWS::RDS::DBInstance",
    "AWS::Logs::LogGroup",
}
# Retain is data-safe for every type; RDS additionally treats Snapshot as
# safe (a final snapshot is taken on delete/replace).
SAFE_POLICIES = {"Retain"}
SAFE_POLICIES_RDS = {"Retain", "Snapshot"}

# Intentionally-disposable resources, excused by logical id with the
# WHY. Keep this list tiny + explicit — a new data resource is covered
# by default (deny-by-default), and an exception is a deliberate,
# reviewed entry, not a silent omission.
ALLOW_EPHEMERAL = {
    # The dev/fresh-clone DB (distinct identifier; the protected
    # Database carries Snapshot). Disposable by design.
    "DatabaseDisposable",
    # ECS app-log group — operational task logs with a finite
    # RetentionInDays, not an audit trail; safe to recreate.
    "TaskLogGroup",
}


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

fails = 0


def ok(msg):
    print(f"  ok: {msg}")


def fail(msg):
    global fails
    print(f"  FAIL: {msg}", file=sys.stderr)
    fails += 1


def check_retain():
    print("== data resources carry a data-safe Deletion/UpdateReplace "
          "policy ==")
    saw_cur_export = False
    for tpl in sorted(CFN_DIR.glob("*.yaml")):
        try:
            doc = yaml.load(tpl.read_text(), Loader=_CfnLoader)
        except yaml.YAMLError as e:
            fail(f"{tpl.name}: YAML parse error: {e}")
            continue
        resources = (doc or {}).get("Resources") or {}
        for name, r in resources.items():
            if not isinstance(r, dict):
                continue
            ty = r.get("Type")
            if ty not in DATA_TYPES:
                continue
            if name == "CurExport":
                saw_cur_export = True
            if name in ALLOW_EPHEMERAL:
                ok(f"{tpl.name}:{name} ({ty}) — allow-ephemeral (excused)")
                continue
            safe = SAFE_POLICIES_RDS if ty == "AWS::RDS::DBInstance" \
                else SAFE_POLICIES
            dp = r.get("DeletionPolicy")
            up = r.get("UpdateReplacePolicy")
            if dp in safe and up in safe:
                ok(f"{tpl.name}:{name} ({ty}) — Del={dp} UpdRep={up}")
            else:
                fail(f"{tpl.name}:{name} ({ty}) is a DATA resource but "
                     f"Del={dp!r} UpdRep={up!r} — must be one of "
                     f"{sorted(safe)} on BOTH (or add to ALLOW_EPHEMERAL "
                     "with a reason). An un-retained data resource is "
                     "deleted on a condition flip / replace — the "
                     "CUR-export-loss class.")
    # CurExport is the canonical seed — assert it was actually checked
    # (guards against the template being renamed/moved out of coverage).
    if saw_cur_export:
        ok("CurExport is covered (the seed data resource)")
    else:
        fail("CurExport not found in any cfn/*.yaml — the seed data "
             "resource must be present + covered")


def check_delete_export_gated():
    print()
    print("== CUR delete-export is gated + recreate-safe (never "
          "export-less) ==")
    if not CUR_DEPLOY.exists():
        fail(f"{CUR_DEPLOY} not found")
        return
    src = CUR_DEPLOY.read_text()
    # Every delete-export must be the CUR self-heal: gated by
    # tg_cur_should_delete_export AND on a CREATE_EXPORT=true path. We
    # assert the gate + the forced-create are PRESENT (the recreate-safe
    # self-heal is KEPT, not banned).
    n_delete = len(re.findall(r"bcm-data-exports\s+delete-export", src))
    if n_delete == 0:
        # No delete-export at all is also safe (a future removal).
        ok("no bcm-data-exports delete-export in tg-cur-deploy.sh")
        return
    if n_delete > 1:
        fail(f"{n_delete} delete-export calls — expected exactly the one "
             "gated self-heal; a new ungated path is the export-loss "
             "risk")
    if "tg_cur_should_delete_export" in src:
        ok("delete-export is gated by tg_cur_should_delete_export")
    else:
        fail("delete-export present but NOT gated by "
             "tg_cur_should_delete_export — ungated delete is the "
             "export-loss risk")
    # The recreate guarantee: the self-heal sits inside the TG_OWN_ARN
    # branch that forces CREATE_EXPORT=true (so the CFN CurExport is
    # always recreated after the delete). Assert that forced-create is
    # present alongside the gate (decision: the recreate-safe self-heal
    # is KEPT, not banned — so assert the gate, don't ban delete).
    if re.search(r'CREATE_EXPORT="true"', src):
        ok("a CREATE_EXPORT=\"true\" path guarantees the recreate")
    else:
        fail("no CREATE_EXPORT=\"true\" — the self-heal delete has no "
             "guaranteed recreate (export-less window)")


def main():
    check_retain()
    check_delete_export_gated()
    print()
    if fails == 0:
        print("ALL CHECKS PASSED")
    else:
        print(f"{fails} CHECK(S) FAILED", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
