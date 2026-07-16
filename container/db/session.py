from __future__ import annotations
import os
from urllib.parse import quote_plus
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager


def _build_database_url() -> str:
    """Resolve DATABASE_URL.

    Precedence:
      1. DATABASE_URL env (compose / local dev)
      2. DB_HOST + DB_USER + DB_NAME + DB_PASSWORD (ECS task path:
         ECS injects DB_PASSWORD via Secrets Manager and DB_HOST
         from the RDS endpoint output)
      3. Fallback to local docker-compose default
    """
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit

    host = os.environ.get("DB_HOST")
    if host:
        user = os.environ.get("DB_USER", "tg")
        name = os.environ.get("DB_NAME", "tg")
        port = os.environ.get("DB_PORT", "5432")
        password = os.environ.get("DB_PASSWORD", "")
        # quote_plus handles special chars even though the
        # generated password excludes the worst offenders.
        return (
            f"postgresql://{user}:{quote_plus(password)}"
            f"@{host}:{port}/{name}"
        )

    return "postgresql://tg:tg@localhost:5432/tg"


DATABASE_URL = _build_database_url()


def _engine_kwargs() -> dict:
    """#547: pool sizing + hardening. The default 5+10 pool gave
    zero headroom — when the per-request STS auth call slowed,
    in-flight requests pinned their connections and a few clicks
    exhausted the pool (QueuePool timeout → 504 → ECS recycled the
    task). Raise the ceiling, recycle stale connections, keep
    pre-ping, and shorten the checkout wait so a wedge FAILS FAST
    instead of stacking 30s waits. All env-overridable for per-env
    tuning.

    Factored out so the config is unit-testable independently of
    the live `engine` object — the test suite's conftest rebinds
    `db.session.engine` to a throwaway testcontainer engine
    (default pool), so asserting on `engine.pool` under
    `pytest tests/` would test the harness, not this config.
    """
    return dict(
        pool_pre_ping=True,
        pool_size=int(os.environ.get("TG_DB_POOL_SIZE", "10")),
        max_overflow=int(os.environ.get("TG_DB_MAX_OVERFLOW", "20")),
        pool_recycle=int(os.environ.get("TG_DB_POOL_RECYCLE", "1800")),
        pool_timeout=int(os.environ.get("TG_DB_POOL_TIMEOUT", "10")),
    )


engine = create_engine(DATABASE_URL, **_engine_kwargs())
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@contextmanager
def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
