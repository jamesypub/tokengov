"""tg — one-click install CLI for the Token Governance pilot (#487).

A thin Python front-end over the proven bash installers
(scripts/tg-ecs-install.sh, tg-ecs-destroy.sh) and the cert helper
(scripts/tg-make-selfsigned-cert.sh). The CLI collects answers via a
7-question wizard, persists them (no secrets) to ~/.tg/config.json so
a Ctrl-C'd run resumes, then sets the TG_* env contract and execs the
relevant script. It deliberately owns NO deploy logic of its own —
the bash scripts remain the single source of truth.
"""

def _read_version() -> str:
    """Single-source the version from the tracked VERSION file at repo
    root (#1049) — the same file the publish flow tags the public orphan
    with, so `tg --version` and the `tg install` build banner can never
    drift. Resolved relative to this file (parents[3]: tg_cli → python →
    scripts → repo root), not cwd. Falls back to a dev marker if the
    file is unreadable — e.g. if the package is ever installed/zipped
    away from the repo tree (then switch to importlib.metadata, the
    canonical PyPA pattern)."""
    from pathlib import Path
    try:
        v = (Path(__file__).resolve().parents[3] / "VERSION") \
            .read_text().strip()
        return v or "0.0.0+dev"
    except OSError:
        return "0.0.0+dev"


__version__ = _read_version()
