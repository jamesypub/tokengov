"""The phase-1 check registry.

`all_checks()` returns the ordered list of Check objects the engine runs
by default. Each category module exposes a `CHECKS` list; adding a check
is adding a Check to its module — no engine change.
"""
from __future__ import annotations

from diagnostics.checks import (
    identity,
    cur_pipeline,
    app_runtime,
    governance,
)


def all_checks():
    checks = []
    for mod in (identity, cur_pipeline, app_runtime, governance):
        checks.extend(mod.CHECKS)
    return checks
