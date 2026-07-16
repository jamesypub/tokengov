"""
#926: the tg_owns_directory org-config flag — does tg own the user
directory (Cognito provisions logins) or does an external IdP? Covers
the db/org_config helpers + the one-time seed (incl. the existing-Okta
→ false migration so a live federated install doesn't silently flip).
"""
from __future__ import annotations


def test_default_true_when_absent(clean_db):
    """A fresh install (key absent) is Cognito-only → tg owns it."""
    from db.session import get_db
    from db.org_config import tg_owns_directory
    with get_db() as db:
        assert tg_owns_directory(db) is True


def test_set_then_get_roundtrips(clean_db):
    from db.session import get_db
    from db.org_config import tg_owns_directory, set_tg_owns_directory
    with get_db() as db:
        assert set_tg_owns_directory(db, False) is False
    with get_db() as db:
        assert tg_owns_directory(db) is False
    with get_db() as db:
        set_tg_owns_directory(db, True)
    with get_db() as db:
        assert tg_owns_directory(db) is True


def test_malformed_value_reads_true(clean_db):
    """Any non-'false' stored value fails safe to the tg-owned
    default (so a malformed row never silently disables Cognito)."""
    from db.session import get_db
    from db.models import AdminConfig
    from db.org_config import tg_owns_directory, TG_OWNS_DIRECTORY_KEY
    with get_db() as db:
        db.add(AdminConfig(key=TG_OWNS_DIRECTORY_KEY, value="garbage"))
    with get_db() as db:
        assert tg_owns_directory(db) is True


def test_seed_fresh_install_defaults_true(clean_db):
    """No TG_AUTH_PROVIDER env (fresh install) → seed true."""
    from db.session import get_db
    from db.org_config import seed_tg_owns_directory, tg_owns_directory
    with get_db() as db:
        seed_tg_owns_directory(db, None)
    with get_db() as db:
        assert tg_owns_directory(db) is True


def test_seed_existing_okta_install_seeds_false(clean_db):
    """An existing install with TG_AUTH_PROVIDER=okta seeds false so a
    live federated deployment doesn't silently flip to Cognito."""
    from db.session import get_db
    from db.org_config import seed_tg_owns_directory, tg_owns_directory
    with get_db() as db:
        seed_tg_owns_directory(db, "okta")
    with get_db() as db:
        assert tg_owns_directory(db) is False


def test_seed_is_insert_if_absent_only(clean_db):
    """Seed never overwrites an existing value — DB is the source of
    truth thereafter (an operator's later flip survives a reboot's
    re-seed, even if the env still says okta)."""
    from db.session import get_db
    from db.org_config import (
        seed_tg_owns_directory, set_tg_owns_directory, tg_owns_directory,
    )
    with get_db() as db:
        set_tg_owns_directory(db, True)        # operator chose Cognito
    with get_db() as db:
        seed_tg_owns_directory(db, "okta")     # reboot, env still okta
    with get_db() as db:
        assert tg_owns_directory(db) is True   # not clobbered
