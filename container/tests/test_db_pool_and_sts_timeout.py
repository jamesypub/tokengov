"""#547 regression: the api must not exhaust the DB pool.

The SQLAlchemy engine is configured with headroom + a finite
pool_timeout + pool_recycle (fail-fast instead of stacking 30s
waits, and recycle stale conns) so a wedged request fails fast
instead of exhausting the pool.

#576: the companion STS-timeout guard (which asserted
auth._validate_sigv4 passed a bounded urlopen timeout) was
removed — `_validate_sigv4` and its STS round-trip are gone with
the deleted desktop client, so there's no longer an unbounded
external call on the request path holding a DB connection. The
pool hardening below still ships and still matters.

No real AWS / DB — the engine config is introspected.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

_CONTAINER = Path(__file__).resolve().parents[1]
if str(_CONTAINER) not in sys.path:
    sys.path.insert(0, str(_CONTAINER))


def test_engine_pool_is_hardened():
    """#547: headroom + finite pool_timeout + recycle, so a wedged
    request fails fast instead of exhausting the pool over 30s.

    Assert on the CONFIG session.py applies (`_engine_kwargs()`),
    NOT on `s.engine.pool`: the suite's conftest rebinds
    `db.session.engine` to a throwaway testcontainer engine built
    WITHOUT these kwargs (default pool_size=5, _timeout=30), so
    under `pytest tests/` `s.engine` is the default engine and the
    old assertions failed in CI (they passed only when session.py
    was imported without the conftest rebind). The kwargs are what
    actually ship.
    """
    import db.session as s

    kw = s._engine_kwargs()
    # ceiling well above the old 5+10=15 that exhausted
    assert kw["pool_size"] >= 10
    assert kw["pool_size"] + kw["max_overflow"] >= 30
    # finite, short-ish checkout wait (default was 30s)
    assert 0 < kw["pool_timeout"] <= 15, f"pool_timeout={kw['pool_timeout']}"
    # connections recycled (not held forever)
    assert kw["pool_recycle"] and kw["pool_recycle"] > 0
    assert kw["pool_pre_ping"] is True

    # And the kwargs really produce a hardened QueuePool when
    # applied to a fresh engine (not the conftest-rebound one).
    from sqlalchemy import create_engine
    eng = create_engine("postgresql://tg:tg@localhost:5432/tg", **kw)
    try:
        assert eng.pool.size() >= 10
        assert 0 < eng.pool._timeout <= 15
        assert eng.pool._recycle and eng.pool._recycle > 0
    finally:
        eng.dispose()


def test_engine_kwargs_env_overridable():
    """#547: per-env tuning via TG_DB_POOL_* envs. `_engine_kwargs`
    reads os.environ at call time, so this needs no module reload
    (a reload would clobber the conftest's engine rebind)."""
    import db.session as s
    with mock.patch.dict("os.environ", {
        "TG_DB_POOL_SIZE": "3",
        "TG_DB_POOL_TIMEOUT": "7",
    }):
        kw = s._engine_kwargs()
        assert kw["pool_size"] == 3
        assert kw["pool_timeout"] == 7
    # outside the patch, defaults return
    assert s._engine_kwargs()["pool_size"] == 10
