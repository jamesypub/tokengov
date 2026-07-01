"""Persisted wizard answers — ~/.tg/config-<account>.json (NO secrets).

The config file lets a Ctrl-C'd `tg install` resume where it left
off. It stores only non-sensitive answers; secrets (the OIDC client
secret) are NEVER written — they're prompted fresh each run and
passed through env only. (#487 acceptance: "stateful+resumable
~/.tg/config.json, NO secrets".)

#874: the config path is keyed on the TARGET ACCOUNT by default
(`config-<account_id>.json`) so two installs on one machine
targeting different accounts can't silently cross-resume each
other's answers. The neutral `config.json` remains the path when no
account is known yet (the fully-interactive first load, before the
wizard collects account_id) and as the legacy filename a single
pre-#874 install resumes from. `load`/`save`/`clear` take an
optional `account_id`; omitting it uses the neutral path (the
back-compat default every existing caller relied on).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("TG_CONFIG_HOME", Path.home() / ".tg"))
# The neutral (account-unkeyed) path. Used pre-account-resolution and
# as the legacy single-install filename. CONFIG_PATH stays exported
# for back-compat (callers/tests reference it).
CONFIG_PATH = CONFIG_DIR / "config.json"

# Keys that must NEVER be persisted, even if a caller stuffs them into
# the answers dict. Defense-in-depth against a secret leaking to disk.
SECRET_KEYS = frozenset(
    {
        "oidc_client_secret",
        "TG_OIDC_CLIENT_SECRET",
        "client_secret",
        # #921: optional operator-provided bootstrap admin password
        # (wizard answer key + its env form). Never persist to
        # ~/.tg/config.json — a one-run secret threaded to the
        # installer's env only.
        "bootstrap_password",
        "TG_BOOTSTRAP_ADMIN_PASSWORD",
    }
)


def config_path_for(account_id: str | None = None) -> Path:
    """#874: the config file for a given target account. Account-keyed
    (`config-<account_id>.json`) when the account is known, else the
    neutral `config.json`. Honors TG_CONFIG_HOME via CONFIG_DIR."""
    if account_id:
        return CONFIG_DIR / f"config-{account_id}.json"
    return CONFIG_PATH


def _scrub(answers: dict) -> dict:
    # Drop secrets AND transient run-only keys. #962: the upgrade
    # markers (`_is_upgrade`, `_image_from`) are derived fresh each run
    # from the live stack — persisting them would make a later
    # greenfield re-run wrongly think it's an upgrade. Convention:
    # any leading-underscore answer key is transient, never persisted.
    return {
        k: v for k, v in answers.items()
        if k not in SECRET_KEYS and not k.startswith("_")
    }


def load(account_id: str | None = None) -> dict:
    """Return persisted answers for this account, or {} if none /
    unreadable. With no account_id, reads the neutral config.json."""
    path = config_path_for(account_id)
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save(answers: dict, account_id: str | None = None) -> None:
    """Persist answers (secrets scrubbed) to the account-keyed config
    (or the neutral config.json when account_id is omitted)."""
    path = config_path_for(account_id)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    safe = _scrub(answers)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(safe, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)
    # Owner-only — the file holds account IDs / emails.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def clear(account_id: str | None = None) -> None:
    """Remove the persisted config (used after a clean success)."""
    try:
        config_path_for(account_id).unlink()
    except FileNotFoundError:
        pass


def migrate_legacy(account_id: str) -> dict:
    """#874: adopt a legacy neutral `config.json` for this account
    ONLY when it matches — i.e. its stored account_id equals
    account_id, or it has no account_id recorded (a pre-account-keyed
    single install). Never resume a config whose account_id differs
    (that's another account's install — the silent-cross-contamination
    bug). Returns the adopted answers (also written to the
    account-keyed path) or {} if there's nothing safe to adopt.

    No-op if the account-keyed file already exists (its own resume
    wins over the legacy file)."""
    if not account_id:
        return {}
    keyed = config_path_for(account_id)
    if keyed.exists():
        return load(account_id)
    legacy = CONFIG_PATH
    if keyed == legacy or not legacy.exists():
        return {}
    try:
        with legacy.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    legacy_acct = data.get("account_id")
    if legacy_acct and legacy_acct != account_id:
        # Belongs to a different account — do NOT adopt/resume it.
        return {}
    # Matches (same account) or is account-less (pre-#874) → adopt it
    # under the account-keyed name so future runs resume cleanly.
    save(data, account_id)
    return load(account_id)
