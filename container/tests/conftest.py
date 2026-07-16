"""
Shared fixtures: a session-scoped Postgres testcontainer with
the schema loaded, and a function-scoped DB session that
truncates tables between tests so worker job tests don't leak
state into each other.
"""
from __future__ import annotations
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

try:  # newer testcontainers: structured wait strategy (preferred)
    from testcontainers.core.waiting_utils import LogMessageWaitStrategy
except ImportError:  # older testcontainers: fall back to wait_for_logs
    LogMessageWaitStrategy = None
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def mailpit():
    """A session-scoped Mailpit testcontainer for the notification
    delivery tests (mirrors the Postgres fixture). Mailpit captures
    mail and never delivers it: it listens for SMTP on 1025 and serves
    a capture REST API on 8025. Started/torn-down per session.

    Mailpit is a TEST DEPENDENCY ONLY — it must never appear in the
    shipped runtime (the leak-guard AC). Tests read the mapped host +
    ports off the container and point `notify.send_alert` at them.
    """
    c = (
        DockerContainer("axllent/mailpit:latest")
        .with_exposed_ports(1025, 8025)
    )
    # Wait for the HTTP API to be up before any test queries it.
    if LogMessageWaitStrategy is not None:
        c.waiting_for(LogMessageWaitStrategy("accessible via"))
        with c:
            yield c   # auto-removed at session end
    else:
        with c:
            wait_for_logs(c, "accessible via", timeout=30)
            yield c


@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("postgres:16") as pg:
        url = pg.get_connection_url()
        # Rebind db.session before models import touches the engine
        import os
        os.environ["DATABASE_URL"] = url
        import db.session as _dbs
        _dbs.DATABASE_URL = url
        _dbs.engine = create_engine(url, pool_pre_ping=True)
        _dbs.SessionLocal = sessionmaker(
            bind=_dbs.engine, autocommit=False, autoflush=False
        )
        from db.models import Base
        Base.metadata.create_all(bind=_dbs.engine)
        yield url


@pytest.fixture
def clean_db(pg_url):
    """Truncate worker-test tables between tests."""
    import db.session as _dbs
    with _dbs.engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE TABLE quota_policies, "
            "users, model_pricing, admin_config, "
            "team_memberships, teams, admin_roles, "
            "jira_sites, jira_issues, pr_jira_refs, "
            "jira_weekly_metrics, github_activity, "
            "linked_accounts, pr_classifications, "
            "team_weekly_metrics, team_daily_metrics, "
            "discovered_models, model_pricing_audit, "
            "service_account_caps, service_account_alerts, "
            "principal_models, sync_state, "
            "governance_drift, cur_user_spend "
            "RESTART IDENTITY CASCADE"
        ))
    yield
